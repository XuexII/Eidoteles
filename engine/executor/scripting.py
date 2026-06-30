# 第 1 层执行器：通过 Monty 运行嵌入式 Python。
#
# 使用 Monty 解释器执行大语言模型生成的 Python 代码。
# 工具调用采用**异步分发**：每次工具调用通过 `resume_pending()` 返回一个 Monty `ExternalFuture`，
# 允许 Python 代码使用 `await` 和 `asyncio.gather()` 进行并行执行。
# 当所有任务被阻塞时，Monty 会产生 `ResolveFutures`，我们通过 `JoinSet` 并发执行待处理的工具。
#
# 遵循 RLM（递归语言模型）模式：
# - 线程上下文作为 Python 变量注入（而非大语言模型注意力输入）
# - `llm_query()` / `llm_query_batched()` 用于递归生成子智能体
# - `FINAL(answer)` / `FINAL_VAR(name)` 用于显式终止
# - 步骤 0 导向性前言，用于上下文感知
# - 错误流回大语言模型进行自我修正（而非终止步骤）
# - 输出截断至可配置限制，并附有变量列表
# - `asyncio.gather()` 用于并行工具执行（通过 ResolveFutures）

import ast
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Any, Dict
import logging
from monty import (
    ExcType, ExtFunctionResult, LimitedTracker, MontyDate, MontyDateTime, MontyException,
    MontyObject, MontyRun, NameLookupResult, OsFunction, PrintWriter, ResourceLimits, RunProgress,
)
from tracing import debug

from ..capability.lease import LeaseManager
from ..capability.policy import (PolicyDecision, PolicyEngine)
from ..traits.effect import (EffectExecutor, ThreadExecutionContext)
from ..traits.llm import (LlmBackend, LlmCallConfig)
from ..types.capability import ActionDef
from ..types.error import EngineError
from ..types.event import EventKind
from ..types.message import (MessageRole, ThreadMessage)
from ..types.step import (ActionResult, CodeExecutionFailure, LlmResponse, TokenUsage)
from ..types.thread import Thread
from ironclaw_common import ValidTimezone

logger = logging.getLogger(__name__)

# ── 配置常量 ─────────────────────────────────────────────────

# 步骤之间包含在 LLM 上下文中的输出最大字符数。
# 匹配 Prime Intellect 的默认值。将来可按线程配置
OUTPUT_TRUNCATE_LEN = 8_000

# 紧凑元数据中预览前缀的最大字符数
OUTPUT_PREVIEW_LEN = 200

# 语法验证接受的编排器源代码最大大小（256 KB）。
# 编译时的默认值约为 2 KB；此上限很宽松，但可防止
# 病态输入在存储写入路径上造成可避免的 CPU/内存压力
MAX_ORCHESTRATOR_SOURCE_BYTES = 256 * 1024


# ── 日期时间构建 ─────────────────────────────────────────────

def build_datetime_now(args: List[Any]) -> dict:
    """为当前时刻构建一个日期时间对象

    当 args[0] 是时区信息时（带固定偏移的感知日期时间）或 None
    （UTC 中的天真日期时间，匹配 CPython 不带 tz 的 `datetime.datetime.now()` 行为），
    遵循该时区。其他任何内容被视为"无 tz"而不是引发错误 —
    我们更希望 LLM 获得可用的时钟读取，即使它传递了奇怪的参数
    """
    utc_now = datetime.now(timezone.utc)

    offset_seconds = None
    timezone_name = None

    if len(args) > 0 and isinstance(args[0], dict) and args[0].get("type") == "TimeZone":
        tz_data = args[0]
        offset_seconds = tz_data.get("offset_seconds")
        timezone_name = tz_data.get("name")

    if offset_seconds is not None:
        try:
            tz = timezone(timedelta(seconds=offset_seconds))
            aware = utc_now.astimezone(tz)
        except Exception:
            aware = utc_now
    else:
        aware = utc_now

    return {
        "type": "DateTime",
        "year": aware.year,
        "month": aware.month,
        "day": aware.day,
        "hour": aware.hour,
        "minute": aware.minute,
        "second": aware.second,
        "microsecond": aware.microsecond,
        "offset_seconds": offset_seconds,
        "timezone_name": timezone_name,
    }


def build_date_today() -> dict:
    """为今天的 UTC 日期构建一个日期对象

    Python 的 `date.today()` 是时区无关的（CPython 上的本地日期）；
    我们返回 UTC 以避免沙箱内的主机时钟时区意外。
    需要本地日期的代理应使用显式时区调用 `time` 工具
    """
    today = datetime.now(timezone.utc).date()
    return {
        "type": "Date",
        "year": today.year,
        "month": today.month,
        "day": today.day,
    }


# ── 默认资源限制 ────────────────────────────────────────────

def default_limits() -> ResourceLimits:
    """Monty 执行的默认资源限制

    `max_duration` 是从 VM 启动开始的实际时间，在内联门控等待暂停期间
    也会计时（我们在相同的 Monty 执行内部等待用户输入）。
    30 秒是捕获不分配内存的失控 CPU 绑定脚本所需的时间
    （`while True: x += 1`）；将其提高到"30 分钟以适应人类批准"
    会挂起这些测试。权衡：使用 30 秒，超过 30 秒的批准会导致脚本超时，
    用户必须重试。大多数批准在几秒钟内返回；更长时间的是已记录的局限性。
    适当的"活跃 CPU vs 暂停"计时器拆分在后续列表中
    （参见 `docs/plans/2026-05-01-codeact-inline-gate-await.md`）
    """
    return ResourceLimits(
        max_duration_secs=30,
        max_allocations=1_000_000,
        max_memory=64 * 1024 * 1024,  # 64 MB
    )


# ── 语法验证 ─────────────────────────────────────────────────

def validate_python_syntax(code: str) -> None:
    """检查 `code` 是否为语法有效的 Python，而不执行它

    使用 Python 的 `ast` 模块（与执行时相同），因此语法检查与运行时完全相同。
    如果有效返回 `None`，否则引发包含语法问题描述的 `SyntaxError`

    **威胁模型**：语法验证防止损坏的补丁消耗故障预算槽（3 次连续故障触发自动回滚），
    而不是防止执行危险代码。语义上危险的模式（`exec(compile(...))`、
    `__import__('os')`）通过验证，因为它们是语法有效的 Python。
    所有安全执行在运行时的 Monty 沙箱中进行（资源限制、主机函数门控、
    无文件系统/网络访问）

    **运行时成本**：`ast.parse()` 仅构建 AST — 不分配堆、不创建命名空间、
    不执行任何 Python 指令。没有模块级代码在此运行。成本随解析器输入大小
    而变化，因此我们将输入限制在 `MAX_ORCHESTRATOR_SOURCE_BYTES`
    （256 KB；编译时的默认值约为 2 KB）以保持存储写入路径
    不会成为病态补丁的 CPU/内存放大器
    """
    encoded = code.encode('utf-8')
    if len(encoded) > MAX_ORCHESTRATOR_SOURCE_BYTES:
        raise ValueError(
            f"编排器源代码过大: {len(encoded)} 字节 (限制: {MAX_ORCHESTRATOR_SOURCE_BYTES})"
        )

    try:
        ast.parse(code)
    except SyntaxError as e:
        raise SyntaxError(f"语法错误: {e}")


# ── 代码执行结果 ─────────────────────────────────────────────

@dataclass
class CodeExecutionResult:
    """执行代码块的结果"""
    # 转换为 JSON 的 Python 返回值
    return_value: Any = None
    # 捕获的打印输出
    stdout: str = ""
    # 执行期间进行的所有动作调用
    action_results: List[ActionResult] = field(default_factory=list)
    # 执行期间生成的事件
    events: List[EventKind] = field(default_factory=list)
    # 如果设置，执行因批准而被中断
    need_approval: Optional[ThreadOutcome] = None
    # 递归 llm_query() 调用使用的 token
    recursive_tokens: TokenUsage = field(default_factory=TokenUsage)
    # 如果设置，代码使用此答案调用了 FINAL() 或 FINAL_VAR()
    final_answer: Optional[str] = None
    # 分类的失败类别。执行成功或被门控暂停时为 None。
    # 代码执行失败时为 Some(category) — `failure is not None` 替换了
    # 以前的 `had_error: bool` 字段
    failure: Optional[CodeExecutionFailure] = None


# ── 输出格式化 ──────────────────────────────────────────────

def compact_output_metadata(stdout: str, return_value: Any) -> str:
    """构建用于在步骤之间包含在 LLM 上下文中的紧凑输出摘要

    截断到 `OUTPUT_TRUNCATE_LEN`（显示最后 N 个字符，类似 fast-rlm）。
    如果可用，包括 REPL 变量名列表
    """
    parts = []

    if stdout:
        char_count = len(stdout)
        if char_count > OUTPUT_TRUNCATE_LEN:
            truncated = stdout[-OUTPUT_TRUNCATE_LEN:]
            parts.append(
                f"[已截断: 显示 {char_count} 个字符中的最后 {OUTPUT_TRUNCATE_LEN} 个]\n{truncated}"
            )
        else:
            parts.append(f"[完整输出: {char_count} 个字符]\n{stdout}")

    if return_value is not None:
        val_str = json.dumps(return_value, ensure_ascii=False, indent=2)
        val_char_count = len(val_str)
        if val_char_count > OUTPUT_PREVIEW_LEN:
            preview = val_str[:OUTPUT_PREVIEW_LEN]
            parts.append(f"返回值 ({val_char_count} 个字符): {preview}...")
        else:
            parts.append(f"返回值: {val_str}")

    if not parts:
        return "[代码已执行，无输出]"
    else:
        return "\n".join(parts)


# ── 门控解决方案映射 ─────────────────────────────────────────

