# 第 0 层执行器：结构化工具调用。
#
# 通过委托给 `EffectExecutor` 特质来执行动作调用，并对每次调用检查租约和策略。
#
# 采用两阶段方法：顺序预检（租约/策略检查），然后通过 `JoinSet` 并行执行所有已批准的动作。


import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import time
import logging
from ..capability.lease import LeaseManager
from ..capability.policy import PolicyDecision, PolicyEngine
from ..runtime.messaging import ThreadOutcome
from ..traits.effect import EffectExecutor, ThreadExecutionContext
from ..types.capability import CapabilityLease
from ..types.error import EngineError
from ..types.event import EventKind
from ..types.step import ActionCall, ActionResult
from ..types.thread import Thread

logger = logging.getLogger(__name__)


# ── 动作批量结果 ─────────────────────────────────────────────

@dataclass
class ActionBatchResult:
    """执行一批动作调用的结果"""
    # 每个动作调用的结果（按顺序）
    results: List[ActionResult]
    # 执行期间生成的事件
    events: List[EventKind]
    # 如果设置，执行被中断且线程需要批准
    need_approval: Optional[ThreadOutcome] = None


# ── 预检结果 ─────────────────────────────────────────────────

class PreflightOutcome:
    """单个动作调用的预检结果"""
    pass


@dataclass
class Runnable(PreflightOutcome):
    """动作通过预检 — 准备并行执行"""
    index: int
    lease: CapabilityLease


@dataclass
class PreflightError(PreflightOutcome):
    """动作被拒绝或没有租约 — 已生成错误结果"""
    index: int
    result: ActionResult
    event: EventKind


# ── 执行动作调用 ─────────────────────────────────────────────

