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

from scripting import (execute_code, json_to_monty, monty_to_json, monty_to_string)
from thread_context import thread_execution_context

from ..capability.lease import LeaseManager
from ..capability.policy import PolicyEngine
from ..memory import RetrievalEngine
from ..runtime.lease_refresh import reconcile_dynamic_tool_lease
from ..runtime.messaging import (SignalReceiver, ThreadOutcome, ThreadSignal)
from ..traits.effect import (EffectExecutor, ThreadExecutionContext)
from ..traits.llm import (LlmBackend, LlmCallConfig)
from ..traits.store import Store
from ..types.event import (EventKind, ThreadEvent, summarize_params)
from ..types.message import ThreadMessage
from ..types.project import ProjectId
from ..types import shared_owner_id
from ..types.step import (ActionCall, StepId, TokenUsage)
from ..types.thread import (ActiveSkillProvenance, Thread, ThreadState)
from engine.executor.prompt import PlatformInfo
from engine.gate import GateController
import regex as re

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
):
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
    # context, goal, actions, state, config
    context, goal, actions, state, config = build_orchestrator_inputs(thread, persisted_state)

    max_iterations = config.get("max_iterations", 30)
    max_nudges = config.get("max_tool_intent_nudges", 2)
    nudge_enabled = config.get("enable_tool_intent_nudge", True)
    # None means "no limit" — callers can disable the guard explicitly.
    max_consecutive_errors = config.get("max_consecutive_errors", 5)
    # None means "no limit" (matches Option::None semantics from Rust caller).
    # Use a sentinel larger than any realistic counter so comparisons stay well-typed.
    if max_consecutive_errors is None:
        max_consecutive_errors = 10 ** 9
    obligation_enabled = config.get("require_action_attempt", False)
    max_obligation_nudges = config.get("max_action_requirement_nudges", 2)

    consecutive_nudges = 0
    consecutive_errors = 0
    consecutive_action_errors = 0
    step_count = config.get("step_count", 0)
    if not isinstance(state, dict):
        state = {}
    state.setdefault("history", [])
    state.setdefault("compaction_count", 0)

    # ----------Step1: 执行显示命令----------

    # ----------Step2: 执行显示命令----------
    working_messages = ensure_working_messages(state, context)

    for step in range(step_count, max_iterations):
        # ----------Step3: 检查信号----------
        signal = handle_check_signals(signal_rx, thread)

        # ----------Step4: 检查预算----------
        budget = handle_check_budget(thread)

        # ----------Step5: 在第一步注入先验知识并激活技能----------
        if step == 0:
            # ----------Step5-1: 查询记忆文档----------
            if docs := await handle_retrieve_docs(goal, 5, thread, retrieval):
                # 格式化
                knowledge = format_docs(docs)
                # 加到第一条的system中
                append_system_append(working_messages, knowledge)

            # ----------Step5-2: 根据目标关键词选择并注入技能----------
            all_skills = handle_list_skills(thread, store)
            explicit_skills, _rewritten_goal, missing_explicit_skills = extract_explicit_skills(all_skills, goal)
            if active_skills := select_skills(all_skills, goal, max_candidates=3, max_tokens=6000):
                explicit_names = set(
                    str(s.get("metadata", {}).get("name", ""))
                    for s in explicit_skills
                )
                skills_json = [
                    {
                        "doc_id": s.get("doc_id", ""),
                        "name": s.get("metadata", {}).get("name", "?"),
                        "version": s.get("metadata", {}).get("version", 1),
                        "snippet_names": [
                            sn.get("name", "")
                            for sn in s.get("metadata", {}).get("code_snippets", [])
                            if sn.get("name")
                        ],
                        "force_activated": (
                                s.get("metadata", {}).get("name", "") in explicit_names
                        ),
                    }
                    for s in active_skills
                ]
                ext_result = handle_set_active_skills(skills_json, thread)
                skill_text = format_skills(active_skills)
                append_system_append(working_messages, skill_text)
                # 为 CLI/网关显示发出技能激活事件——通知用户
                skill_names = ",".join(s.get("metadata", {}).get("name", "?") for s in active_skills)
                logger.info(f"选择使用技能: {skill_names}")
                # handle_emit_event("skill_activated", skill_names=skill_names)
                # 将活动技能 ID 存储在状态中以供追踪。
                state["active_skill_ids"] = [s.get("doc_id", "") for s in active_skills]
                state["skill_snippet_names"] = []
                for s in active_skills:
                    for sn in s.get("metadata", {}).get("code_snippets", []):
                        state["skill_snippet_names"].append(sn.get("name", ""))

            if missing_explicit_skills:
                rendered = ", ".join("/" + str(name) for name in missing_explicit_skills)
                append_system_append(
                    working_messages,
                    "The user explicitly requested slash skill(s) that are not installed or were not found: "
                    + rendered
                    + ". Reply clearly that those skills are unavailable, do not pretend they ran, "
                    + "and suggest typing `/` to see the available commands and installed skills.",
                )

        # ----------Step6: 在需要时，于下次模型调用之前压缩上下文----------
        compact_if_needed(state, config)
        working_messages = ensure_working_messages(state, context)

        # ----------Step7: 调用LLM----------
        # 发送通知
        logger.info(f"step_started: {step}")

        args = (working_messages, actions, None)
        kwargs = {}
        response = await handle_llm_complete(
                    args, kwargs, thread, llm, effects, leases, store, platform_info, total_tokens,
                )
        logger.info(f"step_completed: {step}")

        # ----------Step7: 根据类型处理响应----------
        resp_type = response.get("type", "text")

        if resp_type == "text":
            text = response.get("content", "")
            append_message(working_messages, "Assistant", text)

            # Check for FINAL()
            if final_answer := extract_final(text):
                # 返回结果
                return complete_result(state, "completed", final_answer)
            # 检查工具意图提示（V1 语义：连续计数器，
            # 仅在非意图文本响应时重置，不在动作/代码响应时重置）
            if nudge_enabled and consecutive_nudges < max_nudges and signals_tool_intent(text):
                consecutive_nudges += 1
                append_message(
                    working_messages,
                    "User",
                    "你说过要执行某个操作，但没有包含任何工具调用。"
                    "不要仅仅描述你想做什么——现在请实际调用工具。"
                    "请使用 tool_calls 机制来调用相应的工具。",
                )
                continue

            # 在重置 consecutive_nudges 之前检查执行义务。
            # 这确保互斥守卫（consecutive_nudges == 0）
            # 正确反映了本轮是否触发了工具意图提示。
            # 如果工具意图提示触发并耗尽预算，则 consecutive_nudges > 0
            # 且义务被跳过。重置操作在之后执行。
            available_actions = await handle_get_actions(thread, effects, leases, store)
            if (obligation_enabled
                    and consecutive_nudges == 0
                    and len(available_actions) > 0
                    and not state.get("_obligation_resolved", False)
                    and state.get("_obligation_nudge_count", 0) < max_obligation_nudges):
                state["_obligation_nudge_count"] = state.get("_obligation_nudge_count", 0) + 1
                append_message(
                    working_messages,
                    "User",
                    "你被要求执行某个操作，但你仅以文本作出了回应。"
                    "不要描述或解释——现在请调用相应的工具。"
                    "请使用 tool_calls 机制来调用工具。",
                )
                continue

            # 非意图文本响应——重置提示计数器
            if not signals_tool_intent(text):
                consecutive_nudges = 0
            # 纯文本响应——完成
            return complete_result(state, "completed", text)


        elif resp_type == "code":

            state["_obligation_resolved"] = True  # code attempt satisfies obligation

            code = response.get("code", "")

            append_message(working_messages, "Assistant", "```repl\n" + code + "\n```")

            # 在嵌套的 Monty VM 中执行代码。

            result = handle_execute_code_step(code, state)

            # Update persisted state with results

            if result.get("return_value") is not None:
                state["step_" + str(step) + "_return"] = result["return_value"]

                state["last_return"] = result["return_value"]

            for r in result.get("action_results", []):
                state[r.get("action_name", "unknown")] = r.get("output")

            # Format output for next LLM context

            output = format_output(result)

            append_message(working_messages, "User", output)

            # Check for FINAL() in code output

            if result.get("final_answer") is not None:
                __transition_to__("completed", "FINAL() in code")

                return complete_result(state, "completed", result["final_answer"])

            # Check for unified gate pause (new path)

            gate = result.get("pending_gate")

            if gate is None:
                gate = result.get("need_approval")

            if gate is not None and isinstance(gate, dict) and gate.get("gate_paused"):
                __save_checkpoint__(state, {

                    "nudge_count": consecutive_nudges,

                    "consecutive_errors": consecutive_errors,

                    "consecutive_action_errors": consecutive_action_errors,

                    "compaction_count": state.get("compaction_count", 0),

                    "obligation_nudge_count": state.get("_obligation_nudge_count", 0),

                })

                __transition_to__("waiting", "gate paused: " + gate.get("gate_name", "unknown"))

                return {

                    "outcome": "gate_paused",

                    "state": state,

                    "gate_name": gate.get("gate_name", ""),

                    "action_name": gate.get("action_name", ""),

                    "call_id": gate.get("call_id", ""),

                    "parameters": gate.get("parameters", {}),

                    "resume_kind": gate.get("resume_kind", {}),

                }

            # Check for approval or authentication needed (legacy path)

            if result.get("need_approval") is not None:

                approval = result["need_approval"]

                __save_checkpoint__(state, {

                    "nudge_count": consecutive_nudges,

                    "consecutive_errors": consecutive_errors,

                    "consecutive_action_errors": consecutive_action_errors,

                    "compaction_count": state.get("compaction_count", 0),

                    "obligation_nudge_count": state.get("_obligation_nudge_count", 0),

                })

                if approval.get("need_authentication"):
                    __transition_to__("waiting", "authentication needed")

                    return {

                        "outcome": "need_authentication",

                        "state": state,

                        "credential_name": approval.get("credential_name", ""),

                        "action_name": approval.get("action_name", ""),

                        "call_id": approval.get("call_id", ""),

                        "parameters": approval.get("parameters", {}),

                    }

                __transition_to__("waiting", "approval needed")

                return {

                    "outcome": "need_approval",

                    "state": state,

                    "action_name": approval.get("action_name", ""),

                    "call_id": approval.get("call_id", ""),

                    "parameters": approval.get("parameters", {}),

                }

            # Track consecutive errors

            if result.get("had_error"):

                consecutive_errors += 1

                if max_consecutive_errors is not None and consecutive_errors >= max_consecutive_errors:
                    __transition_to__("failed", "too many consecutive errors")

                    return complete_result(

                        state,

                        "failed",

                        error=str(max_consecutive_errors) + " consecutive code errors",

                    )

            else:

                consecutive_errors = 0

            __save_checkpoint__(state, {

                "nudge_count": consecutive_nudges,

                "consecutive_errors": consecutive_errors,

                "consecutive_action_errors": consecutive_action_errors,

                "compaction_count": state.get("compaction_count", 0),

                "obligation_nudge_count": state.get("_obligation_nudge_count", 0),

            })

        elif resp_type == "actions":
            # 动作尝试满足义务要求
            state["_obligation_resolved"] = True
            # 第 0 层：结构化工具调用。
            # 注意：此处不重置 consecutive_nudges（V1 语义）。
            # 只有非意图文本响应会重置计数器。
            calls = response.get("calls", [])

            # 处理作为结构化工具调用发出的 FINAL。FINAL 是 CodeAct 完成哨兵——
            # 当大语言模型尝试通过 tool_calls 而非在代码块内部调用它时，
            # 引擎的动作执行器没有相应的租约，调用将失败。如果 FINAL
            # 与其他调用一同发出，先执行非 FINAL 调用，
            # 以避免持久化副作用被静默丢弃。
            final_call = None
            duplicate_finals_dropped = 0
            executable_calls = []
            for c in calls:
                if c.get("name", "") == "FINAL":
                    # 第一个 FINAL 胜出；多余的将被丢弃（不追加到
                    # executable_calls），以免它们作为普通动作执行
                    # 并因租约错误而失败。
                    if final_call is None:
                        final_call = c
                    else:
                        duplicate_finals_dropped += 1
                    continue
                executable_calls.append(c)

            if duplicate_finals_dropped > 0:
                # 将丢弃操作暴露出来，以便追踪显示执行的 FINAL 数量
                # 少于大语言模型发出的数量的原因。
                logger.info(f"duplicate_final_dropped数量: duplicate_finals_dropped")

            # 追加仅包含可执行调用的助手消息。
            # FINAL 会从 `action_calls` 中被过滤掉，这样消息历史
            # 就不会记录一个没有匹配 ActionResult 的 FINAL 动作，
            # 从而避免在恢复时混淆上下文重放。
            append_message(
                working_messages,
                "Assistant",
                response.get("content", "") or "",
                action_calls=executable_calls,
            )

            # 通过批量主机函数并行执行所有工具调用。
            # Rust 处理预检（租约/策略）、通过 JoinSet 并行执行，
            # 以及按调用顺序发出事件。
            results = await handle_execute_actions_parallel(
                executable_calls, thread, effects, leases, policy, event_tx, gate_controller,
            )
            # 助手消息中的每个工具调用都必须有匹配的 ActionResult，
            # 否则大语言模型 API 会以“未找到函数调用 <id> 的工具输出”为由拒绝该序列。
            # 遍历 executable_calls（而非 results），以覆盖被 Rust
            # 批量处理程序跳过的调用（例如 RequireApproval 提前返回的情况）。
            batch_error_count = 0
            batch_success_count = 0
            for idx in range(len(executable_calls)):
                call = executable_calls[idx]
                call_id = call.get("call_id", "")
                r = results[idx] if idx < len(results) else None
                if r is not None:
                    action_name = r.get("action_name", call.get("name", ""))
                    output = r.get("output")
                    output_str = str(output) if output is not None else "[no output]"
                    if r.get("is_error"):
                        output_str = "[ACTION FAILED] " + action_name + ": " + output_str
                        batch_error_count += 1
                    else:
                        batch_success_count += 1
                else:
                    action_name = call.get("name", "unknown")
                    output_str = "[execution skipped]"
                    batch_error_count += 1
                append_message(
                    working_messages,
                    "ActionResult",
                    output_str,
                    action_name=action_name,
                    action_call_id=call_id,
                )

            # TODO(#2325)：在此处追踪连续动作错误，镜像上面（第 623-634 行）的
            # 代码错误追踪。需要在两条执行路径上统一进度追踪设计。

            # Check results for auth/approval interrupts
            for r_idx, r in enumerate(results):
                if r is None:
                    continue

                if r.get("gate_paused"):
                    # 统一门控暂停（取代单独的 need_approval/need_authentication）
                    args = (state, {
                        "nudge_count": consecutive_nudges,
                        "consecutive_errors": consecutive_errors,
                        "consecutive_action_errors": consecutive_action_errors,
                        "compaction_count": state.get("compaction_count", 0),
                        "obligation_nudge_count": state.get("_obligation_nudge_count", 0),
                    })
                    handle_save_checkpoint(args, {}, thread)
                    gate = r
                    # 从原始调用或结果中获取动作信息。
                    orig_call = executable_calls[r_idx] if r_idx < len(executable_calls) else {}
                    handle_transition_to("waiting", "gate paused: " + gate.get("gate_name", "unknown"), {}, thread)
                    return {
                        "outcome": "gate_paused",
                        "state": state,
                        "gate_name": gate.get("gate_name", ""),
                        "action_name": gate.get("action_name", orig_call.get("name", "")),
                        "call_id": orig_call.get("call_id", ""),
                        "parameters": orig_call.get("params", {}),
                        "resume_kind": gate.get("resume_kind", {}),
                    }

                if r.get("need_authentication"):
                    args = (state, {
                        "nudge_count": consecutive_nudges,
                        "consecutive_errors": consecutive_errors,
                        "consecutive_action_errors": consecutive_action_errors,
                        "compaction_count": state.get("compaction_count", 0),
                        "obligation_nudge_count": state.get("_obligation_nudge_count", 0),
                    })
                    handle_save_checkpoint(args, kwargs, thread)

                    handle_transition_to("waiting", "authentication needed", {}, thread)
                    return {
                        "outcome": "need_authentication",
                        "state": state,
                        "credential_name": r.get("credential_name", ""),
                        "action_name": r.get("action_name", ""),
                        "call_id": r.get("call_id", ""),
                        "parameters": r.get("parameters", {}),
                    }

                if r.get("need_approval"):
                    handle_save_checkpoint(state, {
                        "nudge_count": consecutive_nudges,
                        "consecutive_errors": consecutive_errors,
                        "consecutive_action_errors": consecutive_action_errors,
                        "compaction_count": state.get("compaction_count", 0),
                        "obligation_nudge_count": state.get("_obligation_nudge_count", 0),
                    })
                    handle_transition_to("waiting", "approval needed")
                    return {
                        "outcome": "need_approval",
                        "state": state,
                        "action_name": r.get("action_name", ""),
                        "call_id": r.get("call_id", ""),
                        "parameters": r.get("parameters", {}),
                    }

            if final_call is not None:
                raw_params = final_call.get("params", {})
                # Some LLMs pass FINAL with the answer as a positional string
                # argument instead of a named param dict. Handle that case so
                # the answer is not silently dropped.
                if isinstance(raw_params, str):
                    answer = raw_params
                else:
                    params = raw_params or {}
                    answer = (
                            params.get("answer")
                            or params.get("result")
                            or params.get("value")
                            or params.get("content")
                            or params.get("text")
                    )
                    if not answer:
                        # 回退到助手的文本内容。这可能包含模型的完整解释，
                        # 而非预期的简洁答案——激进截断，以免将数千个
                        # 令牌的推理内容作为最终答案输出，并发出追踪
                        # 事件以便模糊性可见。
                        fallback_content = response.get("content", "") or ""
                        FINAL_FALLBACK_MAX_CHARS = 500
                        truncated = False
                        if len(fallback_content) > FINAL_FALLBACK_MAX_CHARS:
                            fallback_content = (
                                    fallback_content[:FINAL_FALLBACK_MAX_CHARS]
                                    + "… [truncated by orchestrator: FINAL was emitted with no recognizable answer param]"
                            )
                            truncated = True
                        answer = fallback_content
                        __emit_event__(
                            "final_fallback",
                            reason="no recognizable answer param on FINAL",
                            truncated=truncated,
                            original_length=len(response.get("content", "") or ""),
                        )
                handle_transition_to("completed", "FINAL via tool_calls")
                return complete_result(state, "completed", str(answer))

            # Track consecutive action errors (separate from code errors).
            # Partial batch failures: increment only if ALL actions failed,
            # reset if ANY succeeded.
            if batch_success_count > 0:
                consecutive_action_errors = 0
            elif batch_error_count > 0:
                consecutive_action_errors += 1

            if max_consecutive_errors is not None and consecutive_action_errors > 0 and consecutive_action_errors >= max_consecutive_errors + 2:
                __transition_to__("failed", "too many consecutive action errors")
                return complete_result(
                    state,
                    "failed",
                    error=str(consecutive_action_errors) + " consecutive action errors — all recent tool calls failed",
                )
            elif max_consecutive_errors is not None and consecutive_action_errors > 0 and consecutive_action_errors >= max_consecutive_errors:
                append_message(
                    working_messages,
                    "User",
                    "[SYSTEM] Your last " + str(consecutive_action_errors) +
                    " action calls have all failed. You appear to be stuck in a loop. "
                    "Try a completely different approach: use different tools, different "
                    "parameters, or break the problem down differently. If you cannot "
                    "make progress, call FINAL() with an honest explanation of what failed.",
                )

            __save_checkpoint__(state, {
                "nudge_count": consecutive_nudges,
                "consecutive_errors": consecutive_errors,
                "consecutive_action_errors": consecutive_action_errors,
                "compaction_count": state.get("compaction_count", 0),
                "obligation_nudge_count": state.get("_obligation_nudge_count", 0),
            })

    return complete_result(state, "max_iterations")




