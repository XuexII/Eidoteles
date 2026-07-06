# 引擎 v2 编排器（默认，v0）
#
# 这是可自我修改的执行循环。它将 Rust ExecutionLoop::run() 替换为可在运行时由自我改进任务修补的 Python。
#
# 主机函数（由 Rust 通过 Monty 挂起机制提供）：
#   __llm_complete__(messages, actions, config)  -> 响应字典
#   __execute_code_step__(code, state)           -> 结果字典
#   __execute_action__(name, params)             -> 结果字典
#   __execute_actions_parallel__(calls)          -> 结果字典列表（并行执行）
#   __check_signals__()                          -> None | "stop" | {"inject": 消息}
#   __emit_event__(kind, **data)                 -> None
#   __save_checkpoint__(state, counters)         -> None
#   __transition_to__(state, reason)             -> None
#   __retrieve_docs__(goal, max_docs)            -> 文档字典列表
#   __check_budget__()                           -> 预算字典
#   __get_actions__()                            -> 动作字典列表
#   __list_skills__()                            -> 技能字典列表
#   __record_skill_usage__(doc_id, success)      -> None
#   __regex_match__(pattern, text)               -> bool
#
# 上下文变量（由 Rust 在执行前注入）：
#   context  - 先前的消息列表 [{role, content}]
#   goal     - 线程目标字符串
#   actions  - 可用动作定义列表
#   state    - 来自先前步骤的持久化状态字典
#   config   - 线程配置字典


import os
import json
import time
import asyncio
import threading
from dataclasses import dataclass, field
from typing import Optional, List, Any, Dict
import logging
from monty import (
    ExtFunctionResult, LimitedTracker, MontyObject, MontyRun, NameLookupResult, PrintWriter,
    ResourceLimits, RunProgress,
)

from scripting import (execute_code, json_to_monty, monty_to_json, monty_to_string)
from thread_context import thread_execution_context

from ..capability.lease import LeaseManager
from ..capability.policy import PolicyEngine
from ..memory.RetrievalEngine
from ..runtime.lease_refresh import reconcile_dynamic_tool_lease
from ..runtime.messaging import (SignalReceiver, ThreadOutcome, ThreadSignal)
from ..traits.effect import (EffectExecutor, ThreadExecutionContext)
from ..traits.llm import (LlmBackend, LlmCallConfig)
from ..traits.store import Store
from ..types.error import (EngineError, OrchestratorFailure, OrchestratorFailureKind)
from ..types.event import (EventKind, ThreadEvent, summarize_params)
from ..types.message import ThreadMessage
from ..types.project import ProjectId
from ..types import shared_owner_id
from ..types.step import (ActionCall, StepId, TokenUsage)
from ..types.thread import (ActiveSkillProvenance, Thread, ThreadState)

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────

# 编译时的默认编排器（v0）
# 注意：实际内容应从文件加载，此处为占位符
DEFAULT_ORCHESTRATOR = "../../orchestrator/default.py"

# Store 中编排器代码的知名标题
ORCHESTRATOR_TITLE = "orchestrator:main"

# 编排器代码文档的知名标签
ORCHESTRATOR_TAG = "orchestrator_code"

# 编排器故障跟踪的知名标题
FAILURE_TRACKER_TITLE = "orchestrator:failures"

# 自动回滚前的最大连续故障次数
MAX_FAILURES_BEFORE_ROLLBACK = 3

# 租约刷新警告间隔（秒）
LEASE_REFRESH_WARN_INTERVAL_SECS = 60

# 默认编排器 VM 实际时间预算，以秒为单位
ORCHESTRATOR_DEFAULT_MAX_DURATION_SECS = 300
# 可配置编排器预算的下限，防止无意义的值
ORCHESTRATOR_MIN_MAX_DURATION_SECS = 30
# 可配置编排器预算的上限，限制资源浪费
ORCHESTRATOR_MAX_MAX_DURATION_SECS = 3600

# 用于限流租约刷新警告的最后警告时间戳
_last_lease_warn_ts = 0
_last_lease_warn_lock = threading.Lock()


# ── 编排器结果 ───────────────────────────────────────────────

@dataclass
class OrchestratorResult:
    """运行编排器的结果"""
    # 从编排器返回值解析的线程结果
    outcome: ThreadOutcome
    # 编排器内 LLM 调用使用的总 token 数
    tokens_used: TokenUsage


# ── 编排器故障分类 ───────────────────────────────────────────

class OrchestratorFailureKind:
    """编排器故障类型"""
    pass


@dataclass
class TimeLimit(OrchestratorFailureKind):
    """时间限制"""
    prefix: str
    limit_secs: int


@dataclass
class ResourceLimit(OrchestratorFailureKind):
    """资源限制"""
    prefix: str


@dataclass
class Traceback(OrchestratorFailureKind):
    """Python 回溯"""
    prefix: str


@dataclass
class VmPanic(OrchestratorFailureKind):
    """VM 恐慌"""
    prefix: str
    phase: str


@dataclass
class Other(OrchestratorFailureKind):
    """其他故障"""
    prefix: str


@dataclass
class OrchestratorFailure:
    """编排器故障，携带用户安全分类和保留的低级详细信息"""
    kind: OrchestratorFailureKind
    debug_detail: str


# ── 资源限制 ─────────────────────────────────────────────────

@dataclass
class ResourceLimits:
    """编排器 VM 的资源限制"""
    max_duration_secs: int = 300
    max_allocations: int = 5_000_000
    max_memory: int = 128 * 1024 * 1024  # 128 MB


# ── 辅助函数 ─────────────────────────────────────────────────

def _orchestrator_max_duration() -> int:
    """从环境变量解析编排器 VM 实际时间预算

    从 `IRONCLAW_ORCHESTRATOR_MAX_DURATION_SECS` 解析。
    进程生命周期内缓存
    """
    # 使用模块级变量模拟 OnceLock
    if not hasattr(_orchestrator_max_duration, '_cached'):
        secs_str = os.environ.get("IRONCLAW_ORCHESTRATOR_MAX_DURATION_SECS", "")
        try:
            secs = int(secs_str.strip())
        except (ValueError, AttributeError):
            secs = ORCHESTRATOR_DEFAULT_MAX_DURATION_SECS

        # 限制在合理范围内
        secs = max(ORCHESTRATOR_MIN_MAX_DURATION_SECS,
                   min(secs, ORCHESTRATOR_MAX_MAX_DURATION_SECS))
        _orchestrator_max_duration._cached = secs

    return _orchestrator_max_duration._cached


def orchestrator_limits() -> ResourceLimits:
    """获取编排器 VM 的资源限制"""
    return ResourceLimits(
        max_duration_secs=_orchestrator_max_duration(),
        max_allocations=5_000_000,
        max_memory=128 * 1024 * 1024,  # 128 MB
    )


def apply_snapshot_inventory(
        exec_ctx: ThreadExecutionContext,
        inventory: Optional[ActionInventory] = None,
) -> List[ActionDef]:
    """将快照清单应用到执行上下文"""
    if inventory is not None:
        available_actions = list(inventory.inline)
        exec_ctx.available_actions_snapshot = available_actions
        exec_ctx.available_action_inventory_snapshot = inventory
    else:
        available_actions = []

    return available_actions


def normalize_pause_outcome(thread: Thread, outcome: ThreadOutcome) -> None:
    """规范化暂停结果：如果结果是 GatePaused 且线程不在 Waiting 状态，则转换"""
    if isinstance(outcome, ThreadOutcome) and hasattr(outcome, 'gate_name'):
        if thread.state != ThreadState.Waiting:
            thread.transition_to(
                ThreadState.Waiting,
                "等待外部门控解决方案",
            )


def classify_orchestrator_failure(prefix: str, err_msg: str) -> OrchestratorFailure:
    """将 Monty 编排器故障分类为类型化的 [`OrchestratorFailure`]

    携带用户安全分类和保留的低级详细信息以供网关调试模式使用

    原始 `err_msg`（通常包含内部文件路径和上游 HTTP 正文的 Python 回溯）
    始终存储在返回结构体的 `debug_detail` 字段中并以 `debug` 级别发出，
    永远不会放入用户可见的分类中 — 参见 `.claude/rules/error-handling.md`，
    "Error Boundaries at the Channel Edge" (#2546)
    """
    logger.debug(f"编排器 VM 故障: {prefix}: {err_msg}")

    lower = err_msg.lower()

    # 保留 `TimeLimit` 用于明确的 Monty 实际时间标记 —
    # 面向用户的消息告诉操作员提高 `IRONCLAW_ORCHESTRATOR_MAX_DURATION_SECS`，
    # 这对于上游 LLM/网络超时是错误的建议。
    # 裸露的 "timeout"/"timed out" 过去会捕获这些
    # （例如 `reqwest` 的 "Request timed out"、提供者的 "Connection timed out"）
    # 并将用户指向预算旋钮而不是真正的故障类别。
    # 这些现在落入 `Other`（通用内部故障）。
    # 参考：PR #2753 的 serrrfirat 审查，提交 82d06410
    #
    # 我们保留的谓词要么是 VM 自身错误文本中的显式环境变量名，
    # 要么是 Monty 运行时用于其持续时间限制的短语，
    # 要么是引擎在编排器自身超时步骤时发出的哨兵。
    # 重复 `ResourceLimits` 措辞是可以的 — 这些字符串与此分类器位于同一 crate 中
    hit_time_limit = (
            "duration limit" in lower
            or "max_duration" in lower
            or "maximum duration" in lower
            or "execution duration exceeded" in lower
            or "orchestrator timed out" in lower
    )
    hit_memory_limit = "memory limit" in lower or "allocation limit" in lower
    hit_resource_limit = (
            "resource limit" in lower
            or "out of fuel" in lower
            or "fuel exhausted" in lower
    )
    has_python_traceback = (
            "traceback (most recent call last)" in lower
            or "traceback:" in lower
    )

    if hit_time_limit:
        kind = TimeLimit(
            prefix=prefix,
            limit_secs=_orchestrator_max_duration(),
        )
    elif hit_memory_limit or hit_resource_limit:
        kind = ResourceLimit(prefix=prefix)
    elif has_python_traceback:
        kind = Traceback(prefix=prefix)
    else:
        kind = Other(prefix=prefix)

    return OrchestratorFailure(kind=kind, debug_detail=err_msg)


def orchestrator_vm_panic(prefix: str, phase: str) -> OrchestratorFailure:
    """将 Monty VM 恐慌（解析/启动/恢复阶段）包装为类型化的编排器故障

    恐慌本身没有文本负载 — 我们可以字符串化的 `panic_payload`
    始终是来自 `catch_unwind` 的 `str` 或 `String` —
    因此 `debug_detail` 携带阶段标签以便关联
    """
    logger.debug(f"编排器 VM 恐慌: {prefix}, 阶段: {phase}")
    return OrchestratorFailure(
        kind=VmPanic(prefix=prefix, phase=phase),
        debug_detail=f"Monty VM 在 {phase} 期间发生恐慌",
    )


def warn_on_lease_refresh_failure(context: str, error: EngineError) -> None:
    """限流租约刷新失败的警告日志"""
    global _last_lease_warn_ts

    now = int(time.time())

    with _last_lease_warn_lock:
        if now - _last_lease_warn_ts >= LEASE_REFRESH_WARN_INTERVAL_SECS:
            _last_lease_warn_ts = now
            logger.warning(f"动态租约刷新失败: {context}: {error}")
        else:
            logger.debug(f"动态租约刷新失败: {context}: {error}")


def load_failure_count(docs: List[MemoryDoc]) -> int:
    """加载最新编排器版本的故障计数"""
    for doc in docs:
        if doc.title == FAILURE_TRACKER_TITLE:
            try:
                data = json.loads(doc.content)
                return data.get("count", 0)
            except (json.JSONDecodeError, TypeError):
                return 0
    return 0


def load_orchestrator_from_docs(
        docs: List[MemoryDoc],
        allow_self_modify: bool,
) -> tuple:
    """从预获取的系统记忆文档加载编排器

    当调用者已经拥有 `list_memory_docs` 结果时，使用此函数
    以避免重复的 Store 查询。返回 `(code, version)`

    尊重 `allow_self_modify` — 为 false 时，始终返回编译时的默认值。
    `loop_engine.rs` 中的调用者从引擎配置传入此参数
    """
    if not allow_self_modify:
        return (DEFAULT_ORCHESTRATOR, 0)

    # 查找所有编排器版本，按版本号降序排序
    versions = []
    for doc in docs:
        if doc.title == ORCHESTRATOR_TITLE and ORCHESTRATOR_TAG in doc.tags:
            versions.append(doc)

    # 按版本降序排序
    versions.sort(
        key=lambda d: d.metadata.get("version", 0) if isinstance(d.metadata, dict) else 0,
        reverse=True,
    )

    if not versions:
        logger.debug("使用编译时默认编排器 (v0)")
        return (DEFAULT_ORCHESTRATOR, 0)

    # 检查最新版本的故障计数
    failures = load_failure_count(docs)

    for doc in versions:
        version = doc.metadata.get("version", 1) if isinstance(doc.metadata, dict) else 1

        # 跳过故障过多的版本（仅检查最新的）
        latest_version = versions[0].metadata.get("version", 1) if isinstance(versions[0].metadata, dict) else 1
        if version == latest_version and failures >= MAX_FAILURES_BEFORE_ROLLBACK:
            logger.debug(f"编排器版本 {version} 故障过多 ({failures})，跳过")
            continue

        logger.debug(f"已加载运行时编排器 (v{version})")
        return (doc.content, version)

    # 所有版本都失败 — 回退到编译时默认值
    logger.debug("所有编排器版本均失败，使用编译时默认编排器 (v0)")
    return (DEFAULT_ORCHESTRATOR, 0)