async def execute_action_calls(
        calls: List[ActionCall],
        thread: Thread,
        effects: EffectExecutor,
        leases: LeaseManager,
        policy: PolicyEngine,
        context: ThreadExecutionContext,
        capability_policies: List[PolicyRule],
) -> ActionBatchResult:
    """使用第 0 层（结构化）方法执行一批动作调用

    两阶段执行：
    1. **预检**（顺序）：对于每个调用，查找租约并检查策略。
       被拒绝的调用立即产生错误结果。RequireApproval 中断整个批次。
    2. **执行**（并行）：所有已批准的调用通过 JoinSet 并发运行。
       结果按原始调用顺序收集和合并
    """
    preflight_results = []
    early_events = []
    active_leases = await leases.active_for_thread(thread.id)
    available_inventory = await effects.available_action_inventory(active_leases, context)
    available_actions = available_inventory.inline

    # ── 阶段 1：预检（顺序）─────────────────────────
    # 检查每个调用的租约和策略。RequireApproval 立即中断整个批次。
    # 被拒绝/无租约的调用变为错误结果

    for idx, call in enumerate(calls):
        # 1. 从可调用清单中查找动作定义
        action_def = None
        for action in available_actions:
            if action.matches_name(call.action_name):
                action_def = action
                break

        if action_def is None:
            error = f"动作 '{call.action_name}' 在此执行上下文中不可调用"
            error_result = ActionResult(
                call_id=call.id,
                action_name=call.action_name,
                output={"error": error},
                is_error=True,
                duration_ms=0,
            )
            event = EventKind.ActionFailed(
                step_id=context.step_id,
                action_name=call.action_name,
                call_id=call.id,
                error=str(error_result.output.get("error", "动作在此执行上下文中不可调用")),
                duration_ms=0,
                params_summary=summarize_params(call.action_name, call.parameters),
            )
            preflight_results.append(PreflightError(index=idx, result=error_result, event=event))
            continue

        # 2. 查找此动作的租约（用于策略检查的只读查找）
        lease = await leases.find_lease_for_action(thread.id, call.action_name)
        if lease is None:
            error_result = ActionResult(
                call_id=call.id,
                action_name=call.action_name,
                output={"error": f"没有活跃租约覆盖动作 '{call.action_name}'"},
                is_error=True,
                duration_ms=0,
            )
            event = EventKind.ActionFailed(
                step_id=context.step_id,
                action_name=call.action_name,
                call_id=call.id,
                error=f"动作 '{call.action_name}' 没有租约",
                duration_ms=0,
                params_summary=summarize_params(call.action_name, call.parameters),
            )
            preflight_results.append(PreflightError(index=idx, result=error_result, event=event))
            continue

        # 3. 检查可调用动作的策略
        decision = policy.evaluate(action_def, lease, capability_policies)
        if isinstance(decision, Deny):
            error_result = ActionResult(
                call_id=call.id,
                action_name=call.action_name,
                output={"error": f"被拒绝: {decision.reason}"},
                is_error=True,
                duration_ms=0,
            )
            event = EventKind.ActionFailed(
                step_id=context.step_id,
                action_name=call.action_name,
                call_id=call.id,
                error=decision.reason,
                duration_ms=0,
                params_summary=summarize_params(call.action_name, call.parameters),
            )
            preflight_results.append(PreflightError(index=idx, result=error_result, event=event))
            continue
        elif isinstance(decision, RequireApproval):
            # 内联门控等待：就地暂停此预检循环，直到用户解决门控。
            # 批准后，进入租约消耗并将调用加入执行队列。
            # 拒绝后，标记调用失败并继续批次其余部分的预检 —
            # 与策略 Deny 的影响范围相同
            #
            # 控制器在上下文中是必需的。不暂停的代码路径提供
            # `CancellingGateController`，它在此处表现为类型化拒绝
            #
            # 策略不携带 `allow_always` 轴；默认为历史值（`true`），
            # 以便 UI 提供该选项
            resume_kind = ResumeKind.Approval(allow_always=True)
            early_events.append(EventKind.ApprovalRequested(
                action_name=call.action_name,
                call_id=call.id,
                parameters=call.parameters,
                description=None,
                allow_always=True,
                gate_name="approval",
                params_summary=summarize_params(call.action_name, call.parameters),
            ))
            resolution = await context.gate_controller.pause(GatePauseRequest(
                thread_id=thread.id,
                user_id=thread.user_id,
                gate_name="approval",
                action_name=call.action_name,
                call_id=call.id,
                parameters=call.parameters,
                resume_kind=resume_kind,
                conversation_id=context.conversation_id,
            ))

            denial = denial_outcome_for_resolution(resolution)
            if denial is not None:
                error_msg = denial.event_error()
                error_result = ActionResult(
                    call_id=call.id,
                    action_name=call.action_name,
                    output={"error": error_msg},
                    is_error=True,
                    duration_ms=0,
                )
                event = EventKind.ActionFailed(
                    step_id=context.step_id,
                    action_name=call.action_name,
                    call_id=call.id,
                    error=error_msg,
                    duration_ms=0,
                    params_summary=summarize_params(call.action_name, call.parameters),
                )
                preflight_results.append(PreflightError(index=idx, result=error_result, event=event))
                continue
            # 已批准：进入租约消耗 + 可运行队列

        # 4. 原子化地在单个写锁下查找 + 消耗一次租约使用。
        # 这避免了 TOCTOU 竞态条件，即并发调用可能
        # 在我们的只读查找（步骤 1）和此消耗之间耗尽租约
        lease = await leases.find_and_consume(thread.id, call.action_name)
        preflight_results.append(Runnable(index=idx, lease=lease))

    # ── 阶段 2：执行（并行）─────────────────────────────
    # 所有已批准的调用并发运行。结果收集在以原始索引为键的
    # 字典中，然后按顺序合并

    # 将可运行项与预检错误分开。每个槽携带调用的
    # 终端 `(ActionResult, EventKind)` 以及内联重试助手
    # 发出的任何预终端 `ApprovalRequested` 事件。
    # 在合并阶段，预终端事件在终端事件之前刷新，
    # 以便审计观察者按顺序看到"批准请求 → 动作执行/失败"
    slot_results: List[Optional[tuple]] = [None] * len(calls)
    runnable_indices = []

    for pf in preflight_results:
        if isinstance(pf, PreflightError):
            slot_results[pf.index] = (pf.result, pf.event, [])
        elif isinstance(pf, Runnable):
            runnable_indices.append((pf.index, pf.lease))

    # 短路：单个可运行调用 — 直接执行，无 JoinSet 开销
    if len(runnable_indices) == 1:
        idx, lease = runnable_indices[0]
        call = calls[idx]
        exec_ctx = _stamp_execution_context(context, call.id, available_actions, available_inventory)
        execution_start = time.monotonic()
        exec_result, pre_events = await execute_with_inline_gate_retry(
            effects, leases, lease, call, exec_ctx, thread.id, thread.user_id,
        )
        if interrupted_call_needs_refund(exec_result):
            await leases.refund_use(lease.id)
        execution_duration_ms = int((time.monotonic() - execution_start) * 1000)
        result, event = classify_exec_result(exec_result, call, exec_ctx, execution_duration_ms)
        slot_results[idx] = (result, event, pre_events)
    elif len(runnable_indices) > 1:
        # 多个调用：通过 asyncio 任务并行执行。每个任务将调用包装在
        # `execute_with_inline_gate_retry` 中，这样在中间执行时引发
        # `Approval` 的工具通过共享的桥接控制器内联暂停
        # （该控制器按（用户，线程）序列化并发门控），
        # 并在批准时重试或显示类型化拒绝 — 与单调用快速路径相同的契约。
        # 没有此包装器，并行批次会回退到旧版 `gate_paused` 哨兵 +
        # 线程重新进入路径，重新引入同一批次中任何已完成同级调用的重复执行错误
        thread_id = thread.id
        user_id = thread.user_id

        tasks = []
        for idx, lease in runnable_indices:
            call = calls[idx]
            ctx = _stamp_execution_context(context, call.id, available_actions, available_inventory)

            async def task(_idx=idx, _lease=lease, _call=call, _ctx=ctx):
                execution_start = time.monotonic()
                result, pre_events = await execute_with_inline_gate_retry(
                    effects, leases, _lease, _call, _ctx, thread_id, user_id,
                )
                execution_duration_ms = int((time.monotonic() - execution_start) * 1000)
                return (_idx, _lease.id, result, pre_events, _call, _ctx, execution_duration_ms)

            tasks.append(task())

        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        for result_item in results_list:
            if isinstance(result_item, Exception):
                logger.debug(f"并行工具执行任务异常: {result_item}")
                continue
            idx, lease_id, result, pre_events, call, ctx, execution_duration_ms = result_item
            if interrupted_call_needs_refund(result):
                await leases.refund_use(lease_id)
            action_result, event = classify_exec_result(result, call, ctx, execution_duration_ms)
            slot_results[idx] = (action_result, event, pre_events)

    # ── 阶段 3：按原始调用顺序合并结果 ────────────

    results = []
    # `early_events` 携带预检期间发出的 ApprovalRequested 事件；
    # 它们在每个调用结果事件*之前*发出，以便观察者按正确顺序
    # 看到"批准请求 → 动作失败"
    events = list(early_events)
    first_interrupt = None

    for idx, slot in enumerate(slot_results):
        if slot is not None:
            result, event, pre_events = slot
            # 记录第一个门控暂停作为批次中断，但仍然收集所有其他结果
            if (first_interrupt is None
                    and isinstance(event, EventKind.ApprovalRequested)
                    and result.output.get("status") == "gate_paused"):
                gate_name = result.output.get("gate", "unknown")
                call = calls[idx]
                resume_kind_data = result.output.get("resume_kind", {"Approval": {"allow_always": False}})
                first_interrupt = ThreadOutcome.GatePaused(
                    gate_name=gate_name,
                    action_name=event.action_name,
                    call_id=event.call_id,
                    parameters=call.parameters,
                    resume_kind=ResumeKind.from_dict(resume_kind_data),
                    resume_output=result.output.get("resume_output"),
                    paused_lease=result.output.get("paused_lease"),
                )
            # 内联重试助手的预终端 `ApprovalRequested` 事件首先出现，
            # 以便审计日志按顺序读取为"批准请求 → 动作 <结果>"
            for pe in pre_events:
                events.append(pe)
            results.append(result)
            events.append(event)

    return ActionBatchResult(
        results=results,
        events=events,
        need_approval=first_interrupt,
    )