# ----------辅助函数----------

def format_output(result, max_chars=8000):
    """Format code execution result for the next LLM context message."""
    parts = []

    stdout = result.get("stdout", "")
    if stdout:
        parts.append("[stdout]\n" + stdout)

    for r in result.get("action_results", []):
        name = r.get("action_name", "?")
        output = str(r.get("output", ""))
        if r.get("is_error"):
            parts.append("[" + name + " ERROR] " + output)
        else:
            if len(output) > 500:
                preview = output[:500] + "..."
                parts.append(
                    "[" + name + "] " + preview +
                    "\n(full result stored in state['" + name + "']; "
                    "do NOT retype the data — reference the variable in your next call.)"
                )
            else:
                parts.append("[" + name + "] " + output)

    ret = result.get("return_value")
    if ret is not None:
        parts.append("[return] " + str(ret))

    text = "\n\n".join(parts)

    # Truncate from the front (keep the tail with most recent results)
    if len(text) > max_chars:
        text = "... (truncated) ...\n" + text[-max_chars:]

    if not text:
        text = "[code executed, no output]"

    return text

def strip_quoted_strings(line):
    """
    从一行中移除双引号字符串字面量。
    """
    result = []
    in_quote = False
    prev = ""
    for ch in line:
        if ch == '"' and prev != "\\":
            in_quote = not in_quote
            prev = ch
            continue
        if not in_quote:
            result.append(ch)
        prev = ch
    return "".join(result)