async def load_orchestrator(
        store: Optional[Store],
        project_id: ProjectId,
        allow_self_modify: bool,
) -> tuple:
    """加载编排器代码：来自 Store 的运行时版本，或编译时的默认值

    当 `allow_self_modify` 为 false 时，无论 Store 中有任何运行时版本，
    始终使用编译时的默认值。这是生产环境的安全默认值 —
    运行时编排器修补是选择加入的

    检查故障跟踪器 — 如果最新版本有 >= 3 次连续故障，
    回退到前一个版本（或编译时默认值）
    """
    if not allow_self_modify:
        logger.debug("编排器自我修改已禁用，使用编译时默认编排器 (v0)")
        return (DEFAULT_ORCHESTRATOR, 0)

    if store is None:
        logger.debug("使用编译时默认编排器 (v0，无 Store)")
        return (DEFAULT_ORCHESTRATOR, 0)

    try:
        docs = await store.list_shared_memory_docs(project_id)
    except Exception:
        logger.debug("使用编译时默认编排器 (v0，Store 错误)")
        return (DEFAULT_ORCHESTRATOR, 0)

    return load_orchestrator_from_docs(docs, allow_self_modify)


async def record_orchestrator_failure(
        store: Store,
        project_id: ProjectId,
        version: int,
) -> None:
    """记录当前编排器版本的故障"""
    try:
        docs = await store.list_shared_memory_docs(project_id)
    except Exception as e:
        logger.debug(f"列出故障跟踪器的记忆文档失败: {e}")
        return

    existing = None
    for doc in docs:
        if doc.title == FAILURE_TRACKER_TITLE:
            existing = doc
            break

    if existing is not None:
        tracker = existing.clone()
    else:
        tracker = MemoryDoc.new(
            project_id,
            shared_owner_id(),
            DocType.Note,
            FAILURE_TRACKER_TITLE,
            "",
        )
        tracker = tracker.with_tags(["orchestrator_meta"])

    # 将故障计数存储为内容中的 JSON: {"version": N, "count": M}
    try:
        current = json.loads(tracker.content)
    except (json.JSONDecodeError, TypeError):
        current = {}

    current_version = current.get("version", 0)
    current_count = current.get("count", 0)

    if current_version == version:
        new_count = current_count + 1
    else:
        new_count = 1  # 新版本，重置计数

    tracker.content = json.dumps({
        "version": version,
        "count": new_count,
    })
    tracker.updated_at = datetime.now(timezone.utc)

    # 故障跟踪器携带 `orchestrator:` 标题前缀，因此被 Store 中的
    # `is_protected_orchestrator_doc` 门控。
    # 进入受信任的内部写入作用域，以便系统发起的保存被接纳，
    # 而不会被误认为是 LLM 编写的补丁
    try:
        await with_trusted_internal_writes(store.save_memory_doc(tracker))
    except Exception as e:
        logger.debug(f"保存编排器故障跟踪器失败: {e}")

    logger.debug(f"已记录编排器故障: version={version}, count={new_count}")


async def reset_orchestrator_failures(store: Store, project_id: ProjectId) -> None:
    """重置故障计数器（成功执行后调用）"""
    try:
        docs = await store.list_shared_memory_docs(project_id)
    except Exception:
        docs = []

    existing = None
    for doc in docs:
        if doc.title == FAILURE_TRACKER_TITLE:
            existing = doc
            break

    if existing is not None:
        tracker = existing.clone()
        tracker.content = json.dumps({"version": 0, "count": 0})
        tracker.updated_at = datetime.now(timezone.utc)
        # 与 `record_orchestrator_failure` 相同的理由：跟踪器文档
        # 具有 `orchestrator:` 标题，因此 Store 门控会触发。
        # 进入受信任的写入作用域以进行此系统发起的重置
        try:
            await with_trusted_internal_writes(store.save_memory_doc(tracker))
        except Exception:
            pass


# ── 编排器执行 ───────────────────────────────────────────────

async def execute_orchestrator(
        code: str,
        thread: Thread,
        llm: LlmBackend,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        signal_rx: SignalReceiver,
        event_tx: Optional[asyncio.Queue] = None,
        retrieval: Optional[RetrievalEngine] = None,
        store: Optional[Store] = None,
        platform_info: Optional[PlatformInfo] = None,
        gate_controller: GateController = None,
        persisted_state: dict = None,
) -> OrchestratorResult:
    """通过主机函数调度执行编排器 Python 代码

    这是替代 `ExecutionLoop::run()` 内部循环的核心函数。
    编排器 Python 通过 Monty 的暂停机制调用主机函数，
    此函数通过委托给适当的 Python 实现来处理每次暂停
    """
    total_tokens = TokenUsage()
    final_result = None
    stdout = ""
    persisted_state = persisted_state or {}

    # 构建编排器的上下文变量
    input_names, input_values = build_orchestrator_inputs(thread, persisted_state)

    # 解析并编译编排器代码
    try:
        # orchestrator.py是标识，，用于错误信息和调试
        # 解析和编译整个 default.py 文件，但不执行任何代码
        runner = MontyRun(code, "orchestrator.py", input_names)
    except Exception as e:
        # 通过相同的类型化清理器路由解析失败，
        # 这样错误的 `default.py` 部署不会将 Monty 内部信息泄露到频道边缘
        raise EngineError(f"Orchestrator: {classify_orchestrator_failure('编排器解析错误', str(e))}")

    # 启动执行
    limits = orchestrator_limits()
    try:
        # 执行的 Python 代码：从 default.py 的第一行开始执行，直到遇到第一个 host function 调用或函数定义
        progress = runner.start(input_values, limits, stdout)
    except Exception as e:
        raise EngineError(f"Orchestrator: {classify_orchestrator_failure('编排器运行时错误', str(e))}")

    # 驱动编排器调度循环
    # 1. 检查信号
    #   1.1 `__check_signals__` —— 轮询停止/注入信号

    # 2. 检查预算
    #   2.1 `__check_budget__` —— 剩余令牌/时间/美元额度

    # 3. 在第一步(step=0)注入先验知识并激活技能
    #   3.1 `__retrieve_docs__` —— 查询记忆文档
    #   3.2 __list_skills__()                            -> 技能字典列表
    #   3.2 __set_active_skills__  设置激活的技能

    # 4. 调用llm
    #   4.1 __llm_complete__(messages, actions, config)  -> 响应字典

    # 5. 根据类型处理响应。
    while True:
        if isinstance(progress, RunComplete):
            # 如果设置了 FINAL 结果则使用，否则回退到 VM 返回值
            result = final_result if final_result is not None else progress.value
            sync_runtime_state(thread, result.get("state"))
            outcome = parse_outcome(result)
            sync_visible_outcome(thread, outcome)
            normalize_pause_outcome(thread, outcome)
            return OrchestratorResult(
                outcome=outcome,
                tokens_used=total_tokens,
            )

        elif isinstance(progress, RunFunctionCall):
            action_name = progress.function_name
            args = progress.args
            kwargs = progress.kwargs

            logger.debug(f"编排器: 主机函数调用 {action_name}")

            # 根据函数名分派到对应的处理函数
            if action_name == "FINAL":
                # FINAL(result) — 编排器返回其结果
                val = args[0] if args else {}
                final_result = val
                ext_result = ExtFunctionResult.Return(None)

            # Step1:
            elif action_name == "__check_signals__":
                ext_result = _handle_check_signals(signal_rx, thread)

            elif action_name == "__llm_complete__":
                ext_result = await _handle_llm_complete(
                    args, kwargs, thread, llm, effects, leases, store, platform_info, total_tokens,
                )

            elif action_name == "__execute_code_step__":
                ext_result = await _handle_execute_code_step(
                    args, kwargs, thread, llm, effects, leases, policy, event_tx, gate_controller,
                )

            elif action_name == "__execute_action__":
                ext_result = await _handle_execute_action(
                    args, kwargs, thread, effects, leases, policy, event_tx, gate_controller,
                )

            elif action_name == "__execute_actions_parallel__":
                ext_result = await _handle_execute_actions_parallel(
                    args, thread, effects, leases, policy, event_tx, gate_controller,
                )

            elif action_name == "__emit_event__":
                ext_result = _handle_emit_event(args, kwargs, thread, event_tx)

            elif action_name == "__save_checkpoint__":
                ext_result = _handle_save_checkpoint(args, kwargs, thread)

            elif action_name == "__transition_to__":
                ext_result = _handle_transition_to(args, kwargs, thread)

            elif action_name == "__retrieve_docs__":
                ext_result = await _handle_retrieve_docs(args, kwargs, thread, retrieval)

            elif action_name == "__check_budget__":
                ext_result = _handle_check_budget(thread)

            elif action_name == "__get_actions__":
                ext_result = await _handle_get_actions(thread, effects, leases, store)

            elif action_name == "__list_skills__":
                ext_result = await _handle_list_skills(args, thread, store)

            elif action_name == "__record_skill_usage__":
                ext_result = await _handle_record_skill_usage(args, store)

            elif action_name == "__regex_match__":
                # 使用 Python 的 re 模块评估正则表达式。
                # 无效模式静默返回 False。
                # Monty 没有 `re` 模块，因此此主机函数为技能选择器的
                # 基于模式的评分桥接了差距
                ext_result = _handle_regex_match(args)

            elif action_name == "__set_active_skills__":
                ext_result = _handle_set_active_skills(args, thread)

            else:
                # 未知 — 让 Monty 解析（用户定义的函数、内置函数）
                ext_result = ExtFunctionResult.NotFound(action_name)

            # 恢复编排器 VM
            try:
                progress = progress.resume(ext_result, stdout)
            except Exception as e:
                raise EngineError(f"Orchestrator: {classify_orchestrator_failure('恢复后编排器错误', str(e))}")

            # 如果调用了 FINAL，VM 应在下次迭代时完成
            if final_result is not None:
                continue

        elif isinstance(progress, RunNameLookup):
            # 未定义的变量 — 以 NameError 恢复
            name = progress.name
            logger.debug(f"编排器: 未解析的名称 {name}")
            try:
                progress = progress.resume(NameLookupResult.Undefined, stdout)
            except Exception as e:
                raise EngineError(f"Orchestrator: {classify_orchestrator_failure(f'编排器 NameError {name}', str(e))}")

        elif isinstance(progress, RunOsCall):
            raise EngineError("Effect: 编排器尝试了 OS 调用（已阻止）")

        elif isinstance(progress, RunResolveFutures):
            raise EngineError("Effect: 编排器尝试了异步操作（不支持）")


# ── 主机函数处理函数 ─────────────────────────────────────────

async def _handle_llm_complete(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
        llm: LlmBackend,
        effects: EffectExecutor,
        leases: LeaseManager,
        store: Optional[Store],
        platform_info: Optional[PlatformInfo],
        total_tokens: TokenUsage,
) -> ExtFunctionResult:
    """处理 `__llm_complete__(messages, actions, config)`

    调用 LLM 并将响应作为字典返回：
    `{type: "text"|"code"|"actions", content/code/calls: ..., usage: {...}}`
    """
    explicit_messages = args[0] if len(args) > 0 else None
    explicit_config = args[2] if len(args) > 2 else None

    messages = explicit_messages if explicit_messages is not None else thread.messages

    # 在 LLM 调用前协调动态工具租约
    try:
        await reconcile_dynamic_tool_lease(
            thread, effects, leases, store, LeasePlanner(),
        )
    except Exception as e:
        warn_on_lease_refresh_failure("llm_complete", e)

    active_leases = await leases.active_for_thread(thread.id)
    # 只读路径：`available_actions` 和下面的消息刷新不暂停；
    # 惰性控制器是正确的
    actions_context = thread_execution_context(
        thread, StepId(), None, CancellingGateController(),
    )
    actions = await effects.available_actions(active_leases, actions_context)
    if actions is None:
        actions = []

    await refresh_llm_messages_for_current_surface(
        messages, thread, effects, store, platform_info,
        active_leases, actions_context, actions,
    )

    # 构建 LLM 调用配置
    config = LlmCallConfig(
        max_tokens=explicit_config.get("max_tokens") if explicit_config else None,
        temperature=explicit_config.get("temperature") if explicit_config else None,
        force_text=explicit_config.get("force_text", False) if explicit_config else False,
        depth=thread.config.depth,
        model=explicit_config.get("model") if explicit_config else None,
        metadata={},
    )

    try:
        output = await llm.complete(messages, actions, config)
        total_tokens.input_tokens += output.usage.input_tokens
        total_tokens.output_tokens += output.usage.output_tokens
        total_tokens.cost_usd += output.usage.cost_usd

        usage = {
            "input_tokens": output.usage.input_tokens,
            "output_tokens": output.usage.output_tokens,
            "cost_usd": output.usage.cost_usd,
        }

        if isinstance(output.response, TextResponse):
            result = {"type": "text", "content": output.response.content, "usage": usage}
        elif isinstance(output.response, CodeResponse):
            result = {"type": "code", "code": output.response.code, "usage": usage}
        elif isinstance(output.response, ActionCallsResponse):
            calls_json = action_calls_to_python_json(output.response.calls)
            result = {
                "type": "actions",
                "content": output.response.content,
                "calls": calls_json,
                "usage": usage,
            }
        else:
            result = {"type": "text", "content": "", "usage": usage}

        return ExtFunctionResult.Return(result)
    except Exception as e:
        return ExtFunctionResult.Error(RuntimeError(f"LLM 调用失败: {e}"))