class DenialOutcome:
    """门控未批准的原因。区分用户驱动的拒绝和
    "没有活跃的批准处理程序到达用户"，以便脚本面向和事件日志消息
    不会将取消/过期错误标记为用户拒绝

    包装行为过去对每个非 `Approved` 解决方案使用
    `format!("user denied tool 'X': {reason}")`；
    当脚本在 `CancellingGateController`（无控制器接线）下运行或
    桥接控制器在过期/关闭时取消时，这错误地显示为
    "用户拒绝了工具 'X': 已取消"。在这些情况下，用户从未看到提示 —
    他们没有拒绝任何东西

    此处的辅助函数为每个表面（事件日志错误、脚本面向的异常、
    `EngineError::Effect` 原因）生成正确的措辞，
    以便所有 Tier 0 / Tier 1 调用站点保持一致
    """

    def __init__(self, outcome_type: str, reason: str = "", detail: str = ""):
        self.outcome_type = outcome_type
        self.reason = reason
        self.detail = detail

    @classmethod
    def DeniedByUser(cls, reason: str) -> "DenialOutcome":
        """用户主动拒绝了门控（或主机的控制器将"无输入"视为拒绝）。
        原因文本通常来自用户或控制器的拒绝原因
        """
        return cls(outcome_type="denied_by_user", reason=reason)

    @classmethod
    def Unavailable(cls, detail: str) -> "DenialOutcome":
        """没有活跃的批准处理程序到达用户 — 控制器缺失
        （`CancellingGateController`），桥接控制器在过期/关闭时取消，
        或引擎收到了内联路径不支持的解决方案变体
        """
        return cls(outcome_type="unavailable", detail=detail)

    def event_error(self) -> str:
        """用于 `EventKind::ActionFailed` 的预格式化 `error` 字符串。
        在追踪/审计/观察者路径中显示；此处的 "denied:" 前缀在门控控制器
        存在之前与策略拒绝路径对齐，因此用户驱动的拒绝保留此前缀以保持连续性。
        `Unavailable` 使用不同的前缀，以便扫描日志的操作员可以区分
        "用户说不"和"未显示提示"
        """
        if self.outcome_type == "denied_by_user":
            return f"被拒绝: {self.reason}"
        else:
            return f"批准不可用: {self.detail}"

    def script_message(self, tool_name: str) -> str:
        """用于 CodeAct 脚本的预格式化 `RuntimeError` 消息。
        按名称标识工具，以便脚本可以根据失败原因进行分支，
        并在消息文本中直接显示用户驱动拒绝和无处理程序/已取消之间的区别 —
        修复了后者错误显示为 "用户拒绝了工具 'X': 已取消" 的问题
        """
        if self.outcome_type == "denied_by_user":
            return f"用户拒绝了工具 '{tool_name}': {self.reason}"
        else:
            return f"工具 '{tool_name}' 的批准不可用: {self.detail}"

    def effect_reason(self) -> str:
        """用于 `EngineError::Effect`（Tier 0 结构化路径，
        其中错误被向上冒泡而不是渲染为 Python 异常）的裸原因字符串。
        与 `event_error` 形状相同
        """
        return self.event_error()


def denial_outcome_for_resolution(resolution: GateResolution) -> Optional[DenialOutcome]:
    """Tier 0（`structured.rs`）和 Tier 1（此模块中的同步预检 + 异步输出路径）
    共享的单一真实来源，以便拒绝消息不会在执行器之间漂移

    对于 `Approved` 返回 `None`（唯一允许执行继续的结果）
    """
    if resolution.type == "Approved":
        return None
    elif resolution.type == "Denied":
        reason = resolution.reason or "被用户拒绝"
        return DenialOutcome.DeniedByUser(reason=reason)
    elif resolution.type == "Cancelled":
        return DenialOutcome.Unavailable(detail="批准已取消")
    elif resolution.type in ("CredentialProvided", "ExternalCallback"):
        return DenialOutcome.Unavailable(detail="不支持的门控解决方案")
    else:
        return DenialOutcome.Unavailable(detail="未知的门控解决方案")


# ── 步骤 0 引导前言 ─────────────────────────────────────────

def build_orientation_preamble(thread: Thread) -> str:
    """构建在第一次 LLM 调用之前自动执行的步骤 0 引导前言，
    为模型提供上下文的结构感知
    """
    msg_count = len(thread.messages)
    total_chars = sum(len(m.content) for m in thread.messages)
    user_msgs = sum(1 for m in thread.messages if m.role == MessageRole.User)

    preview = ""
    # 从末尾查找最后一条用户消息
    for m in reversed(thread.messages):
        if m.role == MessageRole.User:
            content = m.content
            content_preview = content[:500]
            truncated = "..." if len(content) > 500 else ""
            preview = f"\n最后一条用户消息预览: {content_preview}{truncated}"
            break

    return (
        f"[步骤 0 — 上下文引导]\n"
        f"目标: {thread.goal}\n"
        f"上下文: {msg_count} 条消息, {total_chars} 个总字符, {user_msgs} 条来自用户\n"
        f"步骤: {thread.step_count + 1}{preview}"
    )


# ── 上下文注入（RLM 3.4）─────────────────────────────────────

def build_context_inputs(
        thread: Thread,
        persisted_state: dict,
) -> tuple:
    """从线程状态构建 Monty 输入变量

    `persisted_state` 携带来自先前代码步骤的变量，
    这样即使每个步骤创建新的 MontyRun，REPL 也感觉是持久的
    """
    names = []
    values = []

    # `context` — 线程消息作为字典列表
    messages = []
    for msg in thread.messages:
        msg_dict = {
            "role": str(msg.role),
            "content": msg.content,
        }
        if hasattr(msg, 'action_name') and msg.action_name is not None:
            msg_dict["action_name"] = msg.action_name
        messages.append(msg_dict)

    names.append("context")
    values.append(messages)

    # `goal` — 线程的目标字符串
    names.append("goal")
    values.append(thread.goal)

    # `step_number` — 当前步骤索引
    names.append("step_number")
    values.append(thread.step_count)

    # `state` — 来自先前代码步骤的持久化变量。
    # 这是一个累积的字典：返回值、工具结果等。
    # 模型可以读取 `state["results"]`、`state["prev_return"]` 等
    names.append("state")
    values.append(persisted_state if persisted_state else {})

    # `previous_results` — 来自先前步骤的 {call_id: result_json} 字典
    result_pairs = {}
    for msg in thread.messages:
        if msg.role == MessageRole.ActionResult:
            call_id = msg.action_call_id if hasattr(msg, 'action_call_id') else None
            if call_id:
                result_pairs[call_id] = msg.content

    names.append("previous_results")
    values.append(result_pairs)

    # `user_timezone` — 来自用户频道的已验证 IANA 时区（例如 "America/New_York"）
    user_timezone_str = thread.metadata.get("user_timezone") if isinstance(thread.metadata, dict) else None
    tz = "UTC"
    if isinstance(user_timezone_str, str):
        try:
            vtz = ValidTimezone.parse(user_timezone_str)
            if vtz is not None:
                tz = vtz.name()
        except Exception:
            pass

    names.append("user_timezone")
    values.append(tz)

    return (names, values)


# ── 线程局部存储：用于挂起的门控 ──────────────────────────────

# 每个执行作用域的 PENDING_GATE_STASH 线程局部变量，
# 以便内联等待的 Cancelled+Authentication 回退（在 `drive_inline_gate` 内部更深层）
# 有一个侧通道可以在退出时将原始门控作为 `need_approval` 显示。
# 参见静态变量的文档注释以了解其存在的原因
_pending_gate_stash = local()


@contextmanager
def pending_gate_scope():
    """为挂起的门控创建一个新的作用域上下文"""
    old_value = getattr(_pending_gate_stash, 'value', None)
    _pending_gate_stash.value = None
    try:
        yield
    finally:
        _pending_gate_stash.value = old_value


def get_pending_gate():
    """获取当前挂起的门控"""
    return getattr(_pending_gate_stash, 'value', None)


def set_pending_gate(gate):
    """设置当前挂起的门控"""
    _pending_gate_stash.value = gate


# ── 主执行函数 ───────────────────────────────────────────────

async def execute_code(
        code: str,
        thread: Thread,
        llm: LlmBackend,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        context: ThreadExecutionContext,
        capability_policies: List[PolicyRule],
        persisted_state: dict,
) -> CodeExecutionResult:
    """使用 Monty 执行 Python 代码块

    处理完整的 RLM 执行模式：上下文作为变量、FINAL() 终止、
    llm_query() 递归调用、错误到 LLM 的流程以及输出截断
    """
    return await execute_code_with_skills(
        code=code,
        thread=thread,
        llm=llm,
        effects=effects,
        leases=leases,
        policy=policy,
        context=context,
        capability_policies=capability_policies,
        persisted_state=persisted_state,
        skill_snippet_names=[],
    )


async def execute_code_with_skills(
        code: str,
        thread: Thread,
        llm: LlmBackend,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        context: ThreadExecutionContext,
        capability_policies: List[PolicyRule],
        persisted_state: dict,
        skill_snippet_names: List[str],
) -> CodeExecutionResult:
    """使用可选的技能代码片段执行 Python 代码块

    `skill_snippet_names` 被注册为 Monty NameLookup 中的额外已知函数，
    与能力租约中的工具名称一起
    """
    # 将每次执行的 PENDING_GATE_STASH 作用域化，
    # 以便内联等待的 Cancelled+Authentication 回退（在 `drive_inline_gate` 内部更深层）
    # 有一个侧通道可以在退出时将原始门控作为 `need_approval` 显示。
    # 参见静态变量的文档注释以了解其存在的原因
    with pending_gate_scope():
        return await execute_code_with_skills_inner(
            code=code,
            thread=thread,
            llm=llm,
            effects=effects,
            leases=leases,
            policy=policy,
            context=context,
            capability_policies=capability_policies,
            persisted_state=persisted_state,
            skill_snippet_names=skill_snippet_names,
        )


async def execute_code_with_skills_inner(
        code: str,
        thread: Thread,
        llm: LlmBackend,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        context: ThreadExecutionContext,
        capability_policies: List[PolicyRule],
        persisted_state: dict,
        skill_snippet_names: List[str],
) -> CodeExecutionResult:
    """代码执行的内部实现

    在实际实现中，此函数会：
    1. 构建上下文输入变量（build_context_inputs）
    2. 创建 Monty 运行时并注入代码
    3. 注册所有可用的工具函数作为主机函数
    4. 注册技能片段作为已知函数
    5. 驱动 Monty 执行循环，处理函数调用（工具、llm_query、FINAL 等）
    6. 收集输出、动作结果和事件
    7. 处理错误和门控暂停
    """
    # 此函数的具体实现取决于 Monty 运行时的 Python 版本
    # 这里提供框架结构，实际执行逻辑由具体的 Monty 实现处理
    pass


# ── 预检结果 ─────────────────────────────────────────────────

class PreflightResultType(Enum):
    Approved = "Approved"
    Denied = "Denied"
    GatePaused = "GatePaused"