def strip_code_blocks(text):
    """
    移除围栏代码块、缩进代码行和双引号字符串。
    """
    result = []
    in_fence = False
    for line in text.split("\n"):
        trimmed = line.lstrip()
        if trimmed.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("    ") or line.startswith("\t"):
            continue
        result.append(strip_quoted_strings(line))
    return "\n".join(result)

def signals_tool_intent(text):
    """
    检测文本何时表达了调用工具的意图但实际并未执行。
    从 V1 Rust llm_signals_tool_intent() 移植而来：
    移除代码块和引号字符串，检查排除短语，
    然后要求将来时前缀（"let me"、"I'll"、"I will"、"I'm going to"）后紧跟动作动词（"search"、"fetch"、"check" 等）。
    """
    stripped = strip_code_blocks(text)
    lower = stripped.lower()

    EXCLUSIONS = [
        "let me explain", "let me know", "let me think",
        "let me summarize", "let me clarify", "let me describe",
        "let me help", "let me understand", "let me break",
        "let me outline", "let me walk you", "let me provide",
        "let me suggest", "let me elaborate", "let me start by",
    ]
    for exc in EXCLUSIONS:
        if exc in lower:
            return False

    PREFIXES = ["let me ", "i'll ", "i will ", "i'm going to "]
    ACTION_VERBS = [
        "search", "look up", "check", "fetch", "find",
        "read the", "write the", "create", "run the", "execute",
        "query", "retrieve", "add it", "add the", "add this",
        "add that", "update the", "delete", "remove the", "look into",
        "stop", "pause", "cancel", "halt", "disable",
    ]

    for prefix in PREFIXES:
        start = 0
        while True:
            i = lower.find(prefix, start)
            if i < 0:
                break
            after = lower[i + len(prefix):]
            for verb in ACTION_VERBS:
                if after.startswith(verb) or (" " + verb) in after.split("\n")[0]:
                    return True
            start = i + 1

    return False