async def _handle_execute_code_step(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
        llm: LlmBackend,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        event_tx: Optional[asyncio.Queue],
        gate_controller: GateController,
) -> ExtFunctionResult:
    """处理 `__execute_code_step__(code, state)`"""
    # 实际实现取决于 CodeAct 执行引擎
    code = args[0] if len(args) > 0 else ""
    state = args[1] if len(args) > 1 else {}
    # ... 执行代码步骤的具体逻辑
    return ExtFunctionResult.Return({"status": "ok"})


async def _handle_execute_action(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        event_tx: Optional[asyncio.Queue],
        gate_controller: GateController,
) -> ExtFunctionResult:
    """处理 `__execute_action__(name, params, call_id=...)`"""
    action_name = args[0] if len(args) > 0 else ""
    params = args[1] if len(args) > 1 else {}
    call_id = kwargs.get("call_id", str(time.time()))
    # ... 执行单个动作的具体逻辑
    return ExtFunctionResult.Return({"status": "ok"})


async def _handle_execute_actions_parallel(
        args: List[Any],
        thread: Thread,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        event_tx: Optional[asyncio.Queue],
        gate_controller: GateController,
) -> ExtFunctionResult:
    """处理 `__execute_actions_parallel__(calls)`"""
    calls = args[0] if len(args) > 0 else []
    # ... 并行执行多个动作的具体逻辑
    return ExtFunctionResult.Return({"results": []})


def _handle_check_signals(
        signal_rx: SignalReceiver,
        thread: Thread,
) -> ExtFunctionResult:
    """处理 `__check_signals__()`"""
    # 检查取消/暂停信号
    try:
        signal = signal_rx.try_recv()
        if signal is not None:
            return ExtFunctionResult.Return({"signal": str(signal)})
    except Exception:
        pass
    return ExtFunctionResult.Return({"signal": None})


def _handle_emit_event(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
        event_tx: Optional[asyncio.Queue],
) -> ExtFunctionResult:
    """处理 `__emit_event__(kind, **data)`"""
    kind = args[0] if len(args) > 0 else "unknown"
    data = args[1] if len(args) > 1 else {}
    # ... 发出事件的具体逻辑
    return ExtFunctionResult.Return(None)


def _handle_save_checkpoint(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
) -> ExtFunctionResult:
    """处理 `__save_checkpoint__(state, counters)`"""
    state = args[0] if len(args) > 0 else {}
    # ... 保存检查点的具体逻辑
    return ExtFunctionResult.Return(None)


def _handle_transition_to(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
) -> ExtFunctionResult:
    """处理 `__transition_to__(state, reason)`"""
    new_state = args[0] if len(args) > 0 else None
    reason = args[1] if len(args) > 1 else None
    if new_state is not None:
        thread.transition_to(new_state, reason)
    return ExtFunctionResult.Return(None)


async def _handle_retrieve_docs(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
        retrieval: Optional[RetrievalEngine],
) -> ExtFunctionResult:
    """处理 `__retrieve_docs__(goal, max_docs)`"""
    goal = args[0] if len(args) > 0 else thread.goal
    max_docs = args[1] if len(args) > 1 else 5
    if retrieval is not None:
        docs = await retrieval.retrieve_context(thread.project_id, thread.user_id, goal, max_docs)
        return ExtFunctionResult.Return({"docs": docs})
    return ExtFunctionResult.Return({"docs": []})


def _handle_check_budget(thread: Thread) -> ExtFunctionResult:
    """处理 `__check_budget__()`"""
    config = thread.config
    # 检查 token 预算
    if config.max_tokens_total is not None and thread.total_tokens_used >= config.max_tokens_total:
        return ExtFunctionResult.Return({"exceeded": True, "reason": "超过 token 限制"})
    # 检查成本预算
    if config.max_budget_usd is not None and thread.total_cost_usd >= config.max_budget_usd:
        return ExtFunctionResult.Return({"exceeded": True, "reason": "超过预算限制"})
    return ExtFunctionResult.Return({"exceeded": False})


async def _handle_get_actions(
        thread: Thread,
        effects: EffectExecutor,
        leases: LeaseManager,
        store: Optional[Store],
) -> ExtFunctionResult:
    """处理 `__get_actions__()`"""
    active_leases = await leases.active_for_thread(thread.id)
    actions_context = thread_execution_context(thread, StepId(), None, CancellingGateController())
    actions = await effects.available_actions(active_leases, actions_context)
    return ExtFunctionResult.Return({"actions": actions if actions else []})


async def _handle_list_skills(
        args: List[Any],
        thread: Thread,
        store: Optional[Store],
) -> ExtFunctionResult:
    """处理 `__list_skills__(max_candidates, max_tokens)`"""
    # ... 列出技能的具体逻辑
    return ExtFunctionResult.Return({"skills": []})


async def _handle_record_skill_usage(
        args: List[Any],
        store: Optional[Store],
) -> ExtFunctionResult:
    """处理 `__record_skill_usage__(doc_id, success)`"""
    # ... 记录技能使用的具体逻辑
    return ExtFunctionResult.Return(None)


def _handle_regex_match(args: List[Any]) -> ExtFunctionResult:
    """处理 `__regex_match__(pattern, text) -> bool`

    使用 Python 的 re 模块评估正则表达式。
    无效模式静默返回 False
    """
    import re
    pattern = args[0] if len(args) > 0 else ""
    text = args[1] if len(args) > 1 else ""
    try:
        result = bool(re.search(pattern, text))
        return ExtFunctionResult.Return(result)
    except re.error:
        return ExtFunctionResult.Return(False)


def _handle_set_active_skills(
        args: List[Any],
        thread: Thread,
) -> ExtFunctionResult:
    """处理 `__set_active_skills__(skills)`"""
    skills = args[0] if len(args) > 0 else []
    thread.set_active_skills(skills)
    return ExtFunctionResult.Return(None)


# ── 常量 ─────────────────────────────────────────────────────

CODE_EXECUTED_MAX_BYTES = 8_000


# ── 辅助函数 ─────────────────────────────────────────────────

def _tail_utf8_bytes(text: str, max_bytes: int) -> str:
    """截断字符串到指定的字节数（UTF-8 编码）"""
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text
    # 从开头截取 max_bytes 字节，确保不截断多字节字符
    truncated = encoded[:max_bytes]
    # 解码时忽略不完整的尾部字符
    return truncated.decode('utf-8', errors='ignore')