@dataclass
class PreflightResult:
    """动作预检的结果"""
    result_type: PreflightResultType
    lease: Optional[CapabilityLease] = None
    ext_result: Optional[Any] = None
    outcome: Optional[ThreadOutcome] = None

    @classmethod
    def Approved(cls, lease: CapabilityLease) -> "PreflightResult":
        return cls(result_type=PreflightResultType.Approved, lease=lease)

    @classmethod
    def Denied(cls, ext_result: Any) -> "PreflightResult":
        return cls(result_type=PreflightResultType.Denied, ext_result=ext_result)

    @classmethod
    def GatePaused(cls, outcome: ThreadOutcome) -> "PreflightResult":
        return cls(result_type=PreflightResultType.GatePaused, outcome=outcome)


# ── 挂起未来 ─────────────────────────────────────────────────

class PendingFutureType(Enum):
    Tool = "Tool"
    Llm = "Llm"


@dataclass
class PendingFuture:
    """挂起的异步执行"""
    future_type: PendingFutureType
    # 用于工具调用
    task: Optional[asyncio.Task] = None
    action_name: Optional[str] = None
    call_id: Optional[str] = None
    lease_id: Optional[LeaseId] = None
    params_summary: Optional[str] = None

    @classmethod
    def Tool(cls, task: asyncio.Task, action_name: str, call_id: str,
             lease_id: LeaseId, params_summary: Optional[str] = None) -> "PendingFuture":
        return cls(
            future_type=PendingFutureType.Tool,
            task=task,
            action_name=action_name,
            call_id=call_id,
            lease_id=lease_id,
            params_summary=params_summary,
        )

    @classmethod
    def Llm(cls, task: asyncio.Task) -> "PendingFuture":
        return cls(future_type=PendingFutureType.Llm, task=task)

    @staticmethod
    def ready_none() -> "PendingFuture":
        """创建一个已就绪的空未来（用于 FINAL/FINAL_VAR）"""

        async def _none():
            return None

        return PendingFuture(
            future_type=PendingFutureType.Llm,
            task=asyncio.ensure_future(_none()),
        )


# ── 主执行函数 ───────────────────────────────────────────────