def _stamp_execution_context(
        context: ThreadExecutionContext,
        call_id: str,
        available_actions: List[ActionDef],
        available_inventory: ActionInventory,
) -> ThreadExecutionContext:
    """为执行上下文添加当前调用信息"""
    exec_ctx = context.clone()
    exec_ctx.current_call_id = call_id
    exec_ctx.available_actions_snapshot = available_actions
    exec_ctx.available_action_inventory_snapshot = available_inventory
    return exec_ctx


def classify_exec_result(
        result: Any,
        call: ActionCall,
        context: ThreadExecutionContext,
        execution_duration_ms: int,
) -> tuple:
    """将执行结果分类为 `(ActionResult, EventKind)` 对

    由单调用快速路径和并行 JoinSet 路径共同使用以生成统一的输出
    """
    if isinstance(result, ActionResult):
        action_result = result
        action_result.call_id = call.id
        # 效果适配器将工具错误包装为 `ActionResult(is_error=True)`
        # — 在这种情况下发出 ActionFailed，以便追踪和下游观察者
        # 看到失败而不是将其视为成功
        if action_result.is_error:
            error_msg = action_result.output.get("error", str(action_result.output))
            duration_ms = action_result.duration_ms if action_result.duration_ms > 0 else execution_duration_ms
            event = EventKind.ActionFailed(
                step_id=context.step_id,
                action_name=call.action_name,
                call_id=call.id,
                error=error_msg,
                duration_ms=duration_ms,
                params_summary=summarize_params(call.action_name, call.parameters),
            )
        else:
            event = EventKind.ActionExecuted(
                step_id=context.step_id,
                action_name=call.action_name,
                call_id=call.id,
                duration_ms=action_result.duration_ms,
                params_summary=summarize_params(call.action_name, call.parameters),
            )
        return (action_result, event)
    elif isinstance(result, EngineError) and result.error_type == "GatePaused":
        gate_name = result.gate_name
        action_name = result.action_name
        call_id = result.call_id
        parameters = result.parameters
        resume_kind = result.resume_kind
        resume_output = result.resume_output
        paused_lease = result.paused_lease

        error_result = ActionResult(
            call_id=call.id,
            action_name=call.action_name,
            output={
                "status": "gate_paused",
                "gate": gate_name,
                "resume_kind": resume_kind.to_dict() if resume_kind else {},
                "resume_output": resume_output,
                "paused_lease": paused_lease,
            },
            is_error=True,
            duration_ms=0,
        )
        allow_always = resume_kind.allow_always if hasattr(resume_kind, 'allow_always') else None
        event = EventKind.ApprovalRequested(
            action_name=action_name,
            call_id=call_id,
            parameters=parameters,
            description=None,
            allow_always=allow_always,
            gate_name=gate_name,
            params_summary=summarize_params(call.action_name, parameters),
        )
        return (error_result, event)
    else:
        # 其他错误
        error_msg = str(result)
        error_result = ActionResult(
            call_id=call.id,
            action_name=call.action_name,
            output={"error": error_msg},
            is_error=True,
            duration_ms=0,
        )
        event = EventKind.ActionFailed(
            step_id=context.step_id,
            action_name=call.action_name,
            call_id=call.id,
            error=error_msg,
            duration_ms=execution_duration_ms,
            params_summary=summarize_params(call.action_name, call.parameters),
        )
        return (error_result, event)