def _tail_chars(text: str, max_chars: int) -> str:
    """截断字符串到指定的字符数"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _bounded_return_value(value: Any, max_bytes: int) -> Optional[str]:
    """将返回值限制在指定字节数内"""
    if value is None:
        return None
    json_str = json.dumps(value, ensure_ascii=False)
    return _tail_utf8_bytes(json_str, max_bytes)


def _code_hash(code: str) -> str:
    """计算代码的哈希值"""
    return hashlib.sha256(code.encode('utf-8')).hexdigest()[:16]


def _extract_string_arg(args: List[Any], kwargs: Dict[str, Any], name: str, index: int) -> Optional[str]:
    """从位置参数或关键字参数中提取字符串值"""
    if index < len(args) and isinstance(args[index], str):
        return args[index]
    return kwargs.get(name)


def _extract_string_kwarg(kwargs: Dict[str, Any], name: str) -> Optional[str]:
    """从关键字参数中提取字符串值"""
    val = kwargs.get(name)
    if isinstance(val, str):
        return val
    return None


# ── LLM 消息刷新 ─────────────────────────────────────────────

async def refresh_llm_messages_for_current_surface(
        messages: List[ThreadMessage],
        thread: Thread,
        effects: EffectExecutor,
        store: Optional[Store] = None,
        platform_info: Optional[PlatformInfo] = None,
        active_leases: List[CapabilityLease] = None,
        actions_context: ThreadExecutionContext = None,
        actions: List[ActionDef] = None,
) -> None:
    """为当前表面刷新 LLM 消息"""
    # 检查消息中是否已存在引擎拥有的系统提示
    has_system_prompt = False
    for message in messages:
        if message.role == MessageRole.System and is_codeact_system_prompt(message.content):
            has_system_prompt = True
            break

    if not has_system_prompt:
        return

    # 加载能力
    try:
        capabilities = await effects.available_capabilities(active_leases, actions_context)
    except Exception as error:
        logger.debug(f"线程 {thread.id}: 加载 llm_complete 提示刷新所需能力失败: {error}")
        capabilities = []

    # 构建系统提示
    system_prompt = await build_codeact_system_prompt(
        capabilities,
        actions if actions else [],
        store,
        thread.project_id,
        platform_info,
    )

    # 更新系统提示
    upsert_codeact_system_prompt(messages, system_prompt)


# ── 执行代码步骤 ─────────────────────────────────────────────

async def handle_execute_code_step(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
        llm: LlmBackend,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        event_tx: Optional[asyncio.Queue] = None,
        gate_controller: GateController = None,
) -> ExtFunctionResult:
    """处理 `__execute_code_step__(code, state)`

    在嵌套的 Monty VM 中运行用户 CodeAct 代码，具有完整的工具调度。
    返回包含 stdout、return_value、action_results 等的字典
    """
    code = args[0] if len(args) > 0 else None
    if code is None:
        return ExtFunctionResult.Error(TypeError("__execute_code_step__ 需要代码字符串"))

    state = args[1] if len(args) > 1 else {}

    exec_ctx = thread_execution_context(thread, StepId(), None, gate_controller)
    code_start = time.monotonic()

    try:
        result = await execute_code(
            code,
            thread,
            llm,
            effects,
            leases,
            policy,
            exec_ctx,
            [],
            state,
        )

        # 将代码执行的事件广播到线程和事件频道。
        # 没有这一步，CodeAct 工具调用的 ActionExecuted 事件会丢失，
        # 永远不会出现在追踪中
        for event_kind in result.events:
            event = ThreadEvent.new(thread.id, event_kind)
            if event_tx is not None:
                try:
                    event_tx.put_nowait(event)
                except asyncio.QueueFull:
                    pass
            thread.events.append(event)

        # 如果 CodeAct 代码片段本身失败（Python SyntaxError、运行时错误等），
        # 将其显示为 ActionFailed 事件，以便追踪和观察者看到失败。
        # 没有这一步，解析错误会通过结果字典静默回退到 LLM，永远不会警告调用者
        if result.failure is not None:
            error_msg = (
                f"CodeAct 执行失败: {_tail_chars(result.stdout, 500)}"
                if result.stdout
                else "CodeAct 执行失败（无 stdout）"
            )
            failed_event = ThreadEvent.new(
                thread.id,
                EventKind.ActionFailed(
                    step_id=exec_ctx.step_id,
                    action_name="__codeact__",
                    # 从步骤 ID 派生的合成 call_id —
                    # CodeAct 代码片段失败没有 LLM 提供的 call_id，
                    # 但 `loop_engine.rs:1277` 断言 ActionFailed 事件
                    # 携带非空的 call_id 用于追踪关联
                    call_id=f"codeact-step-{exec_ctx.step_id.value}",
                    error=error_msg,
                    duration_ms=int((time.monotonic() - code_start) * 1000),
                    params_summary=None,
                ),
            )
            if event_tx is not None:
                try:
                    event_tx.put_nowait(failed_event)
                except asyncio.QueueFull:
                    pass
            thread.events.append(failed_event)

            # 发出结构化的 CodeExecutionFailed 事件用于 instrumentation。
            # 这使得能够对代码执行失败的原因进行聚合分析
            # （Monty 限制 vs LLM 逻辑错误 vs 工具调度失败）
            error_text = _tail_chars(result.stdout, 500)
            instrumentation_event = ThreadEvent.new(
                thread.id,
                EventKind.CodeExecutionFailed(
                    step_id=exec_ctx.step_id,
                    category=result.failure,
                    error=error_text,
                    code_hash=_code_hash(code),
                    duration_ms=int((time.monotonic() - code_start) * 1000),
                ),
            )
            if event_tx is not None:
                try:
                    event_tx.put_nowait(instrumentation_event)
                except asyncio.QueueFull:
                    pass
            thread.events.append(instrumentation_event)

        # 始终发出 CodeExecuted 以便调试观察者看到确切的代码和 stdout，
        # 无论成功还是失败。上下文中的聊天摘要对于诊断来说过于有损。
        # 与 CodeExecutionFailed（携带失败分类器）和上面已广播的每个动作事件保持分离
        #
        # 在发出前将代码、stdout 和 return_value 分别限制在 `CODE_EXECUTED_MAX_BYTES`
        # 字节内，这样打印或返回大型负载的步骤不会使持久化的线程事件膨胀
        # 或淹没 SSE 广播缓冲区。基于字节（而非 `chars().count()`），
        # 即使对于非常大的负载也能保持 O(1)。此文件中其他地方使用的
        # `tail_chars` 保持不变，因为其调用者已在预先有界输入
        # （`OUTPUT_TRUNCATE_LEN`，500 字符错误切片）内运行，
        # 其中字符 vs 字节不是性能问题
        code_executed_event = ThreadEvent.new(
            thread.id,
            EventKind.CodeExecuted(
                step_id=exec_ctx.step_id,
                code=_tail_utf8_bytes(code, CODE_EXECUTED_MAX_BYTES),
                stdout=_tail_utf8_bytes(result.stdout, CODE_EXECUTED_MAX_BYTES),
                return_value=_bounded_return_value(result.return_value, CODE_EXECUTED_MAX_BYTES),
                duration_ms=int((time.monotonic() - code_start) * 1000),
            ),
        )
        if event_tx is not None:
            try:
                event_tx.put_nowait(code_executed_event)
            except asyncio.QueueFull:
                pass
        thread.events.append(code_executed_event)
        thread.updated_at = datetime.now(timezone.utc)

        # 构建动作结果列表
        action_results = [
            {
                "action_name": r.action_name,
                "output": r.output,
                "is_error": r.is_error,
                "duration_ms": r.duration_ms,
            }
            for r in result.action_results
        ]

        # 构建结果 JSON
        need_approval_json = None
        if result.need_approval is not None:
            na = result.need_approval
            if hasattr(na, 'gate_name'):
                need_approval_json = {
                    "gate_paused": True,
                    "gate_name": na.gate_name,
                    "action_name": na.action_name,
                    "call_id": na.call_id,
                    "parameters": na.parameters,
                    "resume_kind": na.resume_kind.to_dict() if hasattr(na.resume_kind, 'to_dict') else str(
                        na.resume_kind),
                    "resume_output": na.resume_output,
                    "paused_lease": na.paused_lease,
                }

        result_json = {
            "return_value": result.return_value,
            "stdout": result.stdout,
            "action_results": action_results,
            "final_answer": result.final_answer,
            "had_error": result.failure is not None,
            "pending_gate": need_approval_json,
        }

        return ExtFunctionResult.Return(result_json)

    except Exception as e:
        return ExtFunctionResult.Error(RuntimeError(f"代码执行失败: {e}"))


# ── 执行单个动作 ─────────────────────────────────────────────

async def handle_execute_action(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        event_tx: Optional[asyncio.Queue] = None,
        gate_controller: GateController = None,
) -> ExtFunctionResult:
    """处理 `__execute_action__(name, params, call_id=...)`

    动作执行的唯一真实来源。执行：
    1. 租约查找
    2. 策略检查
    3. 租约消耗
    4. 通过 EffectExecutor 执行动作
    5. 事件发出（ActionExecuted/ActionFailed）

    Python 拥有工作转录，并决定工具输出如何在内部消息历史中表示
    """
    name = _extract_string_arg(args, kwargs, "name", 0)
    if name is None:
        return ExtFunctionResult.Error(TypeError("__execute_action__ 需要名称参数"))

    params = args[1] if len(args) > 1 else {}
    call_id = _extract_string_kwarg(kwargs, "call_id") or ""

    exec_ctx = thread_execution_context(thread, StepId(), call_id, gate_controller)
    active_leases = await leases.active_for_thread(thread.id)

    # 加载动作清单
    inventory = None
    try:
        inventory = await effects.available_action_inventory(active_leases, exec_ctx)
    except Exception as error:
        logger.debug(f"线程 {thread.id}: 加载编排器动作执行的动作清单失败: action={name}, error={error}")

    available_actions = apply_snapshot_inventory(exec_ctx, inventory)

    # 辅助函数：仅发出事件。编排器拥有转录记录
    def emit_and_record(
            thread: Thread,
            event_tx: Optional[asyncio.Queue],
            event_kind: EventKind,
    ) -> None:
        event = ThreadEvent.new(thread.id, event_kind)
        if event_tx is not None:
            try:
                event_tx.put_nowait(event)
            except asyncio.QueueFull:
                pass
        thread.events.append(event)
        thread.updated_at = datetime.now(timezone.utc)

    # 1. 从可调用清单中查找动作定义
    action_def = None
    for a in available_actions:
        if a.matches_name(name):
            action_def = a
            break

    if exec_ctx.available_actions_snapshot is not None and action_def is None:
        error = f"动作 '{name}' 在此执行上下文中不可调用"
        output = {"error": error}
        emit_and_record(
            thread,
            event_tx,
            EventKind.ActionFailed(
                step_id=exec_ctx.step_id,
                action_name=name,
                call_id=call_id,
                error=error,
                duration_ms=0,
                params_summary=summarize_params(name, params),
            ),
        )
        return ExtFunctionResult.Return({"output": output, "is_error": True})

    # 2. 查找此动作的租约
    lease = await leases.find_lease_for_action(thread.id, name)
    if lease is None:
        error = f"动作 '{name}' 没有租约"
        output = {"error": error}
        emit_and_record(
            thread,
            event_tx,
            EventKind.ActionFailed(
                step_id=exec_ctx.step_id,
                action_name=name,
                call_id=call_id,
                error=error,
                duration_ms=0,
                params_summary=None,
            ),
        )
        return ExtFunctionResult.Return({"output": output, "is_error": True})

    canonical_name = action_def.name if action_def is not None else name

    # 策略检查
    if action_def is not None:
        decision = policy.evaluate(action_def, lease, [])
        if isinstance(decision, Deny):
            output = {"error": f"被拒绝: {decision.reason}"}
            emit_and_record(
                thread,
                event_tx,
                EventKind.ActionFailed(
                    step_id=exec_ctx.step_id,
                    action_name=name,
                    call_id=call_id,
                    error=decision.reason,
                    duration_ms=0,
                    params_summary=None,
                ),
            )
            return ExtFunctionResult.Return({"output": output, "is_error": True})
        elif isinstance(decision, RequireApproval):
            # 策略引发的批准的内联门控等待。
            # 镜像 `structured.rs::execute_action_batch_with_results`：
            # 发出请求，就地暂停执行器，批准后进入租约消耗 + 执行，
            # 拒绝时发出 ActionFailed 并显示拒绝风格的结果。
            # 此代码路径不再有 `gate_paused` 哨兵 + 线程重新进入
            emit_and_record(
                thread,
                event_tx,
                EventKind.ApprovalRequested(
                    action_name=name,
                    call_id=call_id,
                    parameters=params,
                    description=None,
                    allow_always=True,
                    gate_name="approval",
                    params_summary=summarize_params(name, params),
                ),
            )

            resume_kind = ResumeKind.Approval(allow_always=True)
            resolution = await gate_controller.pause(GatePauseRequest(
                thread_id=thread.id,
                user_id=thread.user_id,
                gate_name="approval",
                action_name=name,
                call_id=call_id,
                parameters=params,
                resume_kind=resume_kind,
                conversation_id=exec_ctx.conversation_id,
            ))

            denial = denial_outcome_for_resolution(resolution)
            if denial is not None:
                error = denial.event_error()
                output = {"error": error}
                emit_and_record(
                    thread,
                    event_tx,
                    EventKind.ActionFailed(
                        step_id=exec_ctx.step_id,
                        action_name=name,
                        call_id=call_id,
                        error=error,
                        duration_ms=0,
                        params_summary=summarize_params(name, params),
                    ),
                )
                return ExtFunctionResult.Return({"output": output, "is_error": True})
            # 已批准 — 进入租约消耗 + 执行。
            # 适配器的每次调用 ApprovalRequirement 门控（如果有）独立于策略门控，
            # 如果触发，将由下面的包装器内联处理

    # 3. 原子化地在单个写锁下重新查找 + 消耗一次租约使用。
    # 这关闭了只读 `find_lease_for_action`（上面用于策略检查）
    # 和消耗之间的 TOCTOU 窗口 — 没有它，两个并发调用可能都观察到
    # 剩余一次使用的租约并同时继续执行。
    # 镜像 `structured.rs::execute_action_batch_with_results`
    try:
        lease = await leases.find_and_consume(thread.id, name)
    except Exception as e:
        logger.debug(f"原子化租约 find_and_consume 失败: {e}")
        error = f"动作 '{name}' 的租约消耗失败: {e}"
        output = {"error": error}
        emit_and_record(
            thread,
            event_tx,
            EventKind.ActionFailed(
                step_id=exec_ctx.step_id,
                action_name=name,
                call_id=call_id,
                error=error,
                duration_ms=0,
                params_summary=None,
            ),
        )
        return ExtFunctionResult.Return({"output": output, "is_error": True})

    # 4. 通过内联等待包装器执行。工具引发的 `Err(GatePaused)`
    # 从 `effects.execute_action` 被适配器垫片转换为 `gate_paused` JSON 哨兵，
    # 然后由 `execute_single_action_with_inline_retry` 内联处理：
    # 暂停用户，批准时重试（有界），拒绝时显示拒绝风格的结果。
    # 此路径不再向 Python 返回 `gate_paused` 哨兵
    ps = summarize_params(canonical_name, params)
    result_json, events, _output, _final_lease_id = await execute_single_action_with_inline_retry(
        effects,
        leases,
        canonical_name,
        params,
        call_id,
        lease,
        exec_ctx,
        ps,
        thread.id,
        thread.user_id,
    )

    for event in events:
        emit_and_record(thread, event_tx, event)

    return ExtFunctionResult.Return(result_json)


async def handle_execute_actions_parallel(
        args: List[Any],
        thread: Thread,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        event_tx: Optional[asyncio.Queue] = None,
        gate_controller: GateController = None,
) -> ExtFunctionResult:
    """处理 `__execute_actions_parallel__(calls)`

    接收动作调用列表并并发执行它们的批量主机函数。
    每个调用是一个包含 `name`、`params` 和可选的 `call_id` 的字典

    返回结果字典列表（每个调用一个，按顺序）。
    每个结果与 `__execute_action__` 输出具有相同的形状，加上可选的门控暂停负载

    在所有并行执行完成后，事件按原始调用顺序发出
    """
    # 从第一个参数解析调用列表（字典列表）
    calls_json = args[0] if len(args) > 0 else []
    if not isinstance(calls_json, list):
        return ExtFunctionResult.Error(TypeError("__execute_actions_parallel__ 需要调用字典列表"))

    if not calls_json:
        return ExtFunctionResult.Return([])

    # 将每个调用字典解析为 (name, params, call_id)
    parsed = []
    for c in calls_json:
        name = c.get("name", "") if isinstance(c, dict) else ""
        params = c.get("params", {}) if isinstance(c, dict) else {}
        call_id = c.get("call_id", "") if isinstance(c, dict) else ""
        parsed.append({"name": name, "params": params, "call_id": call_id})

    step_id = StepId()
    actions_context = thread_execution_context(thread, step_id, None, gate_controller)
    active_leases = await leases.active_for_thread(thread.id)

    # 加载动作清单
    inventory = None
    try:
        inventory = await effects.available_action_inventory(active_leases, actions_context)
    except Exception as error:
        logger.debug(f"线程 {thread.id}, 步骤 {step_id}: 加载编排器并行执行的动作清单失败: {error}")

    available_actions = list(inventory.inline) if inventory is not None else []

    # ── 阶段 1：预检（顺序）─────────────────────────
    # 检查租约和策略。拒绝 → 错误结果。批准 → 中断

    preflight = []

    for pc in parsed:
        # 从可调用清单中查找动作定义
        exec_ctx = thread_execution_context(thread, step_id, pc["call_id"], gate_controller)
        if inventory is not None:
            exec_ctx.available_actions_snapshot = available_actions
            exec_ctx.available_action_inventory_snapshot = inventory

        action_def = None
        for a in available_actions:
            if a.matches_name(pc["name"]):
                action_def = a
                break

        if inventory is not None and action_def is None:
            error = f"动作 '{pc['name']}' 在此执行上下文中不可调用"
            output = {"error": error}
            result_json = {"output": output, "is_error": True}
            event = EventKind.ActionFailed(
                step_id=step_id,
                action_name=pc["name"],
                call_id=pc["call_id"],
                error=error,
                duration_ms=0,
                params_summary=summarize_params(pc["name"], pc["params"]),
            )
            preflight.append({"type": "error", "result_json": result_json, "event": event, "output": output})
            continue

        # 查找租约
        lease = await leases.find_lease_for_action(thread.id, pc["name"])
        if lease is None:
            error = f"动作 '{pc['name']}' 没有租约"
            output = {"error": error}
            result_json = {"output": output, "is_error": True}
            event = EventKind.ActionFailed(
                step_id=step_id,
                action_name=pc["name"],
                call_id=pc["call_id"],
                error=error,
                duration_ms=0,
                params_summary=None,
            )
            preflight.append({"type": "error", "result_json": result_json, "event": event, "output": output})
            continue

        # 检查策略
        action_name = action_def.name if action_def is not None else pc["name"]

        if action_def is not None:
            decision = policy.evaluate(action_def, lease, [])
            if isinstance(decision, Deny):
                output = {"error": f"被拒绝: {decision.reason}"}
                result_json = {"output": output, "is_error": True}
                event = EventKind.ActionFailed(
                    step_id=step_id,
                    action_name=action_name,
                    call_id=pc["call_id"],
                    error=decision.reason,
                    duration_ms=0,
                    params_summary=None,
                )
                preflight.append({"type": "error", "result_json": result_json, "event": event, "output": output})
                continue
            elif isinstance(decision, RequireApproval):
                # 内联门控等待：就地暂停此预检调用，直到用户解决门控。
                # 批准后，进入租约消耗 + 排队执行。
                # 拒绝后，推送 ActionFailed 结果并继续预检，
                # 以便批次其余部分仍然运行 —
                # 镜像 `structured.rs::execute_action_batch_with_results`
                #
                # 桥接控制器按 (user, thread) 序列化并发内联门控，
                # 因此两个同时门控的预检调用会顺序提示，
                # 而不是第二个静默取消
                approval_ev = ThreadEvent.new(
                    thread.id,
                    EventKind.ApprovalRequested(
                        action_name=pc["name"],
                        call_id=pc["call_id"],
                        parameters=pc["params"],
                        description=None,
                        allow_always=True,
                        gate_name="approval",
                        params_summary=summarize_params(pc["name"], pc["params"]),
                    ),
                )
                if event_tx is not None:
                    try:
                        event_tx.put_nowait(approval_ev)
                    except asyncio.QueueFull:
                        pass
                thread.events.append(approval_ev)
                thread.updated_at = datetime.now(timezone.utc)

                resume_kind = ResumeKind.Approval(allow_always=True)
                resolution = await gate_controller.pause(GatePauseRequest(
                    thread_id=thread.id,
                    user_id=thread.user_id,
                    gate_name="approval",
                    action_name=pc["name"],
                    call_id=pc["call_id"],
                    parameters=pc["params"],
                    resume_kind=resume_kind,
                    conversation_id=exec_ctx.conversation_id,
                ))

                denial = denial_outcome_for_resolution(resolution)
                if denial is not None:
                    error = denial.event_error()
                    output = {"error": error}
                    result_json = {"output": output, "is_error": True}
                    event = EventKind.ActionFailed(
                        step_id=step_id,
                        action_name=action_name,
                        call_id=pc["call_id"],
                        error=error,
                        duration_ms=0,
                        params_summary=summarize_params(pc["name"], pc["params"]),
                    )
                    preflight.append({"type": "error", "result_json": result_json, "event": event, "output": output})
                    continue
                # 已批准 — 进入租约消耗 + 可运行

        # 原子化地在单个写锁下重新查找 + 消耗一次租约使用，
        # 关闭上面只读 `find_lease_for_action` 和消耗之间的 TOCTOU 窗口。
        # 镜像 `structured.rs::execute_action_batch_with_results`
        try:
            lease = await leases.find_and_consume(thread.id, pc["name"])
        except Exception as e:
            logger.debug(f"原子化租约 find_and_consume 失败: {e}")
            error = f"动作 '{pc['name']}' 的租约消耗失败: {e}"
            output = {"error": error}
            result_json = {"output": output, "is_error": True}
            event = EventKind.ActionFailed(
                step_id=step_id,
                action_name=pc["name"],
                call_id=pc["call_id"],
                error=error,
                duration_ms=0,
                params_summary=None,
            )
            preflight.append({"type": "error", "result_json": result_json, "event": event, "output": output})
            continue

        preflight.append({"type": "runnable", "lease": lease})

    # ── 阶段 2：并行执行 ────────────────────────────

    # 槽数组：索引 → 执行结果。`slot_events` 是每个槽的 `List[EventKind]`，
    # 以便内联重试路径可以记录多个事件（ApprovalRequested + 重试后结果）
    slot_results = [None] * len(parsed)
    slot_events = [None] * len(parsed)
    slot_outputs = [None] * len(parsed)

    # 将可运行项与错误分开
    runnable = []
    for idx, pf in enumerate(preflight):
        if pf["type"] == "error":
            slot_results[idx] = pf["result_json"]
            slot_events[idx] = [pf["event"]]
            slot_outputs[idx] = pf["output"]
        elif pf["type"] == "runnable":
            runnable.append((idx, pf["lease"]))

    if len(runnable) == 1:
        # 单个调用：直接使用内联门控等待重试执行
        idx, lease = runnable[0]
        pc = parsed[idx]

        action_name = pc["name"]
        for a in available_actions:
            if a.matches_name(pc["name"]):
                action_name = a.name
                break

        exec_ctx = thread_execution_context(thread, step_id, pc["call_id"], gate_controller)
        if inventory is not None:
            exec_ctx.available_actions_snapshot = available_actions
            exec_ctx.available_action_inventory_snapshot = inventory

        ps = summarize_params(action_name, pc["params"])
        result_json, events, output, _final_lease_id = await execute_single_action_with_inline_retry(
            effects,
            leases,
            action_name,
            pc["params"],
            pc["call_id"],
            lease,
            exec_ctx,
            ps,
            thread.id,
            thread.user_id,
        )
        slot_results[idx] = result_json
        slot_events[idx] = events
        slot_outputs[idx] = output

    elif len(runnable) > 1:
        # 多个调用：通过 asyncio 任务并行执行。每个任务携带自己的内联重试循环，
        # 这样一个工具的门控不会阻塞批次其余部分 — 并且旧版的"恢复时双重执行"
        # 错误也不会在并行批次中触发
        effects_ref = effects
        leases_ref = leases
        base_exec_ctx = thread_execution_context(thread, step_id, None, gate_controller)
        thread_id = thread.id
        user_id = thread.user_id

        async def task(idx: int, lease: CapabilityLease):
            pc_name = parsed[idx]["name"]
            for a in available_actions:
                if a.matches_name(pc_name):
                    pc_name = a.name
                    break

            pc_params = parsed[idx]["params"]
            pc_call_id = parsed[idx]["call_id"]

            exec_ctx = base_exec_ctx.clone()
            exec_ctx.current_call_id = pc_call_id
            if inventory is not None:
                exec_ctx.available_actions_snapshot = available_actions
                exec_ctx.available_action_inventory_snapshot = inventory

            ps = summarize_params(pc_name, pc_params)
            result_json, events, output, final_lease_id = await execute_single_action_with_inline_retry(
                effects_ref,
                leases_ref,
                pc_name,
                pc_params,
                pc_call_id,
                lease,
                exec_ctx,
                ps,
                thread_id,
                user_id,
            )
            return (idx, final_lease_id, result_json, events, output)

        tasks = [task(idx, lease) for idx, lease in runnable]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        for result_item in results_list:
            if isinstance(result_item, Exception):
                logger.debug(f"并行动作执行任务异常: {result_item}")
                continue
            idx, _lease_id, result_json, events, output = result_item
            # 内联重试助手已经退还了门控等待期间消耗的任何租约。
            # 此处不需要额外的记账
            slot_results[idx] = result_json
            slot_events[idx] = events
            slot_outputs[idx] = output

    # ── 阶段 3：按顺序发出事件 ────────────────────────────

    results_json = []
    for idx in range(len(parsed)):
        result_json = slot_results[idx] or {"is_error": True, "output": {"error": "执行槽为空"}}
        output = slot_outputs[idx] or {"error": "无输出"}

        if slot_events[idx] is not None:
            for event in slot_events[idx]:
                ev = ThreadEvent.new(thread.id, event)
                if event_tx is not None:
                    try:
                        event_tx.put_nowait(ev)
                    except asyncio.QueueFull:
                        pass
                thread.events.append(ev)

        results_json.append(result_json)

    thread.updated_at = datetime.now(timezone.utc)
    return ExtFunctionResult.Return(results_json)


async def execute_single_action(
        effects: EffectExecutor,
        name: str,
        params: dict,
        call_id: str,
        lease: CapabilityLease,
        exec_ctx: ThreadExecutionContext,
        params_summary: Optional[str] = None,
) -> tuple:
    """执行单个动作并返回 (result_json, event, output) 供批量处理器记录。
    由单调用和并行路径共享
    """
    execution_start = time.monotonic()

    try:
        r = await effects.execute_action(name, params, lease, exec_ctx)

        # 将包装的错误显示为 ActionFailed（参见 resolve_tool_future 和
        # 并行执行路径中的相同模式）
        if r.is_error:
            error_msg = r.output.get("error", str(r.output)) if isinstance(r.output, dict) else str(r.output)
            duration_ms = r.duration_ms if r.duration_ms > 0 else int((time.monotonic() - execution_start) * 1000)
            event = EventKind.ActionFailed(
                step_id=exec_ctx.step_id,
                action_name=name,
                call_id=call_id,
                error=error_msg,
                duration_ms=duration_ms,
                params_summary=params_summary,
            )
        else:
            event = EventKind.ActionExecuted(
                step_id=exec_ctx.step_id,
                action_name=name,
                call_id=call_id,
                duration_ms=r.duration_ms,
                params_summary=params_summary,
            )

        result_json = {
            "action_name": r.action_name,
            "output": r.output,
            "is_error": r.is_error,
            "duration_ms": r.duration_ms,
        }
        return (result_json, event, r.output)

    except EngineError as e:
        if e.error_type == "GatePaused":
            gate_name = e.gate_name
            parameters = e.parameters
            resume_kind = e.resume_kind
            resume_output = e.resume_output
            paused_lease = e.paused_lease

            output = {"status": "gate_paused", "gate_name": gate_name}

            allow_always = None
            if hasattr(resume_kind, 'allow_always'):
                allow_always = resume_kind.allow_always

            event = EventKind.ApprovalRequested(
                action_name=name,
                call_id=call_id,
                parameters=parameters,
                description=None,
                allow_always=allow_always,
                gate_name=gate_name,
                params_summary=summarize_params(name, parameters),
            )

            resume_kind_dict = resume_kind.to_dict() if hasattr(resume_kind, 'to_dict') else str(resume_kind)

            result_json = {
                "gate_paused": True,
                "gate_name": gate_name,
                "action_name": name,
                "call_id": call_id,
                "parameters": parameters,
                "resume_kind": resume_kind_dict,
                "resume_output": resume_output,
                "paused_lease": paused_lease,
            }
            return (result_json, event, output)
        else:
            output = {"error": str(e)}
            event = EventKind.ActionFailed(
                step_id=exec_ctx.step_id,
                action_name=name,
                call_id=call_id,
                error=str(e),
                duration_ms=int((time.monotonic() - execution_start) * 1000),
                params_summary=params_summary,
            )
            result_json = {"output": output, "is_error": True}
            return (result_json, event, output)


def interrupted_result_needs_refund(result: dict) -> bool:
    """检查中断的结果是否需要退还租约使用次数"""
    return result.get("gate_paused") is True


# 内联门控重试的最大次数，防止行为异常的工具占用 CPU
MAX_INLINE_GATE_RETRIES = 3


async def execute_single_action_with_inline_retry(
        effects: EffectExecutor,
        leases: LeaseManager,
        name: str,
        params: dict,
        call_id: str,
        initial_lease: CapabilityLease,
        exec_ctx: ThreadExecutionContext,
        params_summary: Optional[str] = None,
        thread_id: ThreadId = None,
        user_id: str = "",
) -> tuple:
    """类似于 [`execute_single_action`]，但在 `Approval` 类型的门控暂停结果上
    内联暂停并重试。受 `MAX_INLINE_GATE_RETRIES` 限制，
    因此行为异常的工具不会占用 CPU

    由 `__execute_actions_parallel__` 用于单可运行和多可运行分支。
    没有此包装器，多可运行分支会回退到旧版 `gate_paused` 哨兵 + 线程重新进入，
    这会双重执行同一批次中较早的非幂等调用 — 正是此 PR 旨在防止的错误
    """
    current_lease = initial_lease
    call_ctx = exec_ctx.clone()
    # `accumulated_events` 携带内联重试循环观察到的每个事件 —
    # 来自每次门控暂停迭代的 `ApprovalRequested`，加上最终的
    # `ActionExecuted` / `ActionFailed`。调用者将它们全部追加到
    # 线程事件日志中，以便观察者看到完整序列
    accumulated_events = []

    for _ in range(MAX_INLINE_GATE_RETRIES):
        result_json, event, output = await execute_single_action(
            effects,
            name,
            params,
            call_id,
            current_lease,
            call_ctx,
            params_summary,
        )
        # 重置一次性批准标志 — 只有紧随批准的调用才应携带它
        call_ctx.call_approval_granted = False

        if not interrupted_result_needs_refund(result_json):
            # 不是门控暂停 — 终端事件；记录并返回
            accumulated_events.append(event)
            return (result_json, accumulated_events, output, current_lease.id)

        # 门控暂停。Approval 和 Authentication 获得内联等待处理
        # （#3133 / #3166）：主机控制器就地解决它们，暂停的调用重试，
        # 编排器在不展开的情况下继续。External 保留旧版 `gate_paused`
        # 哨兵 + 重新进入路径，因为其解决方案负载（回调正文）无法
        # 传回给暂停的调用
        resume_kind = result_json.get("resume_kind", {"Approval": {"allow_always": False}})
        if isinstance(resume_kind, dict):
            if "Approval" in resume_kind:
                resume_kind = ResumeKind.Approval(allow_always=resume_kind["Approval"].get("allow_always", False))
            elif "Authentication" in resume_kind:
                resume_kind = ResumeKind.Authentication()
            else:
                # External 或其他 — 不内联处理
                accumulated_events.append(event)
                return (result_json, accumulated_events, output, current_lease.id)
        else:
            accumulated_events.append(event)
            return (result_json, accumulated_events, output, current_lease.id)

        # 批准门控触发 — 在暂停控制器之前记录请求，
        # 以便观察者无论解决方案如何落地都能看到提示
        accumulated_events.append(event)

        # 退还此尝试消耗的租约使用次数；如果用户批准，我们将在重试时重新消耗。
        # 例外：当门控携带缓存的 `resume_output` 时，动作已经执行
        # （执行后认证门控），下面的缓存输出分支将返回而不重新消耗。
        # 现在退还将让成功的副作用动作消耗零次租约使用。
        # 参见 `scripting::resolve_tool_future` 和
        # `structured::execute_with_inline_gate_retry` 中的匹配保护。
        # 由 #3559 安全审查跟踪
        gate_carries_resume_output = (
                "resume_output" in result_json
                and result_json["resume_output"] is not None
        )
        if not gate_carries_resume_output:
            await leases.refund_use(current_lease.id)

        # 使用来自 GatePaused 负载的门控提供的参数，而不是原始调用者 `params`：
        # 安全层可能已转换/编辑它们，用户看到的提示必须与工具实际想要运行的内容匹配。
        # 镜像 `structured::execute_with_inline_gate_retry` 中的契约
        gate_parameters = result_json.get("parameters", params)

        resolution = await exec_ctx.gate_controller.pause(GatePauseRequest(
            thread_id=thread_id,
            user_id=user_id,
            gate_name=result_json.get("gate_name", "approval"),
            action_name=name,
            call_id=call_id,
            parameters=gate_parameters,
            resume_kind=resume_kind,
            conversation_id=exec_ctx.conversation_id,
        ))

        denial = denial_outcome_for_resolution(resolution)
        if denial is not None:
            # 取消+认证 → 回退到旧版 `gate_paused` 哨兵，
            # 以便任务/非内联感知控制器仍然可以显示暂停状态。
            # 参见 `structured::execute_with_inline_gate_retry` 中的匹配分支。
            # 已累积的 `ApprovalRequested` 事件在暂停前已推送；
            # 我们在携带原始门控元数据的新 result_json 上重新发出它
            if (isinstance(resolution, GateResolution)
                    and resolution.type == "Cancelled"
                    and isinstance(resume_kind, ResumeKind)
                    and resume_kind.type == "Authentication"):
                return (result_json, accumulated_events, output, current_lease.id)

            error_msg = denial.event_error()
            denial_output = {"error": error_msg}
            denial_event = EventKind.ActionFailed(
                step_id=exec_ctx.step_id,
                action_name=name,
                call_id=call_id,
                error=error_msg,
                duration_ms=0,
                params_summary=params_summary,
            )
            accumulated_events.append(denial_event)
            result_json = {
                "action_name": name,
                "output": denial_output,
                "is_error": True,
                "duration_ms": 0,
            }
            return (result_json, accumulated_events, denial_output, current_lease.id)

        # 已批准。如果桥接在引发此门控之前缓存了动作的输出
        # （执行后认证门控路径 — 参见 `effect_adapter::auth_gate_from_extension_result`
        # 和 `check_tool_readiness` 路径），动作已经运行，我们只需要用户侧解决方案。
        # 返回缓存的输出而不是重新执行。没有此快捷方式，重试 `tool_install`
        # 会重新下载 WASM 并第二次运行 `effect_adapter::enforce_tool_permission` 批准检查，
        # 引发用户无法解决的新门控。由 #3533 跟踪
        cached_output = result_json.get("resume_output")
        if cached_output is not None:
            event = EventKind.ActionExecuted(
                step_id=exec_ctx.step_id,
                action_name=name,
                call_id=call_id,
                duration_ms=0,
                params_summary=params_summary,
            )
            accumulated_events.append(event)
            result_json = {
                "action_name": name,
                "output": cached_output,
                "is_error": False,
                "duration_ms": 0,
            }
            return (result_json, accumulated_events, cached_output, current_lease.id)

        # 重新消耗一次租约使用并标记下一次调用为预批准
        try:
            new_lease = await leases.find_and_consume(thread_id, name)
            current_lease = new_lease
            call_ctx.call_approval_granted = True
            continue
        except Exception as e:
            err = {"error": f"批准后租约耗尽: {e}"}
            lease_event = EventKind.ActionFailed(
                step_id=exec_ctx.step_id,
                action_name=name,
                call_id=call_id,
                error=f"批准后租约耗尽: {e}",
                duration_ms=0,
                params_summary=params_summary,
            )
            accumulated_events.append(lease_event)
            result_json = {
                "action_name": name,
                "output": err,
                "is_error": True,
                "duration_ms": 0,
            }
            return (result_json, accumulated_events, err, current_lease.id)

    # 重试预算耗尽 — 工具在每次批准后持续门控。
    # 最后一次循环迭代以成功的 `find_and_consume` 结束，其租约从未被使用；
    # 在返回之前退还它，这样行为异常的工具无法在多次批准中缓慢耗尽 `max_uses`。
    # 尽力而为；如果租约已被撤销/过期，退款是无操作的
    await leases.refund_use(current_lease.id)
    err = {
        "error": f"工具 '{name}' 在 {MAX_INLINE_GATE_RETRIES} 次重试后仍然需要批准"
    }
    accumulated_events.append(EventKind.ActionFailed(
        step_id=exec_ctx.step_id,
        action_name=name,
        call_id=call_id,
        error=f"工具在 {MAX_INLINE_GATE_RETRIES} 次批准后持续门控",
        duration_ms=0,
        params_summary=params_summary,
    ))
    result_json = {
        "action_name": name,
        "output": err,
        "is_error": True,
        "duration_ms": 0,
    }
    return (result_json, accumulated_events, err, current_lease.id)


def handle_check_signals(
        signal_rx: SignalReceiver,
        thread: Thread,
) -> ExtFunctionResult:
    """处理 `__check_signals__()`"""
    try:
        signal = signal_rx.try_recv()
    except Exception:
        return ExtFunctionResult.Return(None)

    if signal is None:
        return ExtFunctionResult.Return(None)

    if signal.type in ("Stop", "Suspend"):
        return ExtFunctionResult.Return("stop")
    elif signal.type == "InjectMessage":
        thread.add_message(signal.message)
        return ExtFunctionResult.Return({"inject": signal.message.content})
    elif signal.type in ("Resume", "ChildCompleted"):
        return ExtFunctionResult.Return(None)
    else:
        return ExtFunctionResult.Return(None)


def handle_emit_event(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
        event_tx: Optional[asyncio.Queue] = None,
) -> ExtFunctionResult:
    """处理 `__emit_event__(kind, **data)`"""
    kind_str = args[0] if len(args) > 0 else ""

    if kind_str == "step_started":
        step = kwargs.get("step", 0)
        kind = EventKind.StepStarted(step_id=StepId())
    elif kind_str == "step_completed":
        input_tokens = kwargs.get("input_tokens", 0)
        output_tokens = kwargs.get("output_tokens", 0)
        # 增加步骤计数（镜像旧的 Rust 循环的 step_count += 1）
        thread.step_count += 1
        # 跟踪 token 使用量
        thread.total_tokens_used += input_tokens + output_tokens
        kind = EventKind.StepCompleted(
            step_id=StepId(),
            tokens=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        )
    elif kind_str == "action_executed":
        action_name = kwargs.get("action_name", "")
        call_id = kwargs.get("call_id", "")
        kind = EventKind.ActionExecuted(
            step_id=StepId(),
            action_name=action_name,
            call_id=call_id,
            duration_ms=0,
            params_summary=None,
        )
    elif kind_str == "action_failed":
        action_name = kwargs.get("action_name", "")
        call_id = kwargs.get("call_id", "")
        error = kwargs.get("error", "")
        duration_ms = kwargs.get("duration_ms", 0)
        kind = EventKind.ActionFailed(
            step_id=StepId(),
            action_name=action_name,
            call_id=call_id,
            error=error,
            duration_ms=duration_ms,
            params_summary=None,
        )
    elif kind_str == "skill_activated":
        names_str = kwargs.get("skill_names", "")
        skill_names = [s.strip() for s in names_str.split(",") if s.strip()]
        kind = EventKind.SkillActivated(skill_names=skill_names)
    else:
        logger.debug(f"编排器: 未知事件类型 '{kind_str}'，跳过")
        return ExtFunctionResult.Return(None)

    event = ThreadEvent.new(thread.id, kind)
    if event_tx is not None:
        try:
            event_tx.put_nowait(event)
        except asyncio.QueueFull:
            pass
    thread.events.append(event)
    thread.updated_at = datetime.now(timezone.utc)

    return ExtFunctionResult.Return(None)


def handle_save_checkpoint(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
) -> ExtFunctionResult:
    """处理 `__save_checkpoint__(state, counters)`"""
    state = args[0] if len(args) > 0 else {}
    counters = args[1] if len(args) > 1 else {}

    sync_runtime_state(thread, state)

    if isinstance(thread.metadata, dict):
        thread.metadata["runtime_checkpoint"] = {
            "persisted_state": state,
            "nudge_count": counters.get("nudge_count", 0),
            "consecutive_errors": counters.get("consecutive_errors", 0),
            "consecutive_action_errors": counters.get("consecutive_action_errors", 0),
            "compaction_count": counters.get("compaction_count", 0),
        }
    thread.updated_at = datetime.now(timezone.utc)

    return ExtFunctionResult.Return(None)


def handle_transition_to(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
) -> ExtFunctionResult:
    """处理 `__transition_to__(state, reason)`"""
    state_str = args[0] if len(args) > 0 else ""
    reason = args[1] if len(args) > 1 else None

    state_map = {
        "running": ThreadState.Running,
        "completed": ThreadState.Completed,
        "failed": ThreadState.Failed,
        "waiting": ThreadState.Waiting,
        "suspended": ThreadState.Suspended,
    }

    target = state_map.get(state_str)
    if target is None:
        return ExtFunctionResult.Error(ValueError(f"未知的线程状态: {state_str}"))

    try:
        thread.transition_to(target, reason)
        return ExtFunctionResult.Return(None)
    except Exception as e:
        return ExtFunctionResult.Error(RuntimeError(f"状态转换失败: {e}"))


async def handle_retrieve_docs(
        args: List[Any],
        kwargs: Dict[str, Any],
        thread: Thread,
        retrieval: Optional[RetrievalEngine] = None,
) -> ExtFunctionResult:
    """处理 `__retrieve_docs__(goal, max_docs)`"""
    if retrieval is None:
        return ExtFunctionResult.Return([])

    goal = args[0] if len(args) > 0 else ""
    max_docs = args[1] if len(args) > 1 else 5
    if not isinstance(max_docs, int):
        max_docs = 5

    try:
        docs = await retrieval.retrieve_context(
            thread.project_id, thread.user_id, goal, max_docs,
        )
        docs_json = [
            {
                "type": str(doc.doc_type),
                "title": doc.title,
                "content": doc.content,
            }
            for doc in docs
        ]
        return ExtFunctionResult.Return(docs_json)
    except Exception as e:
        logger.debug(f"retrieve_docs 失败: {e}")
        return ExtFunctionResult.Return([])


def handle_check_budget(thread: Thread) -> ExtFunctionResult:
    """处理 `__check_budget__()`"""
    # 计算剩余 token
    if thread.config.max_tokens_total is not None:
        tokens_remaining = max(0, thread.config.max_tokens_total - thread.total_tokens_used)
    else:
        tokens_remaining = None  # 表示无限制

    # 计算剩余时间
    if thread.config.max_duration is not None:
        elapsed = datetime.now(timezone.utc) - thread.created_at
        elapsed_ms = max(0, int(elapsed.total_seconds() * 1000))
        max_duration_ms = int(thread.config.max_duration.total_seconds() * 1000)
        time_remaining_ms = max(0, max_duration_ms - elapsed_ms)
    else:
        time_remaining_ms = None

    # 计算剩余美元预算
    if thread.config.max_budget_usd is not None:
        usd_remaining = max(0.0, thread.config.max_budget_usd - thread.total_cost_usd)
    else:
        usd_remaining = None

    result = {
        "tokens_remaining": tokens_remaining,
        "time_remaining_ms": time_remaining_ms,
        "usd_remaining": usd_remaining,
    }

    return ExtFunctionResult.Return(result)


async def handle_get_actions(
        thread: Thread,
        effects: EffectExecutor,
        leases: LeaseManager,
        store: Optional[Store] = None,
) -> ExtFunctionResult:
    """处理 `__get_actions__()`"""
    # 在获取动作前协调动态工具租约
    try:
        await reconcile_dynamic_tool_lease(thread, effects, leases, store, LeasePlanner())
    except Exception as e:
        warn_on_lease_refresh_failure("get_actions", e)

    active_leases = await leases.active_for_thread(thread.id)
    # 只读路径：`available_actions` 不暂停，因此惰性控制器是正确的。
    # 在此处传入实时控制器不会带来任何好处
    actions_context = thread_execution_context(
        thread, StepId(), None, CancellingGateController(),
    )

    try:
        actions = await effects.available_actions(active_leases, actions_context)
        actions_json = [
            {
                "name": a.name,
                "description": a.description,
                "params": a.parameters_schema,
            }
            for a in actions
        ]
        return ExtFunctionResult.Return(actions_json)
    except Exception as e:
        logger.debug(f"get_actions 失败: {e}")
        return ExtFunctionResult.Return([])


# 编译正则表达式的最大大小限制（64 KiB），用于防止 ReDoS 攻击
MAX_REGEX_SIZE = 1 << 16  # 65536 字节


async def handle_list_skills(
        args: List[Any],
        thread: Thread,
        store: Optional[Store] = None,
) -> ExtFunctionResult:
    """处理 `__list_skills__()`

    从项目中加载所有 `DocType::Skill` MemoryDoc 并将其作为 Python 字典列表返回。
    Python 编排器处理评分、选择和注入 — Rust 仅提供数据访问

    ## 设置标记排除（与 v1 选择器的 v2 对等）

    在返回技能列表之前，此函数过滤掉其 `metadata.activation.setup_marker`
    已在当前项目中作为 MemoryDoc 标题存在的任何技能。在 v2 中，工作区文件
    存储为按标题索引的 MemoryDoc，因此"标记文件是否存在"映射到
    "是否存在具有该标题的 MemoryDoc" — 并且我们已经在作用域中拥有
    完整的文档列表用于技能过滤，因此这不会产生额外的存储调用成本

    这是 v1 路径上通过 `ironclaw_skills::prefilter_skills` 传递的
    `satisfied_setup_markers` 参数的 v2 等效项。两条路径实现相同的规则：
    一次性设置技能在其标记文件已被写入后已完成其工作，
    不应在随后的每个回合中持续消耗激活预算
    """
    if store is None:
        return ExtFunctionResult.Return([])

    # 用户在其项目中的文档（所有文档类型 — 技能过滤在下面的
    # `filter(|d| d.doc_type == Skill)` 过程中进行）
    try:
        docs = await store.list_memory_docs(thread.project_id, thread.user_id)
    except Exception as e:
        logger.debug(f"__list_skills__: 加载用户文档失败: {e}")
        docs = []

    # 跨所有项目的管理员/共享技能（修复多租户可见性 —
    # 共享技能存在于所有者的项目中，但必须对所有用户可见，
    # 无论其线程在哪个按用户划分的项目中运行）
    try:
        shared = await store.list_skills_global()
        docs.extend(shared)
    except Exception as e:
        logger.debug(f"__list_skills__: 加载全局技能失败: {e}")

    # 按 ID 排序并去重
    seen_ids = set()
    unique_docs = []
    for doc in sorted(docs, key=lambda d: d.id.value if hasattr(d.id, 'value') else str(d.id)):
        doc_id = d.id.value if hasattr(d.id, 'value') else str(d.id)
        if doc_id not in seen_ids:
            seen_ids.add(doc_id)
            unique_docs.append(doc)
    docs = unique_docs

    # 构建现有非技能文档标题的集合（== v2 中的工作区路径），
    # 这样下面的设置标记过滤对每个技能是 O(1) 的。
    # 排除 Skill 文档，以便像 "github" 这样的标记不会与同名的技能文档冲突
    existing_titles = set()
    for doc in docs:
        if doc.doc_type != DocType.Skill:
            existing_titles.add(doc.title)

    # 过滤技能文档
    skills = []
    for doc in docs:
        if doc.doc_type != DocType.Skill:
            continue

        # 设置标记排除。如果技能的激活元数据声明了一个 setup_marker，
        # 且已存在具有该标题的 MemoryDoc，则该技能的设置已完成，我们跳过它
        metadata = doc.metadata if isinstance(doc.metadata, dict) else {}
        activation = metadata.get("activation", {}) if isinstance(metadata, dict) else {}
        marker = activation.get("setup_marker") if isinstance(activation, dict) else None

        if marker is not None and marker in existing_titles:
            logger.debug(f"__list_skills__: 排除设置技能 — 标记已存在: skill={doc.title}, marker={marker}")
            continue

        skills.append({
            "doc_id": str(doc.id.value) if hasattr(doc.id, 'value') else str(doc.id),
            "title": doc.title,
            "content": doc.content,
            "metadata": doc.metadata,
        })

    return ExtFunctionResult.Return(skills)


async def handle_record_skill_usage(
        args: List[Any],
        store: Optional[Store] = None,
) -> ExtFunctionResult:
    """处理 `__record_skill_usage__(doc_id, success)`

    记录在此线程中使用了某个技能。由 Python 编排器在技能辅助执行完成后调用
    """
    if store is None:
        return ExtFunctionResult.Return(None)

    doc_id_str = args[0] if len(args) > 0 else ""
    success = args[1] if len(args) > 1 else False
    if not isinstance(success, bool):
        success = False

    try:
        doc_uuid = uuid.UUID(doc_id_str)
    except (ValueError, AttributeError):
        logger.debug(f"__record_skill_usage__: 无效的 doc_id: {doc_id_str}")
        return ExtFunctionResult.Return(None)

    tracker = SkillTracker(store)
    try:
        await tracker.record_usage(DocId(doc_uuid), success)
    except Exception as e:
        logger.debug(f"__record_skill_usage__: 失败: {e}")

    return ExtFunctionResult.Return(None)


def handle_regex_match(args: List[Any]) -> ExtFunctionResult:
    """处理 `__regex_match__(pattern, text) -> bool`

    以有界大小限制编译 `pattern`，并返回它是否匹配 `text` 中的任何位置。
    无效的正则表达式或大小限制违规静默返回 `False`。
    由 Python 技能选择器用于正则表达式模式评分（Monty 没有 `re` 模块）

    **安全：ReDoS 安全。** 此处理程序接受来自 Python 编排器（其自身从技能清单接收）
    的任意模式，并在用户提供的文本上运行它们。安全性依赖于 Python `re` 模块的
    线性时间匹配保证（无反向引用，无环视）加上下面的编译大小上限。
    如果 `re` 模块被替换为支持反向引用且不是线性时间的 `regex` 模块，
    这将成为真正的 ReDoS 向量。这仅通过约定和文档强制执行
    """
    pattern = args[0] if len(args) > 0 else ""
    text = args[1] if len(args) > 1 else ""

    if not pattern:
        return ExtFunctionResult.Return(False)

    # 限制编译正则表达式的大小以防止 ReDoS
    # （匹配 `ironclaw_skills` 中 `LoadedSkill::compile_patterns` 使用的 64 KiB 限制）。
    # 同时限制模式长度作为额外的防御层
    if len(pattern) > MAX_REGEX_SIZE:
        logger.debug(f"__regex_match__: 模式超过大小限制 ({len(pattern)} > {MAX_REGEX_SIZE})")
        return ExtFunctionResult.Return(False)

    try:
        compiled = re.compile(pattern)
        matched = bool(compiled.search(text))
        return ExtFunctionResult.Return(matched)
    except re.error as e:
        logger.debug(f"__regex_match__: 无效模式 '{pattern}': {e}")
        return ExtFunctionResult.Return(False)


def handle_set_active_skills(
        args: List[Any],
        thread: Thread,
) -> ExtFunctionResult:
    """处理 `__set_active_skills__(skills)`

    将选定的技能溯源持久化到线程上，以便运行后学习流程
    可以推理出活跃的确切技能版本和代码片段
    """
    skills_json = args[0] if len(args) > 0 else []

    # 将 JSON 数据转换为 ActiveSkillProvenance 对象列表
    try:
        if isinstance(skills_json, list):
            skills = []
            for skill_data in skills_json:
                if isinstance(skill_data, dict):
                    skills.append(ActiveSkillProvenance(
                        doc_id=DocId(skill_data.get("doc_id", "")),
                        name=skill_data.get("name", ""),
                        version=skill_data.get("version", 0),
                        snippet_names=skill_data.get("snippet_names", []),
                        force_activated=skill_data.get("force_activated", False),
                    ))
        else:
            skills = []
    except Exception as e:
        logger.debug(f"__set_active_skills__: 无效负载: {e}")
        return ExtFunctionResult.Return(None)

    try:
        thread.set_active_skills(skills)
    except Exception as e:
        logger.debug(f"__set_active_skills__: 持久化活跃技能失败: {e}")

    return ExtFunctionResult.Return(None)


# ── 辅助函数 ─────────────────────────────────────────────────

def build_orchestrator_inputs(
        thread: Thread,
        persisted_state: dict,
) -> tuple:
    """构建注入到编排器 Python 中的上下文变量"""
    names = ["context", "goal", "actions", "state", "config"]

    # 构建编排器引导上下文。当存在内部执行转录时优先使用它，
    # 否则回退到用户可见的转录
    bootstrap_messages = (
        thread.internal_messages if thread.internal_messages
        else thread.messages
    )

    context = []
    for m in bootstrap_messages:
        # 通过 Python 交换形状（`{name, call_id, params}`）序列化 action_calls，
        # 以便引导上下文与 `python_json_to_action_calls` 往返兼容。
        # 在此处使用裸 `m.action_calls` 会产生规范的 Rust serde 格式
        # （`{action_name, id, parameters}`），Python 编排器会在下一次
        # `__llm_complete__` 调用中原样传回 — 然后 `python_json_to_action_calls`
        # 会因"缺少字段 `name`"而失败，使每个后续工具结果成为孤儿。
        # 这是将 action_calls 输入 Python 工作转录的第二个代码路径
        # （在 `handle_llm_complete` 之后）；两者必须使用相同的形状
        calls_json = None
        if hasattr(m, 'action_calls') and m.action_calls:
            calls_json = action_calls_to_python_json(m.action_calls)

        context.append({
            "role": str(m.role),
            "content": m.content,
            "action_name": m.action_name if hasattr(m, 'action_name') else None,
            "action_call_id": m.action_call_id if hasattr(m, 'action_call_id') else None,
            "action_calls": calls_json,
        })

    # 构建配置
    config = {
        # 最大迭代次数: 限制 LLM 调用的总次数，防止无限循环
        "max_iterations": thread.config.max_iterations,
        # 当前步数
        "step_count": thread.step_count,
        # 最大工具意图提示次数: 限制提示消息的注入次数，避免无限循环
        "max_tool_intent_nudges": thread.config.max_tool_intent_nudges,
        # 是否启用工具意图提示: 当 LLM 说"我会搜索"但没有实际调用工具时，自动注入提示消息
        "enable_tool_intent_nudge": thread.config.enable_tool_intent_nudge,
        # 是否要求尝试执行动作: 强制 LLM 必须调用工具，不能只返回文本
        "require_action_attempt": thread.config.require_action_attempt,
        # 最大动作要求提示次数: 限制"请调用工具"提示的注入次数
        "max_action_requirement_nudges": thread.config.max_action_requirement_nudges,
        # 最大连续错误次数: 连续出现指定次数错误后终止线程
        "max_consecutive_errors": thread.config.max_consecutive_errors,
        # 最大总 token 数: 限制整个线程使用的 token 总量（输入+输出）
        "max_tokens_total": thread.config.max_tokens_total,
        # 最大预算: 限制整个线程的 USD 成本
        "max_budget_usd": thread.config.max_budget_usd,
        # 模型上下文限制: LLM 模型的最大上下文窗口大小，用于触发压缩
        "model_context_limit": thread.config.model_context_limit,
        # 是否启用压缩: 当上下文超过阈值时自动压缩历史消息
        "enable_compaction": thread.config.enable_compaction,
        # 压缩阈值: 当上下文达到模型限制的 85% 时触发压缩
        "compaction_threshold": thread.config.compaction_threshold,
        # 当前递归深度: 当前线程在递归调用树中的深度（0 为根线程）
        "depth": thread.config.depth,
        # 最大递归深度: 限制 rlm_query() 的最大递归深度，防止无限递归
        "max_depth": thread.config.max_depth,
    }

    values = [
        context,
        thread.goal,
        [],  # 动作通过 __get_actions__ 动态加载
        persisted_state if persisted_state else {},
        config,
    ]

    return (names, values)


# ── Python ActionCall 交换格式 ────────────────────────────────

def action_calls_to_python_json(calls: List[ActionCall]) -> List[dict]:
    """将 `ActionCall` 列表序列化为 Python 交换形状

    序列化失败时（对于 String + String + Value 基本上不可能发生，
    但如果 `parameters` 树中包含序列化失败的键，仍然是可能的），
    该条目会被从输出中**丢弃**而不是替换为 `null`。
    之前的 `unwrap_or_else(|_| null)` 会损坏数组 — Python 的
    `default.py` 对每个条目访问 `c.get("name")` / `c.get("call_id")` /
    `c.get("params")`，因此 `null` 会因 Python `AttributeError` 崩溃
    并丢失整个 LLM 步骤。`filter_map` 产生更短的数组，Python 的
    工具结果循环可以正确处理，因为它针对缩短的调用列表迭代
    `range(len(results))`。保留警告日志，以便操作员在触发时有线索
    """
    result = []
    for c in calls:
        try:
            value = {
                "name": c.action_name,
                "call_id": c.id,
                "params": c.parameters,
            }
            result.append(value)
        except Exception as e:
            logger.warning(
                f"为 Python 编排器序列化 ActionCall 失败 — 丢弃条目: "
                f"action_name={c.action_name}, error={e}"
            )
    return result


def python_json_to_action_calls(value: Any) -> Optional[List[ActionCall]]:
    """将 action_calls JSON 数组（Python 交换形状）反序列化回规范的 ActionCall

    失败时记录警告而不是静默吞掉。引入此助手的整个提交是为了撤销一个
    `.ok()` 吞掉，该吞掉在没有任何信号的情况下丢弃了 action_calls —
    用另一个 `.ok()?` 替换它只会重新引入同样的陷阱，只是更深一层。
    如果形状再次漂移（Python 编排器字段重命名、额外的必需字段、部分迁移），
    警告是操作员可见的线索，解释了为什么后续工具结果在
    `sanitize_tool_messages` 中突然看起来是孤立的

    警告日志发出结构摘要（`summarize_action_calls_for_log`）而不是原始值，
    因为工具参数可能包含用户 PII
    """
    if not isinstance(value, list):
        logger.warning(
            f"从 Python 编排器解析 action_calls 失败: "
            f"期望列表但得到 {type(value).__name__}"
        )
        return None

    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            result.append(ActionCall(
                id=item.get("call_id", ""),
                action_name=item.get("name", ""),
                parameters=item.get("params", {}),
            ))
        except Exception as e:
            logger.warning(
                f"从 Python 编排器解析 action_calls 失败 — "
                f"助手消息将丢失 tool_call 链接，下游工具结果将被重写为用户消息: "
                f"error={e}"
            )
            return None

    return result


# ── 字符串截断 ───────────────────────────────────────────────

def tail_chars(s: str, n: int) -> str:
    """提取字符串的最后 `n` 个字符

    错误回溯出现在 stdout 的末尾，在任何 `print()` 输出之后。
    使用开头会捕获打印语句而不是错误
    """
    if len(s) > n:
        return s[-n:]
    return s


def tail_utf8_bytes(s: str, max_bytes: int) -> str:
    """返回 `s` 的最后 `max_bytes` 字节，向前调整到下一个 UTF-8 字符边界，
    以便切片始终是有效的 UTF-8

    基于字节（不像 [`tail_chars`]）以在大负载上保持 O(1)+边界遍历。
    由 `CodeExecuted` 发射路径使用，其中 `code`/`stdout` 可能任意大，
    因此在每个步骤上进行 `chars().count()` 将是可测量的开销
    """
    encoded = s.encode('utf-8')
    if len(encoded) <= max_bytes:
        return s

    start = len(encoded) - max_bytes
    # 向前移动以越过不完整的码点字节。
    # 最多 3 次迭代，因为 UTF-8 码点 ≤ 4 字节
    truncated = encoded[start:]
    try:
        return truncated.decode('utf-8')
    except UnicodeDecodeError:
        # 尝试从下一个可能的边界开始
        for offset in range(1, 4):
            try:
                return encoded[start + offset:].decode('utf-8')
            except UnicodeDecodeError:
                continue
        # 最后手段：从 max_bytes 位置开始
        return encoded[-max_bytes:].decode('utf-8', errors='ignore')


def bounded_return_value(value: Any, max_bytes: int) -> Optional[Any]:
    """在发出之前限制 CodeAct 返回值的序列化大小

    - `None` → `None`（无变化 — null 返回无需显示）
    - `str` → 通过 [`tail_utf8_bytes`] 尾部截断
    - 其他（list、dict、int/float、bool）→ 检查序列化长度；
      如果 ≤ `max_bytes` 则完整返回，否则丢弃

    丢弃（而不是截断任意 JSON）是故意的：截断的列表/字典
    在前端无法解析且不提供诊断价值。观察者看到 `None` 返回值，
    知道负载因大小而被省略，而不是它是 `null`
    """
    if value is None:
        return None
    if isinstance(value, str):
        return tail_utf8_bytes(value, max_bytes)

    # 对于其他类型，序列化后检查大小
    try:
        serialized = json.dumps(value, ensure_ascii=False).encode('utf-8')
        if len(serialized) <= max_bytes:
            return value
        return None
    except Exception:
        return None


# ── 日志摘要 ─────────────────────────────────────────────────

def summarize_action_calls_for_log(value: Any) -> str:
    """为日志输出构建 action_calls JSON 值的 PII 安全摘要

    action_calls 负载包含工具参数，这些参数可能携带用户 PII
    （搜索查询、文件名、邮件内容、对话文本）。
    将完整值转储到 `warning` 日志中会在解析器失败时立即将该 PII 泄露到
    日志聚合系统（Datadog、CloudWatch、Sentry）— 而解析器仅在
    Python ↔ Rust 形状漂移时失败，这正是操作员最可能正在搜索日志的时候

    我们只发出操作员调试形状漂移实际需要的结构信息：
    数组长度和第一个条目的键。键本身不是用户数据 — 它们是像
    `name`/`call_id`/`params` 这样的字段名，在所有调用中都是静态的
    """
    if not isinstance(value, list):
        return f"非数组值，类型为 {_json_value_type_name(value)}"

    if not value:
        return "空数组"

    first = value[0]
    if isinstance(first, dict):
        keys = sorted(first.keys())
        keys_str = ",".join(keys)
        return f"包含 {len(value)} 个条目的数组；第一个条目的键: [{keys_str}]"
    else:
        return f"包含 {len(value)} 个条目的数组；第一个条目: <非对象类型>"


def _json_value_type_name(value: Any) -> str:
    """返回 `value` 的廉价类型名称字符串。由 `summarize_action_calls_for_log`
    用于显示错误形状的情况（例如 Python 传递了字符串而不是数组），
    而不泄露实际内容
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# ── 消息转换 ─────────────────────────────────────────────────