async def execute_code_with_skills_inner(
        code: str,
        thread: Thread,
        llm: LlmBackend,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        context: ThreadExecutionContext,
        capability_policies: List[PolicyRule],
        persisted_state: dict,
        skill_snippet_names: List[str],
) -> CodeExecutionResult:
    """使用可选的技能代码片段执行 Python 代码块的内部实现"""
    stdout = ""
    action_results = []
    events = []
    recursive_tokens = TokenUsage()
    final_answer = None

    # 构建包含先前步骤持久化状态的上下文变量
    input_names, input_values = build_context_inputs(thread, persisted_state if persisted_state else {})

    # 收集已知的工具名称，以便 NameLookup 可以返回可调用的存根。
    # 没有这一步，代码中的 `mission_list()` 会引发 NameError，
    # 因为 Monty 在调用之前解析名称，而 Undefined → NameError
    active_leases = await leases.active_for_thread(thread.id)
    inventory = None
    try:
        inventory = await effects.available_action_inventory(active_leases, context)
    except Exception as error:
        logger.debug(f"线程 {thread.id}: 加载脚本执行的动作清单失败: {error}")

    available_actions = list(inventory.inline) if inventory is not None else []
    execution_context = context.clone()
    if inventory is not None:
        execution_context.available_actions_snapshot = available_actions
        execution_context.available_action_inventory_snapshot = inventory

    known_actions = set()
    for action in available_actions:
        known_actions.add(action.name)

    # 将技能代码片段函数名称注册为额外的已知动作。
    # 这些在 NameLookup 中解析，因此 LLM 可以将它们作为 Python 函数调用
    for name in skill_snippet_names:
        known_actions.add(name)

    # 解析和编译
    try:
        runner = MontyRun(code, "step.py", input_names)
    except SyntaxError as e:
        # 解析错误流回 LLM（不是终止）
        return CodeExecutionResult(
            return_value=None,
            stdout=f"SyntaxError: {e}",
            action_results=action_results,
            events=events,
            need_approval=None,
            recursive_tokens=recursive_tokens,
            final_answer=None,
            failure=CodeExecutionFailure.SyntaxError,
        )
    except Exception:
        # VM 恐慌
        return CodeExecutionResult(
            return_value=None,
            stdout=f"{stdout}\nVmPanic: Monty VM 在代码解析期间发生恐慌",
            action_results=action_results,
            events=events,
            need_approval=None,
            recursive_tokens=recursive_tokens,
            final_answer=None,
            failure=CodeExecutionFailure.VmPanic,
        )

    # 使用资源限制和上下文输入启动执行
    try:
        progress = runner.start(input_values, default_limits(), stdout)
    except Exception as e:
        # 运行时错误流回 LLM。在分类之前，检查内联等待回退存储：
        # 如果 Cancelled+Authentication 门控触发并显示为
        # `RuntimeError("execution paused by gate ...")`，将其显示为
        # `need_approval`，以便编排器可以产生 `ThreadOutcome::GatePaused`
        # 并且任务流程转换到 Paused。
        # 没有这一步，Tier 1 任务线程会静默吞掉门控（原始的 #3133 幽灵触发形状）
        pending_gate = take_pending_gate_stash()
        category = classify_runtime_error(str(e))
        return CodeExecutionResult(
            return_value=None,
            stdout=f"{stdout}\nError: {e}",
            action_results=action_results,
            events=events,
            need_approval=pending_gate,
            recursive_tokens=recursive_tokens,
            final_answer=None,
            failure=category,
        )

    # 挂起的异步工具执行，以 Monty call_id 为键。
    # 当工具 FunctionCall 到达时，我们创建一个 asyncio 任务并在此处存储。
    # 当 ResolveFutures 产生时，我们等待它们
    pending_futures = {}

    # 驱动执行循环
    call_counter = 0
    while True:
        if isinstance(progress, RunComplete):
            return CodeExecutionResult(
                return_value=progress.value,
                stdout=stdout,
                action_results=action_results,
                events=events,
                need_approval=None,
                recursive_tokens=recursive_tokens,
                final_answer=final_answer,
                failure=None,
            )

        elif isinstance(progress, RunFunctionCall):
            call_counter += 1
            str_call_id = f"code_call_{call_counter}"
            monty_call_id = progress.call_id
            action_name = progress.function_name
            params = progress.args if progress.args else {}

            logger.debug(f"Monty: 函数调用 action={action_name}, call_id={str_call_id}, monty_id={monty_call_id}")

            # 需要同步结果的内置函数 — 以值恢复
            #
            # FINAL / FINAL_VAR 同步设置 `final_answer`，但也安装一个
            # 轻松解析的挂起未来。这样 `FINAL(x)` 和 `await FINAL(x)` 都是有效的：
            # 同步调用只是丢弃协程对象，而 `await` 将其解析为 None。
            # LLM 经常通过类比工具调用发出 `await FINAL(...)`，
            # 因此支持两者避免了一整类"NoneType can't be awaited"失败
            sync_result = None

            if action_name == "FINAL":
                answer = str(progress.args[0]) if progress.args else ""
                final_answer = answer
                pending_futures[monty_call_id] = PendingFuture.ready_none()
            elif action_name == "FINAL_VAR":
                var_name = str(progress.args[0]) if progress.args else "result"
                final_answer = f"[FINAL_VAR: {var_name}]"
                pending_futures[monty_call_id] = PendingFuture.ready_none()
            elif action_name == "llm_query":
                # LLM 调用是异步的 — 创建 asyncio 任务，resume_pending。
                # 这允许 asyncio.gather(llm_query(...), tool(...))
                # 并发运行 LLM 调用和工具调用
                task = asyncio.ensure_future(
                    handle_llm_query_standalone(progress.args, progress.kwargs, llm)
                )
                pending_futures[monty_call_id] = PendingFuture.Llm(task=task)
            elif action_name == "llm_query_batched":
                task = asyncio.ensure_future(
                    handle_llm_query_batched_standalone(progress.args, progress.kwargs, llm)
                )
                pending_futures[monty_call_id] = PendingFuture.Llm(task=task)
            elif action_name == "rlm_query":
                # rlm_query 保持同步 — 它创建一个子 Monty VM，该 VM 不可 Send，
                # 因此不能在 asyncio 任务中运行
                sync_result = await handle_rlm_query(
                    progress.args,
                    progress.kwargs,
                    thread,
                    llm,
                    effects,
                    leases,
                    policy,
                    recursive_tokens,
                    execution_context.gate_controller,
                )
            elif action_name in ("globals", "locals"):
                entries = {name: True for name in known_actions}
                sync_result = ExtFunctionResult.Return(entries)

            if sync_result is not None:
                # 内置函数的同步恢复
                try:
                    progress = progress.resume(sync_result, stdout)
                    continue
                except Exception as e:
                    pending_gate = take_pending_gate_stash()
                    stdout += f"\nError: {e}"
                    return CodeExecutionResult(
                        return_value=None,
                        stdout=stdout,
                        action_results=action_results,
                        events=events,
                        need_approval=pending_gate,
                        recursive_tokens=recursive_tokens,
                        final_answer=final_answer,
                        failure=classify_runtime_error(str(e)),
                    )

            # 如果 LLM 调用已经插入了挂起未来，只需 resume_pending 并继续 —
            # 不需要预检
            if monty_call_id in pending_futures:
                try:
                    progress = progress.resume_pending(stdout)
                    continue
                except Exception as e:
                    pending_gate = take_pending_gate_stash()
                    stdout += f"\nError: {e}"
                    return CodeExecutionResult(
                        return_value=None,
                        stdout=stdout,
                        action_results=action_results,
                        events=events,
                        need_approval=pending_gate,
                        recursive_tokens=recursive_tokens,
                        final_answer=final_answer,
                        failure=classify_runtime_error(str(e)),
                    )

            # ── 异步工具调度 ─────────────────────────────
            # 预检（租约 + 策略）是同步的。如果被拒绝或需要批准，
            # 立即以错误恢复。如果已批准，创建 asyncio 任务并 resume_pending()

            preflight = await preflight_action(
                action_name,
                params,
                thread,
                leases,
                policy,
                execution_context,
                capability_policies,
                str_call_id,
                events,
            )

            if preflight.result_type == PreflightResultType.Approved:
                # 创建异步执行任务
                lease = preflight.lease
                ctx = execution_context.clone()
                ctx.current_call_id = str_call_id
                ps = summarize_params(action_name, params)

                async def tool_task(_effects=effects, _name=action_name, _params=params,
                                    _lease=lease, _ctx=ctx):
                    execution_start = time.monotonic()
                    try:
                        result = await _effects.execute_action(_name, _params, _lease, _ctx)
                        duration_ms = int((time.monotonic() - execution_start) * 1000)
                        return (result, duration_ms)
                    except Exception as e:
                        duration_ms = int((time.monotonic() - execution_start) * 1000)
                        return (e, duration_ms)

                task = asyncio.ensure_future(tool_task())
                pending_futures[monty_call_id] = PendingFuture.Tool(
                    task=task,
                    action_name=action_name,
                    call_id=str_call_id,
                    lease_id=lease.id,
                    params_summary=ps,
                )

                # 以挂起未来恢复 — Python 获得 ExternalFuture
                try:
                    progress = progress.resume_pending(stdout)
                    continue
                except Exception as e:
                    stdout += f"\nError: {e}"
                    return CodeExecutionResult(
                        return_value=None,
                        stdout=stdout,
                        action_results=action_results,
                        events=events,
                        need_approval=None,
                        recursive_tokens=recursive_tokens,
                        final_answer=final_answer,
                        failure=CodeExecutionFailure.ToolError,
                    )

            elif preflight.result_type == PreflightResultType.Denied:
                # 以错误恢复 — Python 看到异常
                try:
                    progress = progress.resume(preflight.ext_result, stdout)
                    continue
                except Exception as e:
                    stdout += f"\nError: {e}"
                    return CodeExecutionResult(
                        return_value=None,
                        stdout=stdout,
                        action_results=action_results,
                        events=events,
                        need_approval=None,
                        recursive_tokens=recursive_tokens,
                        final_answer=final_answer,
                        failure=CodeExecutionFailure.ToolError,
                    )

            elif preflight.result_type == PreflightResultType.GatePaused:
                # 内联门控等待：保持 Monty VM 活跃，暂停等待用户，
                # 并在解决方案后从确切的暂停点继续。
                # 控制器在上下文中是必需的 — 不暂停的代码路径提供
                # `CancellingGateController`，它将门控显示为此处的类型化拒绝
                outcome = preflight.outcome
                if not hasattr(outcome, 'gate_name'):
                    return CodeExecutionResult(
                        return_value=None,
                        stdout=stdout,
                        action_results=action_results,
                        events=events,
                        need_approval=None,
                        recursive_tokens=recursive_tokens,
                        final_answer=final_answer,
                        failure=CodeExecutionFailure.ToolError,
                    )

                gate_name = outcome.gate_name
                gate_action_name = outcome.action_name
                gate_call_id = outcome.call_id
                gate_parameters = outcome.parameters
                resume_kind = outcome.resume_kind

                resolution = await execution_context.gate_controller.pause(GatePauseRequest(
                    thread_id=thread.id,
                    user_id=thread.user_id,
                    gate_name=gate_name,
                    action_name=gate_action_name,
                    call_id=gate_call_id,
                    parameters=gate_parameters,
                    resume_kind=resume_kind,
                    conversation_id=execution_context.conversation_id,
                ))

                denial = denial_outcome_for_resolution(resolution)

                if denial is not None:
                    # 在恢复 Monty 之前记录拒绝到线程事件日志，
                    # 以便观察者/追踪分析在所有拒绝路径
                    # （此站点 + `drive_inline_gate` + `structured.rs`）中看到一致的 ActionFailed 输出
                    events.append(EventKind.ActionFailed(
                        step_id=execution_context.step_id,
                        action_name=gate_action_name,
                        call_id=gate_call_id,
                        error=denial.event_error(),
                        duration_ms=0,
                        params_summary=summarize_params(gate_action_name, gate_parameters),
                    ))
                    # 以类型化异常恢复 Monty。RuntimeError 是我们发出的内容；
                    # 消息是显式的，以便用户（和 LLM）可以区分拒绝与其他运行时错误。
                    # `script_message` 区分用户驱动的拒绝（"用户拒绝了工具 'X': ..."）
                    # 和无处理程序/已过期/已取消的门控（"工具 'X' 的批准不可用: ..."）
                    ext_result = ExtFunctionResult.Error(
                        RuntimeError(denial.script_message(gate_action_name))
                    )
                    try:
                        progress = progress.resume(ext_result, stdout)
                        continue
                    except Exception as e:
                        stdout += f"\nError: {e}"
                        return CodeExecutionResult(
                            return_value=None,
                            stdout=stdout,
                            action_results=action_results,
                            events=events,
                            need_approval=None,
                            recursive_tokens=recursive_tokens,
                            final_answer=final_answer,
                            failure=CodeExecutionFailure.ToolError,
                        )

                # 已批准。重新进行预检 — 桥接在交付解决方案之前安装了
                # 任何自动批准偏好，因此策略现在返回 Allow
                retry_preflight = await preflight_action(
                    gate_action_name,
                    gate_parameters,
                    thread,
                    leases,
                    policy,
                    execution_context,
                    capability_policies,
                    gate_call_id,
                    events,
                )

                if retry_preflight.result_type == PreflightResultType.Approved:
                    lease = retry_preflight.lease
                    ctx = execution_context.clone()
                    ctx.current_call_id = gate_call_id
                    # 将用户的一次性批准带入重试调用，以便主机跳过其每次调用批准检查
                    ctx.call_approval_granted = True
                    ps = summarize_params(gate_action_name, gate_parameters)

                    async def retry_tool_task():
                        execution_start = time.monotonic()
                        try:
                            result = await effects.execute_action(
                                gate_action_name, gate_parameters, lease, ctx
                            )
                            duration_ms = int((time.monotonic() - execution_start) * 1000)
                            return (result, duration_ms)
                        except Exception as e:
                            duration_ms = int((time.monotonic() - execution_start) * 1000)
                            return (e, duration_ms)

                    task = asyncio.ensure_future(retry_tool_task())
                    pending_futures[monty_call_id] = PendingFuture.Tool(
                        task=task,
                        action_name=gate_action_name,
                        call_id=gate_call_id,
                        lease_id=lease.id,
                        params_summary=ps,
                    )

                    try:
                        progress = progress.resume_pending(stdout)
                        continue
                    except Exception as e:
                        stdout += f"\nError: {e}"
                        return CodeExecutionResult(
                            return_value=None,
                            stdout=stdout,
                            action_results=action_results,
                            events=events,
                            need_approval=None,
                            recursive_tokens=recursive_tokens,
                            final_answer=final_answer,
                            failure=CodeExecutionFailure.ToolError,
                        )

                elif retry_preflight.result_type == PreflightResultType.Denied:
                    # 竞态条件：在批准和重试之间有人更改了租约/策略。
                    # 将错误显示给 Python，以便脚本可以处理（或未捕获的崩溃）
                    try:
                        progress = progress.resume(retry_preflight.ext_result, stdout)
                        continue
                    except Exception:
                        return CodeExecutionResult(
                            return_value=None,
                            stdout=stdout,
                            action_results=action_results,
                            events=events,
                            need_approval=None,
                            recursive_tokens=recursive_tokens,
                            final_answer=final_answer,
                            failure=CodeExecutionFailure.ToolError,
                        )

                elif retry_preflight.result_type == PreflightResultType.GatePaused:
                    # 策略在用户同意后仍然说需要批准。
                    # 视为拒绝，这样我们就不会永远循环
                    ext_result = ExtFunctionResult.Error(
                        RuntimeError(f"工具 '{gate_action_name}' 在解决方案后仍然需要批准")
                    )
                    try:
                        progress = progress.resume(ext_result, stdout)
                        continue
                    except Exception:
                        return CodeExecutionResult(
                            return_value=None,
                            stdout=stdout,
                            action_results=action_results,
                            events=events,
                            need_approval=None,
                            recursive_tokens=recursive_tokens,
                            final_answer=final_answer,
                            failure=CodeExecutionFailure.ToolError,
                        )

        # ── ResolveFutures：并行执行 ────────────────
        # 解析通过 resume_pending() 延迟的工具调用和 LLM 调用。
        # 所有挂起的 asyncio 任务被等待，其结果反馈给 Monty
        elif isinstance(progress, RunResolveFutures):
            pending_ids = progress.pending_call_ids()
            logger.debug(f"Monty: ResolveFutures — 正在解析 {len(pending_ids)} 个挂起的未来")

            results = []
            for mid in pending_ids:
                if mid in pending_futures:
                    pf = pending_futures.pop(mid)
                    if pf.future_type == PendingFutureType.Tool:
                        ext_result = await resolve_tool_future(
                            pf.task, pf.action_name, pf.call_id, pf.lease_id,
                            pf.params_summary, leases, effects, context,
                            action_results, events,
                        )
                    elif pf.future_type == PendingFutureType.Llm:
                        ext_result = await resolve_llm_future(pf.task, recursive_tokens)
                    else:
                        ext_result = ExtFunctionResult.Error(
                            RuntimeError(f"未知的挂起 call_id {mid}")
                        )
                else:
                    logger.debug(f"ResolveFutures: 未知的挂起 call_id {mid}")
                    ext_result = ExtFunctionResult.Error(
                        RuntimeError(f"未知的挂起 call_id {mid}")
                    )
                results.append((mid, ext_result))

            try:
                progress = progress.resume(results, stdout)
                continue
            except Exception as e:
                pending_gate = take_pending_gate_stash()
                stdout += f"\nError: {e}"
                return CodeExecutionResult(
                    return_value=None,
                    stdout=stdout,
                    action_results=action_results,
                    events=events,
                    need_approval=pending_gate,
                    recursive_tokens=recursive_tokens,
                    final_answer=final_answer,
                    failure=classify_runtime_error(str(e)),
                )

        # ── NameLookup：名称解析 ────────────────────────
        elif isinstance(progress, RunNameLookup):
            name = progress.name

            if name in known_actions:
                logger.debug(f"Monty: 解析为工具函数 name={name}")
                result = NameLookupResult.Value({"name": name, "docstring": None})
            elif name in ("globals", "locals"):
                result = NameLookupResult.Value({"name": name, "docstring": None})
            else:
                logger.debug(f"Monty: 未解析的名称 name={name}")
                result = NameLookupResult.Undefined

            try:
                progress = progress.resume(result, stdout)
                continue
            except Exception as e:
                stdout += f"\nNameError: {e}"
                return CodeExecutionResult(
                    return_value=None,
                    stdout=stdout,
                    action_results=action_results,
                    events=events,
                    need_approval=None,
                    recursive_tokens=recursive_tokens,
                    final_answer=final_answer,
                    failure=CodeExecutionFailure.NameLookup,
                )

        # ── OsCall：操作系统调用 ────────────────────────
        elif isinstance(progress, RunOsCall):
            # 时钟读取（`datetime.now()`、`date.today()`）不是安全问题 —
            # 它们不接触网络、文件系统或环境。Monty 将它们作为专用 OsFunction
            # 变体显示，而不是不透明的系统调用，因此我们可以直接回答它们，
            # 而不是返回全面的 OSError。其他任何内容仍然被拒绝
            if progress.function == "DateTimeNow":
                reply = ExtFunctionResult.Return(build_datetime_now(progress.args))
            elif progress.function == "DateToday":
                reply = ExtFunctionResult.Return(build_date_today())
            else:
                logger.debug(f"Monty: OS 调用被拒绝 function={progress.function}")
                reply = ExtFunctionResult.Error(
                    OSError("CodeAct 脚本中不允许操作系统操作")
                )

            try:
                progress = progress.resume(reply, stdout)
                continue
            except Exception as e:
                stdout += f"\nOSError: {e}"
                return CodeExecutionResult(
                    return_value=None,
                    stdout=stdout,
                    action_results=action_results,
                    events=events,
                    need_approval=None,
                    recursive_tokens=recursive_tokens,
                    final_answer=final_answer,
                    failure=CodeExecutionFailure.OsDenied,
                )