def complete_result(state, outcome, response=None, error=None, extra=None):
    """Return a standard orchestrator result with persisted state."""
    result = {"outcome": outcome, "state": state}
    if response is not None:
        result["response"] = response
    if error is not None:
        result["error"] = error
    if isinstance(extra, dict):
        for key in extra:
            result[key] = extra[key]
    return result

def extract_final(text):
    """
    从文本中提取 FINAL() 内容。如果未找到则返回 None。
    """
    idx = text.find("FINAL(")
    if idx < 0:
        return None
    after = text[idx + 6:]
    # Handle triple-quoted strings
    for q in ['"""', "'''"]:
        if after.startswith(q):
            end = after.find(q, len(q))
            if end >= 0:
                return after[len(q):end]
    # Handle single/double quoted strings
    if after and after[0] in ('"', "'"):
        quote = after[0]
        end = after.find(quote, 1)
        if end >= 0:
            return after[1:end]
    # Handle balanced parens
    depth = 1
    for i, ch in enumerate(after):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return after[:i]
    return None

def append_message(messages, role, content, action_name=None, action_call_id=None, action_calls=None):
    """Append a normalized message to the working transcript."""
    msg = {"role": role, "content": content}
    if action_name is not None:
        msg["action_name"] = action_name
    if action_call_id is not None:
        msg["action_call_id"] = action_call_id
    if action_calls is not None:
        msg["action_calls"] = action_calls
    messages.append(msg)