def json_to_thread_messages(value: Any) -> Optional[List[ThreadMessage]]:
    """将 JSON 值转换为 ThreadMessage 列表"""
    if not isinstance(value, list):
        return None

    messages = []
    for item in value:
        if not isinstance(item, dict):
            continue

        role = item.get("role", "User")
        content = item.get("content", "")

        # 在调用解析器之前过滤掉 null — `action_calls: null`
        # 是 Python 合法的"此消息没有工具调用"信号（文本响应），
        # 不是解析错误。没有此过滤，`python_json_to_action_calls` 中的警告日志
        # 会在每个纯文本助手消息上触发，显示"无效类型: null，期望序列"
        action_calls_raw = item.get("action_calls")
        action_calls = None
        if action_calls_raw is not None:
            action_calls = python_json_to_action_calls(action_calls_raw)

        if role in ("System", "system"):
            message = ThreadMessage.system(content)
        elif role in ("Assistant", "assistant"):
            if action_calls:
                message = ThreadMessage.assistant_with_actions(content, action_calls)
            else:
                message = ThreadMessage.assistant(content)
        elif role in ("ActionResult", "action_result"):
            message = ThreadMessage.action_result(
                item.get("action_call_id", ""),
                item.get("action_name", ""),
                content,
            )
        else:
            message = ThreadMessage.user(content)

        messages.append(message)

    return messages