# ── 辅助函数 ─────────────────────────────────────────────────

async def preflight_action(
        action_name: str,
        params: dict,
        thread: Thread,
        leases: LeaseManager,
        policy: PolicyEngine,
        context: ThreadExecutionContext,
        capability_policies: List[PolicyRule],
        call_id: str,
        events: List[EventKind],
) -> PreflightResult:
    """对动作进行预检：查找租约并检查策略。
    如果被拒绝或需要批准，返回相应的 PreflightResult。
    如果已批准，返回包含已消耗租约的 Approved
    """
    # 1. 查找此动作的租约
    lease = await leases.find_lease_for_action(thread.id, action_name)
    if lease is None:
        error = f"动作 '{action_name}' 没有活跃租约"
        ext_result = ExtFunctionResult.Error(RuntimeError(error))
        events.append(EventKind.ActionFailed(
            step_id=context.step_id,
            action_name=action_name,
            call_id=call_id,
            error=error,
            duration_ms=0,
            params_summary=None,
        ))
        return PreflightResult.Denied(ext_result)

    # 2. 检查策略
    action_def = None
    for action in (context.available_actions_snapshot or []):
        if action.matches_name(action_name):
            action_def = action
            break

    if action_def is not None:
        decision = policy.evaluate(action_def, lease, capability_policies)
        if isinstance(decision, Deny):
            error = f"被拒绝: {decision.reason}"
            ext_result = ExtFunctionResult.Error(RuntimeError(error))
            events.append(EventKind.ActionFailed(
                step_id=context.step_id,
                action_name=action_name,
                call_id=call_id,
                error=decision.reason,
                duration_ms=0,
                params_summary=None,
            ))
            return PreflightResult.Denied(ext_result)
        elif isinstance(decision, RequireApproval):
            # 需要批准
            outcome = ThreadOutcome.GatePaused(
                gate_name="approval",
                action_name=action_name,
                call_id=call_id,
                parameters=params,
                resume_kind=ResumeKind.Approval(allow_always=True),
            )
            return PreflightResult.GatePaused(outcome)

    # 3. 消耗一次租约使用
    try:
        lease = await leases.find_and_consume(thread.id, action_name)
        return PreflightResult.Approved(lease)
    except Exception as e:
        error = f"租约消耗失败: {e}"
        ext_result = ExtFunctionResult.Error(RuntimeError(error))
        events.append(EventKind.ActionFailed(
            step_id=context.step_id,
            action_name=action_name,
            call_id=call_id,
            error=error,
            duration_ms=0,
            params_summary=None,
        ))
        return PreflightResult.Denied(ext_result)


async def resolve_tool_future(
        task: asyncio.Task,
        action_name: str,
        call_id: str,
        lease_id: LeasesId,
        params_summary: Optional[str],
        leases: LeaseManager,
        effects: EffectExecutor,
        context: ThreadExecutionContext,
        action_results: List[ActionResult],
        events: List[EventKind],
) -> ExtFunctionResult:
    """解析工具调用的异步任务结果"""
    try:
        result, duration_ms = await task
    except Exception as e:
        error_result = ActionResult(
            call_id=call_id,
            action_name=action_name,
            output={"error": str(e)},
            is_error=True,
            duration_ms=0,
        )
        action_results.append(error_result)
        events.append(EventKind.ActionFailed(
            step_id=context.step_id,
            action_name=action_name,
            call_id=call_id,
            error=str(e),
            duration_ms=0,
            params_summary=params_summary,
        ))
        return ExtFunctionResult.Return(error_result.output)

    if isinstance(result, Exception):
        error_result = ActionResult(
            call_id=call_id,
            action_name=action_name,
            output={"error": str(result)},
            is_error=True,
            duration_ms=duration_ms,
        )
        action_results.append(error_result)
        events.append(EventKind.ActionFailed(
            step_id=context.step_id,
            action_name=action_name,
            call_id=call_id,
            error=str(result),
            duration_ms=duration_ms,
            params_summary=params_summary,
        ))
        return ExtFunctionResult.Return(error_result.output)

    # 成功结果
    action_result = ActionResult(
        call_id=call_id,
        action_name=action_name,
        output=result.output if hasattr(result, 'output') else result,
        is_error=result.is_error if hasattr(result, 'is_error') else False,
        duration_ms=duration_ms,
    )
    action_results.append(action_result)

    if action_result.is_error:
        error_msg = str(action_result.output.get("error", str(action_result.output)))
        events.append(EventKind.ActionFailed(
            step_id=context.step_id,
            action_name=action_name,
            call_id=call_id,
            error=error_msg,
            duration_ms=duration_ms,
            params_summary=params_summary,
        ))
    else:
        events.append(EventKind.ActionExecuted(
            step_id=context.step_id,
            action_name=action_name,
            call_id=call_id,
            duration_ms=duration_ms,
            params_summary=params_summary,
        ))

    return ExtFunctionResult.Return(action_result.output)


async def resolve_llm_future(
        task: asyncio.Task,
        recursive_tokens: TokenUsage,
) -> ExtFunctionResult:
    """解析 LLM 调用的异步任务结果"""
    try:
        result = await task
        if hasattr(result, 'usage'):
            recursive_tokens.input_tokens += result.usage.input_tokens
            recursive_tokens.output_tokens += result.usage.output_tokens
            recursive_tokens.cost_usd += result.usage.cost_usd
        return ExtFunctionResult.Return(result)
    except Exception as e:
        return ExtFunctionResult.Error(RuntimeError(f"LLM 调用失败: {e}"))


def take_pending_gate_stash() -> Optional[ThreadOutcome]:
    """获取并清除挂起的门控存储"""
    gate = get_pending_gate()
    set_pending_gate(None)
    return gate


def classify_runtime_error(error_msg: str) -> CodeExecutionFailure:
    """将运行时错误分类为 CodeExecutionFailure 类型"""
    lower = error_msg.lower()

    if "syntax" in lower:
        return CodeExecutionFailure.SyntaxError
    elif "name" in lower and ("not defined" in lower or "lookup" in lower):
        return CodeExecutionFailure.NameLookup
    elif "resource" in lower or "memory" in lower or "timeout" in lower or "timed out" in lower:
        return CodeExecutionFailure.ResourceLimit
    elif "os" in lower and ("denied" in lower or "blocked" in lower or "not permitted" in lower):
        return CodeExecutionFailure.OsDenied
    elif "tool" in lower and "error" in lower:
        return CodeExecutionFailure.ToolError
    elif "panic" in lower:
        return CodeExecutionFailure.VmPanic
    else:
        return CodeExecutionFailure.RuntimeError


# ── 错误分类 ─────────────────────────────────────────────────

def classify_runtime_error(error_msg: str) -> CodeExecutionFailure:
    """将运行时错误消息分类为失败类别

    解析来自 Monty 的错误文本，以区分 LLM 逻辑错误
    （NameError、TypeError 等）、资源限制命中和 Monty VM 问题
    """
    lower = error_msg.lower()

    # 最具体的检查优先，以避免子字符串误报
    if any(keyword in lower for keyword in [
        "timed out", "timeout", "memory limit", "allocation limit",
        "out of fuel", "fuel exhausted", "resource limit",
    ]):
        return CodeExecutionFailure.ResourceLimit
    elif "os operations are not permitted" in lower or "oserror" in lower:
        return CodeExecutionFailure.OsDenied
    elif "syntaxerror" in lower:
        return CodeExecutionFailure.SyntaxError
    else:
        # NameError、TypeError、ValueError、AttributeError、IndexError、
        # KeyError、ModuleNotFoundError、NotImplementedError 等
        return CodeExecutionFailure.RuntimeError