def interrupted_call_needs_refund(result: Any) -> bool:
    """检查中断的调用是否需要退还租约使用次数"""
    return isinstance(result, EngineError) and result.error_type == "GatePaused"


async def execute_with_inline_gate_retry(
        effects: EffectExecutor,
        leases: LeaseManager,
        lease: CapabilityLease,
        call: ActionCall,
        exec_ctx: ThreadExecutionContext,
        thread_id: ThreadId,
        user_id: str,
) -> tuple:
    """使用内联门控等待重试运行单个工具动作

    如果执行器返回 `EngineError::GatePaused { resume_kind: Approval, .. }`，
    退还租约，通过上下文控制器暂停等待用户，并在批准时重试。
    拒绝/取消时，显示为拒绝风格的 `EngineError::Effect`，
    以便调用者生成 `ActionFailed` 事件而不是 "gate_paused" 哨兵

    受 `MAX_INLINE_GATE_RETRIES` 限制：行为异常的工具在每次批准后
    持续门控会导致清晰的错误，而不是占用 CPU。
    桥接在交付解决方案之前安装自动批准，
    因此行为良好的链在 1-2 次迭代内收敛，上限仅在错误时达到

    认证恢复类型现在也流经此循环 —
    `bridge::resolve_inline_gates_for_credential`（来自 #3133 half-2 的 OAuth 回调钩子）
    在凭证到达密钥存储时立即将 `GateResolution::Approved` 传递给暂停的控制器，
    因此重试会看到凭证且动作成功。
    （`bridge::resume_paused_missions_for_credential` 是子线程暂停的任务的并行路径 —
    与此循环驱动的内联等待等待者分开。）
    外部恢复类型仍保留旧版重新进入路径：它们的解决方案安装回调负载状态，
    暂停的调用在不展开的情况下无法看到

    返回 `(final_result, events)`，其中 `events` 携带跨重试迭代发出的
    `ApprovalRequested` 审计事件 — 每个门控暂停周期一个，按触发顺序排列。
    调用者必须在每个调用结果事件之前发出这些事件，
    以便重放/审计观察者看到"批准请求 → 动作 <结果>"而不仅仅是最终结果
    """
    MAX_INLINE_GATE_RETRIES = 3

    current_lease = lease
    # `call_ctx` 跨重试携带一次性批准标志。
    # 第一次迭代：false（门控尚未触发）。每次批准后我们设置为 true；
    # 调用后立即重置为 false，这样重新门控的工具不会在一次批准中获得两次标志
    call_ctx = exec_ctx.clone()
    emitted_events = []

    for _ in range(MAX_INLINE_GATE_RETRIES):
        result = await effects.execute_action(
            call.action_name,
            call.parameters,
            current_lease,
            call_ctx,
        )
        call_ctx.call_approval_granted = False

        # 快照原始门控（用于取消+认证时的重新发出，见下文）
        original_err = None
        if (isinstance(result, EngineError)
                and result.error_type == "GatePaused"
                and result.resume_kind.type in ("Approval", "Authentication")):
            original_err = result

        if not (isinstance(result, EngineError)
                and result.error_type == "GatePaused"
                and result.resume_kind.type in ("Approval", "Authentication")):
            return (result, emitted_events)

        gate_name = result.gate_name
        action_name = result.action_name
        call_id = result.call_id
        parameters = result.parameters
        resume_kind = result.resume_kind
        resume_output = result.resume_output

        # 在等待控制器之前发出审计事件，以便即使
        # 用户从未解决，观察者也能看到请求。
        # 镜像编排器（第 1 层）路径，该路径在调用 `pause()` 之前记录事件
        allow_always = resume_kind.allow_always if resume_kind.type == "Approval" else None
        emitted_events.append(EventKind.ApprovalRequested(
            action_name=action_name,
            call_id=call_id,
            parameters=parameters,
            description=None,
            allow_always=allow_always,
            gate_name=gate_name,
            params_summary=summarize_params(call.action_name, parameters),
        ))

        # 退还此尝试消耗的租约使用次数；如果用户批准，我们将在重试时重新消耗。
        # 例外：当 `resume_output` 设置时，动作已经成功执行
        # （门控是携带缓存输出的执行后认证门控）—
        # 下面的缓存输出分支将返回而不重新消耗。
        # 现在退还将使成功动作的租约使用次数净为零，
        # 让有副作用的工具免费耗尽 `max_uses=∞`。
        # 参见 scripting.rs 中的 `drive_inline_gate` 和
        # orchestrator.rs 中的 `execute_action_with_inline_gate` 的匹配保护。
        # 由 #3559 安全审查跟踪
        if resume_output is None:
            await leases.refund_use(current_lease.id)

        resolution = await exec_ctx.gate_controller.pause(GatePauseRequest(
            thread_id=thread_id,
            user_id=user_id,
            gate_name=gate_name,
            action_name=action_name,
            call_id=call_id,
            parameters=parameters,
            resume_kind=resume_kind,
            conversation_id=exec_ctx.conversation_id,
        ))

        denial = denial_outcome_for_resolution(resolution)
        if denial is not None:
            # 取消+认证 → 展开到旧版 `ThreadOutcome::GatePaused`，
            # 这样任务/非内联感知控制器仍然可以显示暂停状态。
            # 此处的取消意味着控制器无法内联解析认证
            # （例如测试中的 `CancellingGateController`，或没有 OAuth 接线的 BridgeGateController）—
            # 这在语义上是"不存在内联路径"，旧版展开是正确的回退。
            # 拒绝/显式用户取消仍然是失败
            if (isinstance(resolution, GateResolution)
                    and resolution.type == "Cancelled"
                    and resume_kind.type == "Authentication"
                    and original_err is not None):
                return (original_err, emitted_events)
            return (EngineError(Effect=denial.effect_reason()), emitted_events)

        # 已批准。如果桥接在引发此门控之前缓存了动作的输出
        # （执行后认证门控路径 — 参见 `effect_adapter::auth_gate_from_extension_result`），
        # 动作已经运行，我们只需要用户侧解决方案。
        # 跳过重新执行并从缓存的输出合成成功的 ActionResult。
        # 镜像第 1 层在 `scripting::drive_inline_gate` 中的快捷方式。由 #3533 跟踪。
        #
        # 不要在此处发出 `ActionExecuted`。调用者将我们返回的
        # `ActionResult` 包装在 `classify_exec_result` 中，该函数为 Ok 分支
        # 发出终端 `ActionExecuted`。在此处发出会为一个动作产生两个
        # `ActionExecuted` 事件，混淆审计观察者。
        # （第 1 层的 `scripting::drive_inline_gate` 和第 1 层替代的
        # `orchestrator::execute_action_with_inline_gate` 自己发出，
        # 因为它们的调用者不运行 Ok 分支分类器 — 参见 #3559 审查了解
        # 为什么结构化在这里是异常值）
        if resume_output is not None:
            return (ActionResult(
                call_id=call_id,
                action_name=action_name,
                output=resume_output,
                is_error=False,
                duration_ms=0,
            ), emitted_events)

        # 重新消耗一次租约使用，并标记下一次调用为预批准，
        # 以便主机的 `EffectExecutor` 跳过其批准检查
        new_lease = await leases.find_and_consume(thread_id, call.action_name)
        if isinstance(new_lease, EngineError):
            return (EngineError(Effect=f"批准后租约耗尽: {new_lease}"), emitted_events)

        current_lease = new_lease
        call_ctx.call_approval_granted = True
        continue

    # 重试预算耗尽。最后一次循环迭代以成功的 `find_and_consume` 结束，
    # 其租约从未被使用 — 在返回之前退还它，
    # 这样行为异常的工具无法在多次批准中缓慢耗尽 `max_uses`。
    # 尽力而为；如果租约已被撤销/过期，退款是无操作的
    await leases.refund_use(current_lease.id)
    return (EngineError(
        Effect=f"工具 '{call.action_name}' 在 {MAX_INLINE_GATE_RETRIES} 次重试后仍然需要批准"
    ), emitted_events)