# ── 状态同步 ─────────────────────────────────────────────────

def sync_runtime_state(thread: Thread, state: Optional[dict]) -> None:
    """将运行时状态同步到线程"""
    if state is None:
        return

    working_messages = state.get("working_messages")
    if working_messages is not None:
        messages = json_to_thread_messages(working_messages)
        if messages is not None:
            thread.internal_messages = messages
            thread.updated_at = datetime.now(timezone.utc)


def sync_visible_outcome(thread: Thread, outcome: ThreadOutcome) -> None:
    """将可见结果同步到线程消息"""
    if hasattr(outcome, 'response') and outcome.response is not None:
        response = outcome.response
        # 检查是否已经存在相同的助手消息
        already_present = False
        if thread.messages:
            last_msg = thread.messages[-1]
            if (last_msg.role == MessageRole.Assistant
                    and last_msg.content == response):
                already_present = True

        if not already_present:
            thread.add_message(ThreadMessage.assistant(response))


def parse_outcome(result: dict) -> ThreadOutcome:
    """将编排器的返回值解析为 ThreadOutcome"""
    outcome = result.get("outcome", "completed")
    if not isinstance(outcome, str):
        outcome = "completed"

    if outcome == "completed":
        response = result.get("response")
        return ThreadOutcome.Completed(
            response=response if isinstance(response, str) else None,
        )
    elif outcome == "stopped":
        return ThreadOutcome.Stopped()
    elif outcome == "max_iterations":
        return ThreadOutcome.MaxIterations()
    elif outcome == "failed":
        error = result.get("error", "未知错误")
        return ThreadOutcome.Failed(
            error=error if isinstance(error, str) else "未知错误",
            debug_detail=None,
        )
    elif outcome == "gate_paused":
        resume_kind_value = result.get("resume_kind", {})
        resume_kind = ResumeKind.from_dict(resume_kind_value) if isinstance(resume_kind_value,
                                                                            dict) else ResumeKind.Approval(
            allow_always=False)

        gate_name = result.get("gate_name", "unknown")
        action_name = result.get("action_name", "")
        call_id = result.get("call_id", "")
        parameters = result.get("parameters", {})
        resume_output = result.get("resume_output")
        paused_lease = result.get("paused_lease")

        return ThreadOutcome.GatePaused(
            gate_name=gate_name if isinstance(gate_name, str) else "unknown",
            action_name=action_name if isinstance(action_name, str) else "",
            call_id=call_id if isinstance(call_id, str) else "",
            parameters=parameters if isinstance(parameters, dict) else {},
            resume_kind=resume_kind,
            resume_output=resume_output,
            paused_lease=paused_lease,
        )
    else:
        return ThreadOutcome.Completed(response=None)