def code_hash(code: str) -> str:
    """计算 Python 代码的短哈希值，用于事件中的去重/关联

    使用 FNV-1a（64 位），在不同 Python 版本间保持稳定。
    非加密用途 — 在典型使用水平下碰撞概率约为 2^-32，足以用于去重
    """
    FNV_OFFSET = 0xcbf29ce484222325
    FNV_PRIME = 0x00000100000001B3

    hash_val = FNV_OFFSET
    for byte in code.encode('utf-8'):
        hash_val ^= byte
        hash_val = (hash_val * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF  # 保持在 64 位范围内

    return f"{hash_val:016x}"


# ── llm_query() — 递归子代理（RLM 3.5）───────────────────────

async def handle_llm_query(
        args: List[Any],
        kwargs: Dict[str, Any],
        llm: LlmBackend,
        recursive_tokens: TokenUsage,
) -> ExtFunctionResult:
    """处理 `llm_query(prompt, context)` — 单次递归子调用"""
    prompt = extract_string_arg(args, kwargs, "prompt", 0)
    context_arg = extract_string_arg(args, kwargs, "context", 1)
    # `model` 必须显式解析 — `extract_string_arg` 通过 `str()` 强制转换，
    # 会将 `None` 变成字面字符串 "None" 并将非字符串值字符串化，
    # 两者都会静默地将调用路由到无效的模型 ID。只接受 str 或 None
    model_arg = extract_optional_string_kwarg(args, kwargs, "model", 2)

    if prompt is None:
        return ExtFunctionResult.Error(TypeError("llm_query() 需要 'prompt' 参数"))

    messages = []
    if context_arg is not None:
        messages.append(ThreadMessage.system(
            f"你是一个子代理。根据上下文简洁回答。\n\n{context_arg}"
        ))
    else:
        # 某些提供者（例如 OpenAI Codex Responses API）需要系统消息/指令字段。
        # 始终包含一个
        messages.append(ThreadMessage.system("你是一个有用的子代理。请简洁回答。"))
    messages.append(ThreadMessage.user(prompt))

    config = LlmCallConfig(
        force_text=True,
        model=model_arg,
    )

    try:
        output = await llm.complete(messages, [], config)
        recursive_tokens.input_tokens += output.usage.input_tokens
        recursive_tokens.output_tokens += output.usage.output_tokens

        if isinstance(output.response, TextResponse):
            text = output.response.content
        elif isinstance(output.response, ActionCallsResponse):
            text = output.response.content or ""
        elif isinstance(output.response, CodeResponse):
            text = output.response.content or ""
        else:
            text = ""

        return ExtFunctionResult.Return(text)
    except Exception as e:
        return ExtFunctionResult.Error(RuntimeError(f"llm_query 失败: {e}"))


async def handle_llm_query_standalone(
        args: List[Any],
        kwargs: Dict[str, Any],
        llm: LlmBackend,
) -> tuple:
    """处理独立的 llm_query 调用，返回 (ExtFunctionResult, TokenUsage)"""
    recursive_tokens = TokenUsage()
    result = await handle_llm_query(args, kwargs, llm, recursive_tokens)
    return (result, recursive_tokens)


# ── llm_query_batched() — 并行递归子调用（RLM 3.5）───────────

async def handle_llm_query_batched(
        args: List[Any],
        kwargs: Dict[str, Any],
        llm: LlmBackend,
        recursive_tokens: TokenUsage,
) -> ExtFunctionResult:
    """处理 `llm_query_batched(prompts)` — 并行递归子调用

    接受提示字符串列表并并发调度它们。
    以相同顺序返回响应字符串列表
    """
    # 提取提示列表（第一个参数或关键字参数 "prompts"）
    prompts_obj = None
    if len(args) > 0:
        prompts_obj = args[0]
    if prompts_obj is None and isinstance(kwargs, dict):
        prompts_obj = kwargs.get("prompts")

    if isinstance(prompts_obj, list):
        prompts = [str(p) for p in prompts_obj]
    elif prompts_obj is not None:
        return ExtFunctionResult.Error(
            TypeError(f"llm_query_batched() 期望提示列表，但得到 {type(prompts_obj).__name__}")
        )
    else:
        return ExtFunctionResult.Error(
            TypeError("llm_query_batched() 需要 'prompts' 参数")
        )

    # 位置/关键字参数布局（匹配文档签名
    # `llm_query_batched(prompts, context=None, model=None, models=None)`）：
    #   arg 0 = prompts   （上面已提取）
    #   arg 1 = context
    #   arg 2 = model
    #   arg 3 = models
    context_arg = extract_optional_string_kwarg(args, kwargs, "context", 1)

    # 可选的模型覆盖：
    #   - `model="..."` 将相同模型应用于每个提示
    #   - `models=[...]` 是并行数组（必须匹配 prompts 长度）；
    #     通过传递 `prompts=[same]*N, models=[m1, m2, ...]` 使用此方式
    #     将相同提示广播到模型委员会。在 `models` 中，`None` 槽表示
    #     "此提示无覆盖"（调用者选择不为该槽路由）；单数 `model=` 关键字参数
    #     不会填充这些槽，因为混合两者会令人惊讶
    single_model = extract_optional_string_kwarg(args, kwargs, "model", 2)

    models_list = None
    if isinstance(kwargs, dict):
        models_kwarg = kwargs.get("models")
    else:
        models_kwarg = args[3] if len(args) > 3 else None

    if isinstance(models_kwarg, list):
        models_list = []
        for item in models_kwarg:
            if isinstance(item, str):
                models_list.append(item)
            elif item is None:
                models_list.append(None)
            else:
                return ExtFunctionResult.Error(
                    TypeError(f"llm_query_batched(): models 列表条目必须是 str 或 None")
                )
    elif models_kwarg is not None:
        return ExtFunctionResult.Error(
            TypeError(f"llm_query_batched(): `models` 必须是 str 或 None 的列表")
        )

    if models_list is not None and len(models_list) != len(prompts):
        return ExtFunctionResult.Error(
            ValueError(f"llm_query_batched(): models 列表长度 ({len(models_list)}) "
                       f"必须匹配 prompts 长度 ({len(prompts)})")
        )

    # 并发执行所有 LLM 调用
    async def make_llm_call(index: int, prompt: str):
        # 如果提供了 `models=`，每个槽是权威的 — `None` 表示
        # "此提示无覆盖"，不从 `model=` 回填。
        # 否则，回退到单数 `model=` 关键字参数（或 None）
        if models_list is not None:
            model_override = models_list[index]
        else:
            model_override = single_model

        config = LlmCallConfig(force_text=True, model=model_override)

        messages = []
        if context_arg is not None:
            messages.append(ThreadMessage.system(
                f"你是一个子代理。请简洁回答。\n\n{context_arg}"
            ))
        else:
            messages.append(ThreadMessage.system("你是一个有用的子代理。请简洁回答。"))
        messages.append(ThreadMessage.user(prompt))

        return await llm.complete(messages, [], config)

    tasks = [make_llm_call(i, prompt) for i, prompt in enumerate(prompts)]
    outputs = await asyncio.gather(*tasks, return_exceptions=True)

    # 收集结果
    results = []
    total_input = 0
    total_output = 0

    for output in outputs:
        if isinstance(output, Exception):
            results.append(f"Error: {output}")
        else:
            total_input += output.usage.input_tokens
            total_output += output.usage.output_tokens

            if isinstance(output.response, TextResponse):
                text = output.response.content
            elif isinstance(output.response, ActionCallsResponse):
                text = output.response.content or ""
            elif isinstance(output.response, CodeResponse):
                text = output.response.content or ""
            else:
                text = ""
            results.append(text)

    recursive_tokens.input_tokens += total_input
    recursive_tokens.output_tokens += total_output

    return ExtFunctionResult.Return(results)


async def handle_llm_query_batched_standalone(
        args: List[Any],
        kwargs: Dict[str, Any],
        llm: LlmBackend,
) -> tuple:
    """处理独立的 llm_query_batched 调用，返回 (ExtFunctionResult, TokenUsage)"""
    recursive_tokens = TokenUsage()
    result = await handle_llm_query_batched(args, kwargs, llm, recursive_tokens)
    return (result, recursive_tokens)


# ── rlm_query() — 完整递归子代理（RLM 3.5）───────────────────

async def handle_rlm_query(
        args: List[Any],
        kwargs: Dict[str, Any],
        parent_thread: Thread,
        llm: LlmBackend,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        recursive_tokens: TokenUsage,
        gate_controller: GateController,
) -> ExtFunctionResult:
    """处理 `rlm_query(prompt)` — 创建一个具有自己执行循环、工具和
    迭代预算的子 CodeAct 线程

    与 `llm_query()`（单次 LLM 调用）不同，`rlm_query()` 创建一个
    具有完整 CodeAct 能力的子线程。子线程继承父线程的剩余预算和工具访问权限
    """
    prompt = extract_string_arg(args, kwargs, "prompt", 0)
    if prompt is None:
        return ExtFunctionResult.Error(TypeError("rlm_query() 需要 'prompt' 参数"))

    # 深度检查 — 如果达到最大递归深度则拒绝
    current_depth = parent_thread.config.depth
    max_depth = parent_thread.config.max_depth
    if current_depth >= max_depth:
        return ExtFunctionResult.Error(
            RuntimeError(f"rlm_query() 深度限制已达到: 深度 {current_depth} >= 最大 {max_depth}")
        )

    # 使用继承的预算构建子线程
    remaining_tokens = None
    if parent_thread.config.max_tokens_total is not None:
        remaining_tokens = max(0, parent_thread.config.max_tokens_total - parent_thread.total_tokens_used)

    remaining_budget = None
    if parent_thread.config.max_budget_usd is not None:
        remaining_budget = max(0.0, parent_thread.config.max_budget_usd - parent_thread.total_cost_usd)

    child_config = ThreadConfig(
        max_iterations=min(parent_thread.config.max_iterations, 20),  # 限制子线程迭代次数
        enable_tool_intent_nudge=False,
        max_tokens_total=remaining_tokens,
        max_budget_usd=remaining_budget,
        max_duration=parent_thread.config.max_duration,
        depth=current_depth + 1,
        max_depth=max_depth,
    )

    child_thread = Thread.new(
        goal=prompt,
        thread_type=ThreadType.Research,
        project_id=parent_thread.project_id,
        user_id=parent_thread.user_id,
        config=child_config,
    ).with_parent(parent_thread.id)

    # 将提示添加为用户消息
    child_thread.add_message(ThreadMessage.user(prompt))

    # 创建信号通道和子线程的租约管理器
    signal_tx, signal_rx = signal_channel(8)
    child_leases = LeaseManager()

    # 授予子线程与父线程相同的租约（在子线程的管理器中）
    parent_leases_list = await leases.active_for_thread(parent_thread.id)
    now = datetime.now(timezone.utc)
    for parent_lease in parent_leases_list:
        # 将父租约的 expires_at 转换为剩余持续时间
        remaining_duration = None
        if parent_lease.expires_at is not None:
            delta = parent_lease.expires_at - now
            if delta.total_seconds() > 0:
                remaining_duration = delta

        try:
            lease = await child_leases.grant(
                thread_id=child_thread.id,
                capability_name=parent_lease.capability_name,
                granted_actions=parent_lease.granted_actions,
                duration=remaining_duration,
                max_uses=parent_lease.max_uses,
            )
            child_thread.capability_leases.append(lease.id)
        except Exception as e:
            logger.debug(f"rlm_query: 跳过子线程的无效租约: {e}")
            continue

    # 从父策略复制拒绝的效果
    child_policy_engine = PolicyEngine()
    for effect in policy.denied_effects:
        child_policy_engine.deny_effect(effect)

    # 创建并运行子执行循环
    child_loop = ExecutionLoop(
        thread=child_thread,
        llm=llm,
        effects=effects,
        leases=child_leases,
        policy=child_policy_engine,
        signal_rx=signal_rx,
        _user_id="rlm_child",
        gate_controller=gate_controller,
    )

    logger.debug(
        f"rlm_query: 创建子 CodeAct 线程, "
        f"parent_thread={parent_thread.id}, depth={current_depth + 1}, "
        f"prompt_len={len(prompt)}"
    )

    # 运行子循环
    try:
        outcome = await child_loop.run()
        # 跟踪子线程的 token 使用量
        recursive_tokens.input_tokens += child_loop.thread.total_tokens_used
        recursive_tokens.cost_usd += child_loop.thread.total_cost_usd

        if isinstance(outcome, ThreadOutcome) and hasattr(outcome, 'response'):
            response = outcome.response or ""
        elif isinstance(outcome, ThreadOutcome) and hasattr(outcome, 'error'):
            response = f"rlm_query 子线程失败: {outcome.error}"
        elif isinstance(outcome, ThreadOutcome) and hasattr(outcome, 'type'):
            if outcome.type == "MaxIterations":
                response = "rlm_query 子线程达到最大迭代次数"
            else:
                response = ""
        else:
            response = ""

        return ExtFunctionResult.Return(response)
    except Exception as e:
        return ExtFunctionResult.Error(RuntimeError(f"rlm_query 失败: {e}"))


# ── 辅助函数 ─────────────────────────────────────────────────

def extract_optional_string_kwarg(
        args: List[Any],
        kwargs: Dict[str, Any],
        name: str,
        position: int,
) -> Optional[str]:
    """提取可选的字符串关键字参数，正确处理 None 值

    与 `extract_string_arg` 不同，此函数区分 "None"（未提供/显式为 None）
    和字符串值。`extract_string_arg` 会将 `None` 强制转换为字面字符串 "None"
    """
    # 首先检查关键字参数
    if isinstance(kwargs, dict) and name in kwargs:
        val = kwargs[name]
        if val is None:
            return None
        if isinstance(val, str):
            return val
        return None

    # 回退到位置参数
    if position < len(args):
        val = args[position]
        if val is None:
            return None
        if isinstance(val, str):
            return val

    return None


# ── 常量 ─────────────────────────────────────────────────────

# 单个工具调用的内联门控等待迭代最大次数。
# 第一次尝试来自调用者；此上限涵盖了批准后策略仍要求批准
# 所触发的重试（例如，安装自动批准后第二个门控介入）。
# 三次对于任何合理的链都足够了 — 超过此数量的工具是行为异常的，
# 我们宁愿显示清晰的错误而不是永远循环
#
# 与 Tier 0（`structured::execute_with_inline_gate_retry`）共享，
# 因此两个执行器执行相同的上限
MAX_INLINE_GATE_RETRIES = 3


# ── 内联门控数据结构 ─────────────────────────────────────────

@dataclass
class InlineGate:
    """驱动一次内联门控等待所需的输入"""
    gate_name: str
    action_name: str
    call_id: str
    parameters: dict
    resume_kind: Any  # ResumeKind
    # 在门控引发时缓存的预计算动作输出。当动作*已经执行*
    # 且只有后续解决方案（例如 OAuth）挂起时 —
    # `effect_adapter` 在成功的 `tool_install` 后引发 Authentication 门控是典型情况 —
    # 桥接将安装的输出附加在此处。解决方案时我们返回该缓存输出，
    # 而不是重新执行动作，否则会重新下载 WASM 包并重新引发新的批准门控（#3533 后续）
    resume_output: Optional[dict] = None


# ── 内联门控驱动 ─────────────────────────────────────────────

async def drive_inline_gate(
        gate: InlineGate,
        leases: LeaseManager,
        effects: EffectExecutor,
        context: ThreadExecutionContext,
        action_results: List[ActionResult],
        events: List[EventKind],
        params_summary: Optional[str] = None,
) -> ExtFunctionResult:
    """将 `Approval` 门控驱动到终端解决方案，如果批准后重试本身
    返回 `GatePaused`，则最多重试动作 [`MAX_INLINE_GATE_RETRIES`] 次

    集中 Tier 1 的门控处理，以便异步输出路径和同步预检路径
    发出一致的事件/错误消息 — 并且行为异常的工具（批准后反复门控）
    产生有界的 `RuntimeError`，而不是泄露旧的
    "execution paused by gate" 消息
    """
    for _ in range(MAX_INLINE_GATE_RETRIES):
        resolution = await context.gate_controller.pause(GatePauseRequest(
            thread_id=context.thread_id,
            user_id=context.user_id,
            gate_name=gate.gate_name,
            action_name=gate.action_name,
            call_id=gate.call_id,
            parameters=gate.parameters,
            resume_kind=gate.resume_kind,
            conversation_id=context.conversation_id,
        ))

        denial = denial_outcome_for_resolution(resolution)
        if denial is not None:
            # 取消+认证 → 通过旧的 `RuntimeError("execution paused by gate ...")` 展开，
            # 以便外部编排器可以产生 `ThreadOutcome::GatePaused`，
            # 任务可以转换到 Paused。此处的取消意味着控制器无法内联解析认证
            # （无 OAuth 接线）— 旧展开路径是正确的回退。
            # 拒绝/显式用户取消仍然是失败
            if (hasattr(resolution, 'type') and resolution.type == "Cancelled"
                    and hasattr(gate.resume_kind, 'type') and gate.resume_kind.type == "Authentication"):
                # 存储原始门控，以便 `execute_code` 的退出可以将其显示为 `need_approval`。
                # 没有这一步，Tier 1 任务子线程会静默吞掉门控，
                # cron 会不断重新触发任务（#3133 幽灵触发）。
                # 尽力而为 — 如果线程局部不在作用域内（理论上不可能，
                # 因为我们总是在 `execute_code_with_skills` 的作用域内运行，但防御性的）
                set_pending_gate(ThreadOutcome.GatePaused(
                    gate_name=gate.gate_name,
                    action_name=gate.action_name,
                    call_id=gate.call_id,
                    parameters=gate.parameters,
                    resume_kind=gate.resume_kind,
                    resume_output=None,
                    paused_lease=None,
                ))
                return ExtFunctionResult.Error(
                    RuntimeError(f"执行被门控 '{gate.gate_name}' 暂停")
                )

            events.append(EventKind.ActionFailed(
                step_id=context.step_id,
                action_name=gate.action_name,
                call_id=gate.call_id,
                error=denial.event_error(),
                duration_ms=0,
                params_summary=params_summary,
            ))
            return ExtFunctionResult.Error(
                RuntimeError(denial.script_message(gate.action_name))
            )

        # 已批准。如果桥接在引发此门控之前缓存了动作的输出
        # （执行后认证门控路径 — 参见 `effect_adapter::auth_gate_from_extension_result`
        # 和 `check_tool_readiness` 路径），动作已经运行，我们只需要用户侧解决方案。
        # 跳过重新执行并直接返回缓存的输出。没有此短路，重试会重新运行
        # `tool_install`（重新下载 WASM），第二次通过 `effect_adapter::enforce_tool_permission`
        # 会引发用户无法解决的全新批准门控。由 #3533 跟踪
        if gate.resume_output is not None:
            cached_output = gate.resume_output
            gate.resume_output = None
            events.append(EventKind.ActionExecuted(
                step_id=context.step_id,
                action_name=gate.action_name,
                call_id=gate.call_id,
                duration_ms=0,
                params_summary=params_summary,
            ))
            action_results.append(ActionResult(
                call_id=gate.call_id,
                action_name=gate.action_name,
                output=cached_output,
                is_error=False,
                duration_ms=0,
            ))
            return ExtFunctionResult.Return(cached_output)

        # 重新获取租约使用并重试动作。桥接在交付解决方案之前安装了
        # 任何自动批准偏好，因此策略现在返回 Allow
        #
        # 注意：如果多个授权覆盖此动作，`find_and_consume` 可能选择
        # 与原始退还的租约不同的租约。这对于使用计数契约来说是可以的；
        # 如果租约曾经携带每个授权的身份（凭证绑定），
        # 需要重新评估此假设
        try:
            lease = await leases.find_and_consume(context.thread_id, gate.action_name)
        except Exception as e:
            return ExtFunctionResult.Error(
                RuntimeError(f"批准后租约不可用: {e}")
            )

        # 将用户的一次性批准带入重试调用，以便主机的 `EffectExecutor`
        # 跳过 `ApprovalRequirement::Always` / AskEachTime 门控，
        # 否则会再次触发。镜像旧的 `execute_resolved_pending_action(approval_already_granted=true)` 路径。
        # 通过 `current_call_id` 限定为此次单次调用
        retry_ctx = context.clone()
        retry_ctx.current_call_id = gate.call_id
        retry_ctx.call_approval_granted = True

        retry_start = time.monotonic()
        retry_result = await effects.execute_action(
            gate.action_name,
            gate.parameters,
            lease,
            retry_ctx,
        )
        retry_duration_ms = int((time.monotonic() - retry_start) * 1000)

        if isinstance(retry_result, ActionResult):
            # 成功或包装的错误
            if retry_result.is_error:
                error_msg = retry_result.output.get("error", str(retry_result.output)) if isinstance(
                    retry_result.output, dict) else str(retry_result.output)
                duration_ms = retry_result.duration_ms if retry_result.duration_ms > 0 else retry_duration_ms
                events.append(EventKind.ActionFailed(
                    step_id=context.step_id,
                    action_name=gate.action_name,
                    call_id=gate.call_id,
                    error=error_msg,
                    duration_ms=duration_ms,
                    params_summary=params_summary,
                ))
            else:
                events.append(EventKind.ActionExecuted(
                    step_id=context.step_id,
                    action_name=gate.action_name,
                    call_id=gate.call_id,
                    duration_ms=retry_result.duration_ms,
                    params_summary=params_summary,
                ))
            action_results.append(retry_result)
            return ExtFunctionResult.Return(retry_result.output)

        elif isinstance(retry_result, EngineError) and retry_result.error_type == "GatePaused":
            # 重试本身返回了 GatePaused — 再次循环
            resume_kind = retry_result.resume_kind
            resume_output = retry_result.resume_output

            # 退还我们刚刚消耗的使用次数 — 下一次循环迭代将暂停并在解决方案时重新消耗。
            # 例外：当重试的门控携带缓存的 `resume_output` 时，
            # 下一次迭代将返回该缓存输出而不重新消耗；在此处退还会将
            # 重试已经花费的租约使用清零
            if resume_output is None:
                await leases.refund_use(lease.id)

            events.append(EventKind.ApprovalRequested(
                action_name=retry_result.action_name,
                call_id=retry_result.call_id,
                parameters=retry_result.parameters,
                description=None,
                allow_always=getattr(resume_kind, 'allow_always', None) if hasattr(resume_kind,
                                                                                   'type') and resume_kind.type == "Approval" else None,
                gate_name=retry_result.gate_name,
                params_summary=params_summary,
            ))

            gate = InlineGate(
                gate_name=retry_result.gate_name,
                action_name=retry_result.action_name,
                call_id=retry_result.call_id,
                parameters=retry_result.parameters,
                resume_kind=resume_kind,
                resume_output=resume_output,
            )
            continue

        else:
            # 其他错误
            error_str = str(retry_result)
            events.append(EventKind.ActionFailed(
                step_id=context.step_id,
                action_name=gate.action_name,
                call_id=gate.call_id,
                error=error_str,
                duration_ms=retry_duration_ms,
                params_summary=params_summary,
            ))
            return ExtFunctionResult.Error(RuntimeError(error_str))

    # 重试预算耗尽 — 工具在每次批准后持续门控。
    # 显示为类型化错误，以便脚本可以响应；用户已经连续批准了这么多次，
    # 再问没有意义
    events.append(EventKind.ActionFailed(
        step_id=context.step_id,
        action_name=gate.action_name,
        call_id=gate.call_id,
        error=f"工具在 {MAX_INLINE_GATE_RETRIES} 次批准后持续门控",
        duration_ms=0,
        params_summary=params_summary,
    ))
    return ExtFunctionResult.Error(
        RuntimeError(f"工具 '{gate.action_name}' 在 {MAX_INLINE_GATE_RETRIES} 次重试后仍然需要批准")
    )


# ── 未来解析辅助函数 ─────────────────────────────────────────

async def resolve_tool_future(
        task: asyncio.Task,
        action_name: str,
        call_id: str,
        lease_id: LeaseId,
        params_summary: Optional[str],
        leases: LeaseManager,
        effects: EffectExecutor,
        context: ThreadExecutionContext,
        action_results: List[ActionResult],
        events: List[EventKind],
) -> ExtFunctionResult:
    """解析挂起的工具执行未来

    故意不接受原始 `parameters`：当工具返回 `EngineError::GatePaused` 时，
    门控携带自己的参数快照（可能由安全层转换），这就是我们向用户显示的内容。
    将原始参数传入此处会使它们成为误导的第二真实来源
    """
    try:
        result, execution_duration_ms = await task
    except Exception as e:
        logger.debug(f"异步工具任务异常: {e}")
        return ExtFunctionResult.Error(RuntimeError(f"工具执行异常: {e}"))

    if isinstance(result, ActionResult):
        # 如果效果适配器将工具错误包装为 is_error=True 的 ActionResult
        # （当前 `EffectBridgeAdapter::execute_action_internal` 中的约定），
        # 将其显示为 ActionFailed，以便追踪、观察者和批准流程正确看到失败。
        # 没有这一步，每个包装的错误看起来都像是成功的工具调用
        if result.is_error:
            error_msg = result.output.get("error", str(result.output)) if isinstance(result.output, dict) else str(
                result.output)
            duration_ms = result.duration_ms if result.duration_ms > 0 else execution_duration_ms
            events.append(EventKind.ActionFailed(
                step_id=context.step_id,
                action_name=action_name,
                call_id=call_id,
                error=error_msg,
                duration_ms=duration_ms,
                params_summary=params_summary,
            ))
        else:
            events.append(EventKind.ActionExecuted(
                step_id=context.step_id,
                action_name=action_name,
                call_id=call_id,
                duration_ms=result.duration_ms,
                params_summary=params_summary,
            ))
        action_results.append(result)
        return ExtFunctionResult.Return(result.output)

    elif isinstance(result, EngineError) and result.error_type == "GatePaused":
        # 当门控携带缓存的 `resume_output` 时跳过退款：
        # 动作已经执行（执行后认证门控），`drive_inline_gate` 将在批准时
        # 返回缓存输出而不重新消耗租约。在此处退还会让成功的副作用动作消耗零次使用。
        # 匹配的保护位于 `structured::execute_with_inline_gate_retry`
        # 和 `orchestrator::execute_action_with_inline_gate`。由 #3559 安全审查跟踪
        if result.resume_output is None:
            await leases.refund_use(lease_id)

        events.append(EventKind.ApprovalRequested(
            action_name=result.action_name,
            call_id=result.call_id,
            parameters=result.parameters,
            description=None,
            allow_always=getattr(result.resume_kind, 'allow_always', None) if hasattr(result.resume_kind,
                                                                                      'type') and result.resume_kind.type == "Approval" else None,
            gate_name=result.gate_name,
            params_summary=params_summary,
        ))

        # 外部恢复类型保留旧重新进入路径 —
        # 它们的解决方案安装回调负载状态，无法传回给暂停的调用。
        # Approval 和 Authentication 都通过 `drive_inline_gate`：
        # Approval 在用户点击时解决，Authentication 在
        # `bridge::resolve_inline_gates_for_credential`（来自 #3133 half-2 的 OAuth 回调钩子）
        # 将 `GateResolution::Approved` 传递给暂停的控制器时解决。
        # （任务范围的恢复通过 `bridge::resume_paused_missions_for_credential` —
        # 这是子线程在同一门控上暂停的后台任务的单独路径。）
        # 在这两种内联等待情况下，动作内联重试，脚本在不展开的情况下继续
        resume_kind = result.resume_kind
        if not (hasattr(resume_kind, 'type') and resume_kind.type in ("Approval", "Authentication")):
            return ExtFunctionResult.Error(
                RuntimeError(f"执行被门控 '{result.gate_name}' 暂停")
            )

        return await drive_inline_gate(
            InlineGate(
                gate_name=result.gate_name,
                action_name=result.action_name,
                call_id=result.call_id,
                parameters=result.parameters,
                resume_kind=resume_kind,
                resume_output=result.resume_output,
            ),
            leases,
            effects,
            context,
            action_results,
            events,
            params_summary,
        )

    elif isinstance(result, Exception):
        events.append(EventKind.ActionFailed(
            step_id=context.step_id,
            action_name=action_name,
            call_id=call_id,
            error=str(result),
            duration_ms=execution_duration_ms,
            params_summary=params_summary,
        ))
        action_results.append(ActionResult(
            call_id=call_id,
            action_name=action_name,
            output={"error": str(result)},
            is_error=True,
            duration_ms=execution_duration_ms,
        ))
        return ExtFunctionResult.Error(RuntimeError(str(result)))

    else:
        return ExtFunctionResult.Error(RuntimeError(f"未知的工具执行结果: {type(result).__name__}"))


async def resolve_llm_future(
        task: asyncio.Task,
        recursive_tokens: TokenUsage,
) -> ExtFunctionResult:
    """解析挂起的 LLM 调用未来，累积 token 使用量"""
    try:
        result, tokens = await task
        recursive_tokens.input_tokens += tokens.input_tokens
        recursive_tokens.output_tokens += tokens.output_tokens
        recursive_tokens.cost_usd += tokens.cost_usd
        return result
    except Exception as e:
        logger.debug(f"异步 LLM 任务异常: {e}")
        return ExtFunctionResult.Error(RuntimeError(f"LLM 调用异常: {e}"))


# ── MontyObject ↔ JSON 转换 ──────────────────────────────────

def monty_to_json(obj: Any) -> Any:
    """将 MontyObject 转换为 JSON 值"""
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return [monty_to_json(item) for item in obj]
    if isinstance(obj, tuple):
        return [monty_to_json(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): monty_to_json(v) for k, v in obj.items()}
    if isinstance(obj, bytes):
        return obj.hex()
    # 对于其他类型，回退到字符串表示
    return str(obj)


def json_to_monty(val: Any) -> Any:
    """将 JSON 值转换为 MontyObject"""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return val
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return [json_to_monty(item) for item in val]
    if isinstance(val, dict):
        return {k: json_to_monty(v) for k, v in val.items()}
    return str(val)


def monty_args_to_json(args: List[Any], kwargs: Dict[str, Any]) -> dict:
    """将 Monty 参数转换为 JSON"""
    result = {}
    if args:
        result["_args"] = [monty_to_json(arg) for arg in args]
    if isinstance(kwargs, dict):
        for k, v in kwargs.items():
            key = str(k)
            result[key] = monty_to_json(v)
    return result


def monty_to_string(obj: Any) -> str:
    """将 MontyObject 转换为字符串"""
    if obj is None:
        return "None"
    if isinstance(obj, bool):
        return str(obj)
    if isinstance(obj, (int, float)):
        return str(obj)
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(monty_to_json(obj), ensure_ascii=False)
    except Exception:
        return str(obj)


def extract_string_arg(
        args: List[Any],
        kwargs: Dict[str, Any],
        name: str,
        position: int,
) -> Optional[str]:
    """从位置参数或关键字参数中提取字符串值"""
    # 首先检查关键字参数
    if isinstance(kwargs, dict) and name in kwargs:
        return monty_to_string(kwargs[name])
    # 回退到位置参数
    if position < len(args):
        return monty_to_string(args[position])
    return None


def extract_optional_string_kwarg(
        args: List[Any],
        kwargs: Dict[str, Any],
        name: str,
        position: int,
) -> Optional[str]:
    """严格的可选字符串提取器，用于静默强制转换很危险的情况
    （例如 `model=` — 传递错误类型不应成为意外的模型 ID）。
    返回：
      - `None` 当参数缺失或显式为 None 时
      - 字符串值当参数是字符串时
      - 对于任何其他类型返回 None（不引发错误，与 Rust 版本略有不同）
    """
    # 首先检查关键字参数
    raw = None
    if isinstance(kwargs, dict) and name in kwargs:
        raw = kwargs[name]
    elif position < len(args):
        raw = args[position]

    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    # 对于非字符串类型，返回 None（而不是引发 TypeError，
    # 因为在 Python 中我们更宽松地处理类型）
    return None