# 保守的回退启发式，与旧的 Rust 端估算器匹配。
# 这些必须在 `estimate_context_tokens` 之前定义（因此在下面的 `FINAL(result)` 入口点调用之前）。
# 如果将它们移到入口点之后，每次运行 `compact_if_needed` 时都会产生潜在的 NameError。
CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_CHARS = 4

def estimate_context_tokens(messages):
    """
    使用粗略的字符/令牌启发式方法估算记录的令牌计数。
    """
    total_chars = 0
    for msg in messages:
        total_chars += len(msg.get("content", ""))
        total_chars += len(msg.get("action_name", "") or "")
        total_chars += MESSAGE_OVERHEAD_CHARS
    return (total_chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN

def compact_if_needed(state, config):
    """
    当活动消息历史过大时压缩线程上下文。

    编排器拥有压缩策略。Rust 仅提供令牌估算、显式大语言模型调用以及在生成摘要后替换活动消息骨架的辅助函数。
    """
    if not config.get("enable_compaction", False):
        return False

    context_limit = config.get("model_context_limit", 128000)
    threshold_pct = config.get("compaction_threshold", 0.85)
    threshold = int(context_limit * threshold_pct)
    working_messages = state.get("working_messages")
    if not isinstance(working_messages, list) or not working_messages:
        return False

    current_tokens = estimate_context_tokens(working_messages)
    if current_tokens < threshold:
        return False

    snapshot = list(working_messages)

    history = state.get("history")
    if not isinstance(history, list):
        history = []
        state["history"] = history

    compaction_count = state.get("compaction_count", 0) + 1
    history.append({
        "kind": "compaction",
        "index": compaction_count,
        "tokens_before": current_tokens,
        "messages": snapshot,
    })

    summary_prompt = (
        "Summarize progress so far in a concise but complete way.\n"
        "Include:\n"
        "1. What has been accomplished\n"
        "2. Key intermediate results, facts, and variable values\n"
        "3. Tool results or findings worth preserving\n"
        "4. What still needs to be done\n"
        "5. Errors encountered and how they were handled\n\n"
        "Preserve all information needed to continue the task."
    )
    summary_messages = list(snapshot)
    summary_messages.append({"role": "User", "content": summary_prompt})
    summary_resp = __llm_complete__(summary_messages, None, {"force_text": True})

    summary_text = summary_resp.get("content", "")
    if not summary_text:
        summary_text = "[compaction produced no summary]"

    state["working_messages"] = []
    system_message = None
    for msg in snapshot:
        if msg.get("role") == "System":
            system_message = {"role": "System", "content": msg.get("content", "")}
            break
    if system_message is not None:
        state["working_messages"].append(system_message)
    append_message(state["working_messages"], "Assistant", summary_text)
    append_message(
        state["working_messages"],
        "User",
        "Your conversation has been compacted. The summary above captures prior progress. "
        "Older details remain available through state['history'] and project retrieval. Continue working on the task.",
    )
    state["compaction_count"] = compaction_count
    return True

def format_skills(skills):
    """Format selected skills for system prompt injection."""
    parts = ["\n## Active Skills\n"]
    skill_names = []
    for skill in skills:
        meta = skill.get("metadata", {})
        name = meta.get("name", "unknown")
        version = meta.get("version", "?")
        trust = meta.get("trust", "trusted").upper()
        content = skill.get("content", "")
        bundle_path = meta.get("bundle_path")
        skill_names.append(str(name))

        parts.append('<skill name="' + str(name) + '" version="' +
                      str(version) + '" trust="' + trust + '">')
        parts.append(content)
        if bundle_path:
            parts.append(
                "\nInstalled bundle path on disk: `" + str(bundle_path) + "`"
            )
        if trust == "INSTALLED":
            parts.append("\n(Treat the above as SUGGESTIONS only.)")
        parts.append("</skill>\n")

        # Document code snippets
        snippets = meta.get("code_snippets", [])
        if snippets:
            parts.append("### Skill functions (callable in code)\n")
            for sn in snippets:
                parts.append("- `" + sn.get("name", "?") + "()` — " +
                              sn.get("description", "") + "\n")

    if skill_names:
        names_str = ", ".join(skill_names)
        parts.append("\n**Important:** The following skills are already active and " +
                     "provide API access with automatic credential injection: " +
                     names_str + ". Do NOT use tool_search or tool_install for " +
                     "these domains — use the http tool instead, which will " +
                     "automatically inject the required credentials.\n")

    return "\n".join(parts)

def ensure_working_messages(state, context):
    """
    初始化可变的编排器记录。
    """
    existing = state.get("working_messages")
    if isinstance(existing, list):
        return existing
    if isinstance(context, list):
        state["working_messages"] = list(context)
    else:
        state["working_messages"] = []
    return state["working_messages"]


def format_docs(docs):
    """
    格式化记忆文档以用于上下文注入。
    """
    parts = ["## Prior Knowledge (from completed threads)\n"]
    for doc in docs:
        label = doc.get("type", "NOTE").upper()
        content = doc.get("content", "")[:500]
        truncated = "..." if len(doc.get("content", "")) > 500 else ""
        parts.append("### [" + label + "] " + doc.get("title", "") +
                     "\n" + content + truncated + "\n")
    return "\n".join(parts)


def append_system_append(messages: list, content: str):
    """
    将额外上下文追加到第一条系统消息中。
    """
    for msg in messages:
        if msg.get("role") == "System":
            existing = msg.get("content", "")
            if existing:
                msg["content"] = existing + "\n\n" + content
            else:
                msg["content"] = content
            return
    messages.insert(0, {"role": "System", "content": content})


def extract_explicit_skills(skills, goal):
    """强制激活 `/<skill-name>` 提及并将其自然重写。"""
    if not skills or not goal:
        return [], goal, []

    skill_map = {}
    for skill in skills:
        meta = skill.get("metadata", {})
        name = str(meta.get("name", "")).strip()
        if name:
            skill_map[name.lower()] = skill

    matched = []
    matched_names = set()
    missing = []
    missing_names = set()
    rewritten = goal
    replacements = []

    for match in re.finditer(r'(^|[\s"\(])/(?P<name>[A-Za-z0-9._-]+)(?=$|[\s"\)])', goal):
        name = match.group("name")
        skill = skill_map.get(name.lower())
        if not skill:
            lowered = name.lower()
            if lowered not in missing_names:
                missing.append(name)
                missing_names.add(lowered)
            continue
        meta = skill.get("metadata", {})
        description = str(meta.get("description", "")).strip()
        replacement = description or name.replace("-", " ")
        prefix = match.group(1) or ""
        slash_start = match.start() + len(prefix)
        slash_end = slash_start + 1 + len(name)
        replacements.append((slash_start, slash_end, replacement))
        lowered = name.lower()
        if lowered not in matched_names:
            matched.append(skill)
            matched_names.add(lowered)

    for start, end, replacement in reversed(replacements):
        rewritten = rewritten[:start] + replacement + rewritten[end:]

    return matched, rewritten, missing


# ── 技能选择与注入（可自我修改）────────────────────────

# iOS、macOS 及大多数富文本输入中自动校正产生的智能引号/智能破折号字符。
# 技能激活模式和关键词是使用 ASCII 标点编写的，因此输入的 `I'm a CEO`
# （弯引号 U+2019）会静默地无法匹配 `I'm a CEO`（ASCII U+0027），
# 除非我们在边界处进行归一化。每轮评分前执行一次，因此每个技能都能受益，
# 而无需每个清单都在其正则表达式中拼写备选 `[\u2019']`。
#
# 配对为（排印字符，ASCII 字符）。`str.maketrans` / `.translate()`
# 在 Monty 中不可用，因此我们使用链式 `.replace()` 调用来应用——
# 对于每轮单个目标字符串上的 10 条目表来说足够。

_PUNCT_FOLD = [
    ("\u2018", "'"),  # left single
    ("\u2019", "'"),  # right single / apostrophe (the common autocorrect)
    ("\u201a", "'"),  # low single
    ("\u201b", "'"),  # reversed single
    ("\u201c", '"'),  # left double
    ("\u201d", '"'),  # right double
    ("\u201e", '"'),  # low double
    ("\u201f", '"'),  # reversed double
    ("\u2013", "-"),  # en dash
    ("\u2014", "-"),  # em dash
]


def normalize_punctuation(text):
    """
    将排印引号/破折号折叠为 ASCII 以用于激活匹配。

仅应用于与技能评分时使用的消息，绝不会应用于发送给大语言模型或存储在记忆中的消息。其目标是使模式/关键词匹配能够抵御自动校正的影响，而不是修改用户内容。
    """
    if not text:
        return text
    out = text
    for src, dst in _PUNCT_FOLD:
        out = out.replace(src, dst)
    return out


def select_skills(skills, goal, max_candidates=3, max_tokens=6000):
    """
    使用确定性评分选择相关技能。

    镜像 v1 Rust `ironclaw_skills::selector::prefilter_skills` 的行为：

    1. **评分**：根据消息对每个技能进行评分。设置标记排除在上游 Rust `handle_list_skills` 中处理，因此当技能列表到达此函数时，被排除的技能已经移除。
    2. **排序**：按得分降序排序。
    3. **选择**：在预算和 `max_candidates` 限制内贪婪地选择得分技能。
    4. **链式加载**：从每个被选父技能的 `requires.skills` 中加载配套技能，绕过评分过滤。配套技能随父技能被选中，因此角色/捆绑技能可以在配套技能自身得分不高的情况下仍将其操作配套技能拉入。

    链式加载是**非传递的**（仅深度为 1），以保持行为可预测：被链式加载的配套技能不会再拉入其自身的配套技能。链式加载的技能与评分技能一样遵守相同的预算和 `max_candidates` 上限。
    """
    if not skills or not goal:
        return []

    # Fold typographic quotes/dashes before extraction and scoring so autocorrected
    # user input matches manifests and slash commands.
    normalized_goal = normalize_punctuation(goal)
    explicit, rewritten_goal, _missing = extract_explicit_skills(skills, normalized_goal)
    message_lower = rewritten_goal.lower()
    message_original = rewritten_goal

    # Build name -> skill lookup for chain-loading companion resolution.
    by_name = {}
    for sk in skills:
        meta = sk.get("metadata", {})
        name = meta.get("name")
        if name:
            by_name[str(name)] = sk

    scored = []
    for skill in skills:
        s = score_skill(skill, message_lower, message_original)
        if s > 0:
            scored.append((s, skill))

    scored.sort(key=lambda x: -x[0])

    # Seed with explicitly-activated skills (slash-command mentions) first,
    # so they are guaranteed a slot regardless of keyword score.
    selected = []
    selected_names = set()
    budget = max_tokens

    for skill in explicit:
        if len(selected) >= max_candidates:
            break
        meta = skill.get("metadata", {})
        name = meta.get("name")
        if name is None or str(name) in selected_names:
            continue
        activation = meta.get("activation", {})
        cost = _skill_token_cost(skill, activation)
        if cost > budget:
            continue
        selected.append(skill)
        selected_names.add(str(name))
        budget -= cost

    # Greedy selection with chain-loading. `selected_names` tracks
    # what's already in the result to dedup across explicit, scored,
    # and companion skills.
    for _, parent in scored:
        if len(selected) >= max_candidates:
            break
        parent_meta = parent.get("metadata", {})
        parent_name = parent_meta.get("name")
        if parent_name is None or str(parent_name) in selected_names:
            continue
        parent_activation = parent_meta.get("activation", {})
        parent_cost = _skill_token_cost(parent, parent_activation)
        if parent_cost > budget:
            continue
        selected.append(parent)
        selected_names.add(str(parent_name))
        budget -= parent_cost

        # Chain-load companions (depth 1, non-transitive).
        requires = parent_meta.get("requires", {})
        companion_names = requires.get("skills", [])
        for companion_name in companion_names:
            cname = str(companion_name)
            if len(selected) >= max_candidates:
                break
            if cname in selected_names:
                continue
            companion = by_name.get(cname)
            if companion is None:
                # Listed but not loaded — ignore silently, persona
                # bundles often list optional companions.
                continue
            comp_meta = companion.get("metadata", {})
            comp_activation = comp_meta.get("activation", {})
            comp_cost = _skill_token_cost(companion, comp_activation)
            if comp_cost > budget:
                # Budget exhausted for companions. Parent is still
                # selected; the remaining companions are skipped.
                continue
            selected.append(companion)
            selected_names.add(cname)
            budget -= comp_cost

    return selected


# ----------执行函数----------

def handle_check_signals(
        signal_rx: SignalReceiver,
        thread: Thread,
) -> ExtFunctionResult:
    """检查信号"""
    # 检查取消/暂停信号
    try:
        signal = signal_rx.try_recv()
        if signal is not None:
            return ExtFunctionResult.Return({"signal": str(signal)})
    except Exception:
        pass
    return ExtFunctionResult.Return({"signal": None})


def handle_check_budget(thread: Thread) -> ExtFunctionResult:
    """检查预算"""
    config = thread.config
    # 检查 token 预算
    if config.max_tokens_total is not None and thread.total_tokens_used >= config.max_tokens_total:
        return ExtFunctionResult.Return({"exceeded": True, "reason": "超过 token 限制"})
    # 检查成本预算
    if config.max_budget_usd is not None and thread.total_cost_usd >= config.max_budget_usd:
        return ExtFunctionResult.Return({"exceeded": True, "reason": "超过预算限制"})
    return ExtFunctionResult.Return({"exceeded": False})


async def handle_retrieve_docs(
        goal: str,
        max_docs: int,
        thread: Thread,
        retrieval: Optional[RetrievalEngine] = None,
) -> ExtFunctionResult:
    """查询记忆文档"""
    if retrieval is None:
        return ExtFunctionResult.Return([])

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


async def handle_list_skills(
        thread: Thread,
        store: Optional[Store] = None,
) -> "ExtFunctionResult":
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
    docs.sort(key=lambda d: d.id.value if hasattr(d.id, 'value') else str(d.id))
    seen_ids = set()
    unique_docs = []
    for doc in docs:
        doc_id = doc.id.value if hasattr(doc.id, 'value') else str(doc.id)
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
            logger.debug(
                f"__list_skills__: 排除设置技能 — 标记已存在: "
                f"skill={doc.title}, marker={marker}"
            )
            continue

        skills.append({
            "doc_id": str(doc.id.value) if hasattr(doc.id, 'value') else str(doc.id),
            "title": doc.title,
            "content": doc.content,
            "metadata": doc.metadata,
        })

    return ExtFunctionResult.Return(skills)


def handle_set_active_skills(
        skills_json: List[Any],
        thread: Thread,
) -> "ExtFunctionResult":
    """
    将选定的技能溯源持久化到线程上，以便运行后学习流程
    可以推理出活跃的确切技能版本和代码片段
    """

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

    # 将技能持久化到线程上
    try:
        thread.set_active_skills(skills)
    except Exception as e:
        logger.debug(f"__set_active_skills__: 持久化活跃技能失败: {e}")

    return ExtFunctionResult.Return(None)

def handle_emit_event(
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

async def handle_llm_complete(
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

async def handle_get_actions(
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


import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List, Any

logger = logging.getLogger(__name__)


async def handle_execute_actions_parallel(
        args: List[Any],
        thread: "Thread",
        effects: "EffectExecutor",
        leases: "LeaseManager",
        policy: "PolicyEngine",
        event_tx: Optional[asyncio.Queue] = None,
        gate_controller: "GateController" = None,
) -> "ExtFunctionResult":
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
        return ExtFunctionResult.Error(
            TypeError("__execute_actions_parallel__ 需要调用字典列表")
        )

    if not calls_json:
        return ExtFunctionResult.Return([])

    # 将每个调用字典解析为 (name, params, call_id)
    parsed = []
    for c in calls_json:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        params = c.get("params", {})
        call_id = c.get("call_id", "")
        parsed.append({"name": name, "params": params, "call_id": call_id})

    step_id = StepId()
    actions_context = thread_execution_context(thread, step_id, None, gate_controller)
    active_leases = await leases.active_for_thread(thread.id)

    # 加载动作清单
    inventory = None
    try:
        inventory = await effects.available_action_inventory(active_leases, actions_context)
    except Exception as error:
        logger.debug(
            f"加载编排器并行执行的动作清单失败: "
            f"thread_id={thread.id}, step_id={step_id}, error={error}"
        )

    available_actions = list(inventory.inline) if inventory is not None else []

    # ── 阶段 1：预检（顺序）─────────────────────────
    # 检查租约和策略。拒绝 → 错误结果。批准 → 中断

    preflight = []

    for pc in parsed:
        # 从可调用清单中查找动作定义
        exec_ctx = thread_execution_context(
            thread, step_id, pc["call_id"], gate_controller,
        )
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
            preflight.append({
                "type": "error",
                "result_json": result_json,
                "event": event,
                "output": output,
            })
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
            preflight.append({
                "type": "error",
                "result_json": result_json,
                "event": event,
                "output": output,
            })
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
                preflight.append({
                    "type": "error",
                    "result_json": result_json,
                    "event": event,
                    "output": output,
                })
                continue
            elif isinstance(decision, RequireApproval):
                # 内联门控等待：就地暂停此预检调用，直到用户解决门控。
                # 批准后，进入租约消耗 + 排队执行。
                # 拒绝后，推送 ActionFailed 结果并继续预检，
                # 以便批次其余部分仍然运行
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
                    preflight.append({
                        "type": "error",
                        "result_json": result_json,
                        "event": event,
                        "output": output,
                    })
                    continue
                # 已批准 — 进入租约消耗 + 可运行

        # 原子化地在单个写锁下重新查找 + 消耗一次租约使用，
        # 关闭上面只读 `find_lease_for_action` 和消耗之间的 TOCTOU 窗口
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
            preflight.append({
                "type": "error",
                "result_json": result_json,
                "event": event,
                "output": output,
            })
            continue

        preflight.append({"type": "runnable", "lease": lease})

    # ── 阶段 2：并行执行 ────────────────────────────

    slot_results: List[Optional[dict]] = [None] * len(parsed)
    slot_events: List[Optional[List[EventKind]]] = [None] * len(parsed)
    slot_outputs: List[Optional[dict]] = [None] * len(parsed)

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

        exec_ctx = thread_execution_context(
            thread, step_id, pc["call_id"], gate_controller,
        )
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
        # 这样一个工具的门控不会阻塞批次其余部分
        base_exec_ctx = thread_execution_context(
            thread, step_id, None, gate_controller,
        )
        thread_id_val = thread.id
        user_id_val = thread.user_id

        async def execute_single(idx: int, lease: "CapabilityLease"):
            pc_name = parsed[idx]["name"]
            for a in available_actions:
                if a.matches_name(pc_name):
                    pc_name = a.name
                    break

            pc_params = parsed[idx]["params"]
            pc_call_id = parsed[idx]["call_id"]

            exec_ctx = thread_execution_context(
                thread, step_id, pc_call_id, gate_controller,
            )
            if inventory is not None:
                exec_ctx.available_actions_snapshot = available_actions
                exec_ctx.available_action_inventory_snapshot = inventory

            ps = summarize_params(pc_name, pc_params)
            result_json, events, output, final_lease_id = await execute_single_action_with_inline_retry(
                effects,
                leases,
                pc_name,
                pc_params,
                pc_call_id,
                lease,
                exec_ctx,
                ps,
                thread_id_val,
                user_id_val,
            )
            return (idx, final_lease_id, result_json, events, output)

        tasks = [execute_single(idx, lease) for idx, lease in runnable]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        for result_item in results_list:
            if isinstance(result_item, Exception):
                logger.debug(f"并行动作执行任务异常: {result_item}")
                continue
            idx, _lease_id, result_json, events, output = result_item
            slot_results[idx] = result_json
            slot_events[idx] = events
            slot_outputs[idx] = output

    # ── 阶段 3：按顺序发出事件 ────────────────────────────

    results_json = []
    for idx in range(len(parsed)):
        result_json = slot_results[idx] or {
            "is_error": True,
            "output": {"error": "执行槽为空"},
        }
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


def handle_save_checkpoint(
    args: List[Any],
    kwargs: dict,
    thread: "Thread",
) -> "ExtFunctionResult":
    """处理 `__save_checkpoint__(state, counters)`"""
    # 从第一个参数获取状态数据
    state = args[0] if len(args) > 0 else {}

    # 从第二个参数获取计数器数据
    counters = args[1] if len(args) > 1 else {}

    # 将运行时状态同步到线程
    sync_runtime_state(thread, state)

    # 将检查点数据持久化到线程元数据中
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