def extract_string_arg(
        args: List[Any],
        kwargs: Dict[str, Any],
        name: str,
        position: int,
) -> Optional[str]:
    """从位置参数或关键字参数中提取字符串值

    优先检查关键字参数，然后回退到位置参数
    """
    # 首先检查关键字参数
    if isinstance(kwargs, dict) and name in kwargs:
        val = kwargs[name]
        if isinstance(val, str):
            return val
        return str(val) if val is not None else None

    # 回退到位置参数
    if position < len(args):
        val = args[position]
        if isinstance(val, str):
            return val
        return str(val) if val is not None else None

    return None


def extract_string_kwarg(kwargs: Dict[str, Any], name: str) -> Optional[str]:
    """从关键字参数中提取字符串值"""
    if isinstance(kwargs, dict) and name in kwargs:
        val = kwargs[name]
        if isinstance(val, str):
            return val
        return str(val) if val is not None else None
    return None


def extract_u64_kwarg(kwargs: Dict[str, Any], name: str) -> Optional[int]:
    """从关键字参数中提取无符号整数值"""
    if isinstance(kwargs, dict) and name in kwargs:
        val = kwargs[name]
        if isinstance(val, int) and val >= 0:
            return val
        if isinstance(val, float) and val >= 0 and val.is_integer():
            return int(val)
    return None
