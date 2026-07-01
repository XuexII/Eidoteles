from __future__ import annotations

from enum import Enum

from engine import (
    ThreadId,
    Capability,
    CapabilityRegistry,
    ConversationManager,
    EffectExecutor,
    LeaseManager,
    MissionManager,
    PolicyEngine,
    Project,
    Store,
    ThreadConfig,
    ThreadManager,
    ThreadOutcome,
)

from ironclaw_common import AppEvent
from engine.types import (is_shared_owner, shared_owner_id)

from agent import Agent, augment_with_attachments
from auth.extension import AuthManager
from bridge.effect_adapter import EffectBridgeAdapter
from bridge.engine_actions import mission_capability_actions
from bridge.llm_adapter import LlmBridgeAdapter
from bridge.store_adapter import HybridStore
from channels.web import GATEWAY_CHANNEL_NAME
from channels.web.sse import SseManager
from channels import (IncomingMessage, OutgoingResponse, StatusUpdate)
from db import Database
from error import Error
from extensions.naming import legacy_extension_alias
from gate.pending import (PendingGate, PendingGateKey)
from gate.store import PendingGateStore, GateStoreError
from engine.gate import (
    # 用户对于gage的处理结果
    GateResolution,
    GateResolutionApproved,
    GateResolutionDenied,
    GateResolutionDenied,
    GateResolutionCredentialProvided,
    GateResolutionCancelled,
    GateResolutionExternalCallback,

    # gate的处理方式
    ResumeKind,
    ResumeKindApproval,
    ResumeKindAuthentication,
    ResumeKindExternal,
    resume_kind_name
)
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Set, Optional, Union, Any
import os
import regex as re
from datetime import datetime, timezone
from pathlib import Path
import uuid
import json
import asyncio
from llm import user_signals_execution_intent
import asyncio

logger = logging.getLogger(__name__)


# ----------数据结构: 定义引擎状态---------
# 职责说明:
#   1. 跨消息持久化的引擎状态
#   2. 定义全局的引擎状态 —— ENGINE_STATE，并用异步锁包含

@dataclass(kw_only=True)
class EngineState:
    """跨消息持久化的引擎状态。"""
    thread_manager: ThreadManager
    conversation_manager: ConversationManager
    effect_adapter: EffectBridgeAdapter
    store: Store
    default_project_id: ProjectId
    # 统一待处理门控存储——按 (user_id, thread_id) 键控
    pending_gates: PendingGateStore
    # 用于向 Web 网关广播 AppEvents 的 SSE 管理器
    sse: Optional[SseManager] = None
    # 用于写入对话消息的 V1 数据库（网关从此处读取）
    db: Optional[Database] = None
    # 用于在认证流后存储凭证的密钥存储
    secrets_store: Optional[SecretsStore] = None
    # 用于设置指令查找和凭证检查的集中式认证管理器
    auth_manager: Optional[AuthManager] = None
    # 当没有认证管理器时，用于扩展支持的认证/设置的扩展管理器
    extension_manager: Optional[ExtensionManager] = None
    # 项目本地附件持久化的文件系统根目录
    project_root: Path

    # 调用者提供的外部工具的每线程目录（Responses API）。
    # 通过 Arc 克隆与 effect adapter（在操作列表和分派期间读取）
    # 和 Responses API 处理程序（在将请求发送到代理循环之前写入）共享。
    external_tool_catalog: ExternalToolCatalog = None
    # 引擎 v2 能力注册表。保存在此处（除了 `effect_adapter` 的内部句柄），
    # 以便 Responses API 处理程序可以枚举内部操作名称并拒绝会遮蔽它们的调用者提供的工具。
    capability_registry: CapabilityRegistry = None
    # 内联门控等待控制器。让引擎在 `Approval` 和 `Authentication` 门控上
    # 原地暂停 Tier 0 和 Tier 1 执行，而不是回退到编排器并在恢复时重新进入
    # （这会重新执行先前的非幂等工具调用）。
    gate_controller: BridgeGateController = None
    # 进程范围内的进行中门控解析通道注册表。
    # 与 `gate_controller` 一起保存，以便 OAuth 回调路径可以按凭证名称
    # 唤醒暂停的 Authentication 等待者，而无需通过控制器的内部机制。
    gate_resolutions: GateResolutions = None


# 全局引擎状态，首次使用时初始化
# 为了确保 Engine v2 状态的线程安全访问和单例初始化
ENGINE_STATE: Optional[EngineState] = None
ENGINE_STATE_LOCK: asyncio.Lock | None = None


# ----------流程1: 初始化引擎---------
# 职责说明:
#   1. 使用代理的依赖项获取或初始化引擎状态

async def init_engine(agent: Agent) -> None:
    pass


# ----------流程2: 解析待处理gate----------
# 职责说明:
#   1. 解析用户有哪些待处理的gate

class PendingGateResolutionKind(Enum):
    # 没有需要处理的gate
    NONE = "none"
    # 有需要处理的gate
    Resolved = "resolved"
    # 有多个gate，不明确
    Ambiguous = "ambiguous"


@dataclass
class PendingGateResolution:
    """
    待处理gate的解析结果
    """
    kind: PendingGateResolutionKind
    # 需要处理的gata，每次仅处理一个
    gate: Optional[PendingGate] = None

async def resolve_pending_gate_for_user(
        pending_gates: PendingGateStore,
        user_id: str,
        thread_id_hint: Optional[str],
) -> PendingGateResolution:
    """
    解析用户的待处理门控。

    根据 thread_id_hint 过滤候选门控：
    - 优先匹配 scope_thread_id
    - 回退到匹配 thread_id 或 conversation_id 的 UUID
    - 如果有多个候选，选择最近创建的
    - 如果有多个候选但没有有效的提示 UUID，返回 Ambiguous

    :parameter pending_gates: 待处理gate存储器
    :parameter user_id: 用户id
    :parameter thread_id_hint:
    """
    hinted_uuid = parse_scope_uuid(thread_id_hint)
    hinted_scope = thread_id_hint

    candidates: List[PendingGate] = []
    for gate in await pending_gates.list_for_user(user_id):
        if hinted_scope is None:
            candidates.append(gate)
        # 优先匹配 scope_thread_id
        elif gate.scope_thread_id == hinted_scope:
            candidates.append(gate)
        # 回退到匹配 thread_id 或 conversation_id
        elif hinted_uuid and (gate.thread_id == hinted_uuid or gate.conversation_id == hinted_uuid):
            candidates.append(gate)

    if len(candidates) == 0:
        resolved_kind = PendingGateResolutionKind.NONE
        resolved_gate = None
    elif len(candidates) == 1:
        resolved_kind = PendingGateResolutionKind.Resolved
        resolved_gate = candidates[0]
    elif hinted_uuid is not None:
        # 多个候选，选择最近创建的
        resolved_kind = PendingGateResolutionKind.Resolved
        resolved_gate = max(candidates, key=lambda gate: gate.created_at)
    else:
        # 否则，返回 Ambiguous
        resolved_kind = PendingGateResolutionKind.Ambiguous
        resolved_gate = None

    return PendingGateResolution(kind=resolved_kind, gate=resolved_gate)


# ----------流程3: 验证解析待处理gate----------
# 职责说明:
#   1. 原子化验证和移除代处理gate
#   2. 内联等待快速路径
#   3. 根据 GateResolution 枚举执行不同逻辑

async def resolve_gate(
        agent: Agent,
        message: IncomingMessage,
        thread_id: ThreadId,
        request_id: str,
        resolution: GateResolution,
) -> BridgeOutcome:
    """
    解析统一的待处理门控。

    这是解析存储在 [`PendingGateStore`] 中的门控的单一入口点。
    它在恢复或停止线程之前原子性地验证 request_id、通道授权和过期时间。

    使用统一门控抽象替换了新代码路径的单独批准和认证解析路径。

    Args:
        agent: Agent
        message: IncomingMessage
        thread_id: ThreadId
        request_id: str
        resolution: 用户对于gage的处理结果
    """
    await init_engine(agent)
    if ENGINE_STATE_LOCK is None:
        raise RuntimeError("init", "引擎状态未初始化")
    async with ENGINE_STATE_LOCK:
        state = ENGINE_STATE
        if state is None:
            raise RuntimeError("init", "引擎状态为空")

        # 按 `(user_id, thread_id)` 键控——每个线程恰好一个待处理门控
        key = PendingGateKey(user_id=message.user_id, thread_id=thread_id)

        # 在验证所有不变量后原子性地从PendingGateStore中取出gate
        try:
            pending = await state.pending_gates.take_verified(
                key, request_id, message.channel
            )
            # TODO 直接返回错误，暂不区分类型
        except Exception as e:
            raise e

        # 内联门控等待快速路径：如果引擎正在主动等待此门控
        # （活动 Tier 0 批处理或 Tier 1 CodeAct VM），则通过控制器的
        # 内存通道将解析返回。引擎从确切的暂停点继续——
        # 无需重新进入、无需重放、无需双重执行同一步骤中先前的非幂等工具调用。
        #
        # 我们仍然在投递之前安装任何自动批准首选项，以便同一执行中的
        # 后续门控看到策略 `Allow` 而不是再次门控。
        if isinstance(resolution, (GateResolutionApproved, GateResolutionDenied, GateResolutionCancelled)):
            # 用户是否永久批准
            always_for_inline = (
                clamp_always_to_resume_kind(resolution.always, pending.resume_kind)
                if isinstance(resolution, GateResolutionApproved)
                else False
            )

            # 将'_'替换为 "-"
            legacy_registry_name = legacy_extension_alias(pending.action_name)
            prior_permission = None
            if always_for_inline:
                # 将工具标记为自动批准（用户表示“始终允许”）
                await state.effect_adapter.auto_approve_tool(pending.action_name)
                if legacy_registry_name is not None:
                    await state.effect_adapter.auto_approve_tool(legacy_registry_name)

                # 当用户点击"始终批准"时，将 `AlwaysAllow` 持久化到数据库
                prior_permission = await persist_always_allow(agent, state, pending)

            inline_resolution = resolution
            if isinstance(resolution, GateResolutionApproved):
                inline_resolution = GateResolutionApproved(always=always_for_inline)

            # 将解析结果转发到内联等待注册表中。如果引擎正在主动等待它，则返回 true
            if await state.gate_controller.try_deliver(request_id, inline_resolution):
                if state.sse is not None:
                    if isinstance(resolution, GateResolutionApproved):
                        label = "approved_always" if always_for_inline else "approved"
                        status_msg = "门控已批准。正在恢复执行。"
                    elif isinstance(resolution, GateResolutionDenied):
                        label = "denied"
                        status_msg = "门控已拒绝。"
                    else:
                        label = "cancelled"
                        status_msg = "门控已取消。"

                    # 广播限定于特定用户的事件。
                    #
                    # 只有订阅了该 user_id 的订阅者（或无范围限定的订阅者）才会收到此事件。
                    state.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution=label,
                            message=status_msg,
                            thread_id=pending.effective_wire_thread_id,
                        ),
                    )  # 投影豁免：桥接调度器，内联等待快速路径解析事件
                return BridgeOutcome.Pending

            # 投递失败——没有活动 VM 在等待（进程重启，或门控是通过未注册
            # 内联等待接收器的代码路径创建的）。回滚我们刚刚安装的任何自动批准，
            # 以便后续调用不会看到过时的首选项，然后穿透到下面的遗留重新进入路径。
            if always_for_inline:
                await state.effect_adapter.revoke_auto_approve(pending.action_name)
                if legacy_registry_name is not None:
                    await state.effect_adapter.revoke_auto_approve(legacy_registry_name)

                # 当恢复的工具执行失败时，从数据库回滚 `AlwaysAllow`。
                await revert_always_allow(agent, pending, prior_permission)

            # 根据解析类型处理
        match resolution:
            case GateResolutionApproved(always):
                # 用户是否永久批准
                always = clamp_always_to_resume_kind(always, pending.resume_kind)

                # 飞行前线程检查，在提交 `AlwaysAllow` 持久化之前 (#2347)：
                # 如果线程在 `take_verified` 和现在之间被删除，持久化自动批准
                # 会为从未运行的工具留下永久首选项。此分支底部的回滚仅在 `Err` 上触发，
                # 因此 `execute_pending_gate_action` 对缺失线程的优雅 `Ok(Respond)` 会绕过它。
                # 在此处短路。
                try:
                    thread = await state.store.load_thread(pending.thread_id)
                except Exception as e:
                    raise RuntimeError("加载线程", e)

                if thread is None:
                    # 广播 `GateResolved { resolution: "expired" }` 事件并返回关闭结果。
                    return emit_gate_expired_dismissal(state, message, pending)

                if state.sse is not None:
                    state.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="approved_always" if always else "approved",
                            message="门控已批准。正在恢复执行。",
                            thread_id=pending.effective_wire_thread_id,
                        ),
                    )

                legacy_registry_name = legacy_extension_alias(pending.action_name)
                prior_permission = None
                if always:
                    await state.effect_adapter.auto_approve_tool(pending.action_name)
                    if legacy_registry_name is not None:
                        await state.effect_adapter.auto_approve_tool(legacy_registry_name)
                    prior_permission = await persist_always_allow(agent, state, pending)

                # 执行gate的操作
                result = await execute_pending_gate_action(
                    agent,
                    state,
                    message,
                    pending,
                    True,
                    (pending.call_id, True),
                )

                if always and isinstance(result, Exception):
                    await state.effect_adapter.revoke_auto_approve(pending.action_name)
                    if legacy_registry_name is not None:
                        await state.effect_adapter.revoke_auto_approve(legacy_registry_name)
                    await revert_always_allow(agent, pending, prior_permission)

                return result

            case GateResolutionDenied(reason):
                if state.sse is not None:
                    state.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="denied",
                            message="门控已拒绝。",
                            thread_id=pending.effective_wire_thread_id,
                        ),
                    )

                try:
                    await agent.channels.send_status(
                        message.channel,
                        StatusUpdate.Status("工具调用已拒绝。"),
                        message.metadata,
                    )
                except Exception:
                    pass

                reason_text = f" 原因: {resolution.reason}" if resolution.reason else ""
                deny_msg = ThreadMessage.user(
                    f"用户拒绝了操作 '{pending.action_name}'。不要重试；选择不同的方法。{reason_text}"
                )

                guard.effect_adapter.reset_call_count()
                await guard.thread_manager.resume_thread(
                    pending.thread_id,
                    message.user_id,
                    deny_msg,
                    (pending.call_id, False),
                    None,
                )

            case GateResolutionCancelled():
                if guard.sse is not None:
                    guard.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="cancelled",
                            message="门控已取消。",
                            thread_id=pending.effective_wire_thread_id(),
                        ),
                    )

                try:
                    await guard.thread_manager.stop_thread(pending.thread_id, message.user_id)
                except Exception as e:
                    logger.debug("取消时停止线程失败: %s", e)

                return BridgeOutcome.Respond("已取消。")

            case GateResolutionCredentialProvided(token):
                if not isinstance(pending.resume_kind, ResumeKind.Authentication):
                    raise engine_err(
                        "解析不匹配",
                        "为非认证门控发送了 CredentialProvided",
                    )

                credential_name = pending.resume_kind.credential_name

                submit_target = await resolve_extension_for_action(
                    guard.auth_manager,
                    guard.extension_manager,
                    guard.effect_adapter.tools(),
                    pending.action_name,
                    pending.parameters,
                    credential_name,
                    message.user_id,
                )
                display_name = submit_target

                if guard.sse is not None:
                    guard.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="credential_provided",
                            message="凭证已收到。正在恢复执行。",
                            thread_id=pending.effective_wire_thread_id(),
                        ),
                    )

                submission = await submit_pending_auth_credential(
                    guard,
                    str(submit_target),
                    credential_name,
                    resolution.token,
                    message.user_id,
                )

                # 根据提交结果处理
                if isinstance(submission, PendingAuthCredentialSubmission.Stored):
                    result = submission.result
                    if classify_configure_result(result) == ConfigureFlowOutcome.Ready:
                        try:
                            await agent.channels.send_status(
                                message.channel,
                                StatusUpdate.AuthCompleted(
                                    extension_name=display_name,
                                    success=True,
                                    message=format_auth_completed_resuming(result.message),
                                ),
                                message.metadata,
                            )
                        except Exception:
                            pass
                    elif classify_configure_result(result) == ConfigureFlowOutcome.PairingRequired:
                        next_pending = await requeue_pairing_pending_gate(
                            guard, pending, str(display_name)
                        )
                        if guard.sse is not None:
                            guard.sse.broadcast_for_user(
                                message.user_id,
                                OnboardingStateDto.pairing_required(
                                    display_name,
                                    str(next_pending.request_id),
                                    pending.effective_wire_thread_id(),
                                    result.message,
                                    result.instructions,
                                    result.onboarding,
                                ),
                            )
                        return BridgeOutcome.Pending
                    elif classify_configure_result(result) in (
                            ConfigureFlowOutcome.AuthRequired,
                            ConfigureFlowOutcome.RetryAuth,
                    ):
                        return await requeue_auth_pending_gate(
                            agent,
                            guard,
                            message,
                            pending,
                            result.message,
                            result.auth_url,
                        )
                elif isinstance(submission, PendingAuthCredentialSubmission.SkippedNoBackend):
                    if pending.resume_output is not None:
                        logger.debug(
                            "认证门控恢复：无后端，令牌已丢弃，因为 resume_output 已准备, user_id=%s, thread_id=%s, request_id=%s",
                            message.user_id,
                            pending.thread_id,
                            pending.request_id,
                        )
                    else:
                        msg = "没有可用的认证管理器、扩展管理器或密钥存储来存储凭证。"
                        await fail_waiting_thread(guard, message.user_id, pending.thread_id, msg)
                        try:
                            await agent.channels.send_status(
                                message.channel,
                                StatusUpdate.AuthCompleted(
                                    extension_name=display_name,
                                    success=False,
                                    message=msg,
                                ),
                                message.metadata,
                            )
                        except Exception:
                            pass
                        return BridgeOutcome.Respond(msg)

                # 处理认证回退重放
                if pending.action_name == "authentication_fallback" and pending.original_message is not None:
                    retry_content = pending.original_message
                    retry_msg = IncomingMessage(
                        content=retry_content,
                        channel=pending.source_channel,
                        user_id=pending.user_id,
                        metadata=message.metadata,
                    )
                    guard.release()
                    return await handle_with_engine_inner(agent, retry_msg, retry_content, 1)

                # 使用 resume_output 或执行待处理操作
                if pending.resume_output is not None:
                    resolved_call_id = await resolved_or_synthetic_call_id_for_pending_action(
                        guard, pending
                    )
                    await guard.thread_manager.resume_thread(
                        pending.thread_id,
                        message.user_id,
                        resumed_action_result_message(
                            resolved_call_id,
                            pending.action_name,
                            pending.resume_output,
                        ),
                        None,
                        resolved_call_id,
                    )
                else:
                    return await execute_pending_gate_action(
                        agent,
                        guard,
                        message,
                        pending,
                        pending.approval_already_granted,
                        None,
                    )

            case GateResolutionExternalCallback(payload):
                if guard.sse is not None:
                    guard.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="external_callback",
                            message="外部回调已收到。正在恢复执行。",
                            thread_id=pending.effective_wire_thread_id(),
                        ),
                    )

                is_external_tool_callback = (
                        isinstance(pending.resume_kind, ResumeKind.External)
                        and is_external_tool_callback_id(pending.resume_kind.callback_id)
                )

                if is_external_tool_callback:
                    resolved_call_id = await resolved_or_synthetic_call_id_for_pending_action(
                        guard, pending
                    )
                    synthesized_output = extract_external_tool_output(
                        resolution.payload, resolved_call_id
                    )
                    raw_rendered = json.dumps(synthesized_output, indent=2, ensure_ascii=False)
                    sanitized = guard.effect_adapter.safety().sanitize_tool_output(
                        pending.action_name, raw_rendered
                    )
                    await guard.thread_manager.resume_thread(
                        pending.thread_id,
                        message.user_id,
                        ThreadMessage.action_result(
                            resolved_call_id,
                            pending.action_name,
                            sanitized.content,
                        ),
                        None,
                        resolved_call_id,
                    )
                elif pending.resume_output is not None:
                    resolved_call_id = await resolved_or_synthetic_call_id_for_pending_action(
                        guard, pending
                    )
                    await guard.thread_manager.resume_thread(
                        pending.thread_id,
                        message.user_id,
                        resumed_action_result_message(
                            resolved_call_id,
                            pending.action_name,
                            pending.resume_output,
                        ),
                        None,
                        resolved_call_id,
                    )
                else:
                    return await execute_pending_gate_action(
                        agent,
                        guard,
                        message,
                        pending,
                        pending.approval_already_granted,
                        None,
                    )

        return await await_thread_outcome(
            agent,
            guard,
            message,
            pending.conversation_id,
            pending.thread_id,
        )


# ----------流程4: 执行待处理gate----------
# 职责说明:
#   1. 执行

async def execute_pending_gate_action(
        agent: Agent,
        state: EngineState,
        message: IncomingMessage,
        pending: PendingGate,
        approval_already_granted: bool,
        approval_event: Optional[Tuple[str, bool]],
) -> BridgeOutcome:
    """执行待处理门控操作。"""
    # 加载线程
    try:
        thread = await state.store.load_thread(pending.thread_id)
    except Exception as e:
        # 瞬态数据库故障——传播以便调用者可以重试，而不是永久丢弃门控
        raise engine_err("加载线程", e)

    if thread is None:
        return emit_gate_expired_dismissal(state, message, pending)

    resolved_call_id = await resolved_or_synthetic_call_id_for_pending_action(state, pending)

    lease = await resume_lease_for_pending_gate(pending, state.thread_manager.leases)
    if lease is None:
        raise engine_err(
            "恢复租约",
            f"没有活动租约覆盖操作 '{pending.action_name}'",
        )

    exec_ctx = ThreadExecutionContext(
        thread_id=pending.thread_id,
        thread_type=thread.thread_type,
        project_id=thread.project_id,
        user_id=thread.user_id,
        step_id=StepId.new(),
        current_call_id=resolved_call_id,
        source_channel=pending.source_channel,
        user_timezone=(
            ValidTimezone.parse(thread.metadata.get("user_timezone"))
            if thread.metadata.get("user_timezone")
            else None
        ),
        thread_goal=thread.goal,
        available_actions_snapshot=None,
        available_action_inventory_snapshot=None,
        conversation_scope=None,
        # 解析后重放：门控已在上游解析，因此不需要真正的控制器。
        # 惰性控制器将任何意外的重新门控显示为类型化拒绝，而不是重现修复前的回滚错误。
        gate_controller=CancellingGateController.arc(),
        # 遗留的 resolved-pending 路径直接将其自己的 `approval_already_granted`
        # 传递给 `execute_resolved_pending_action`，因此此字段对该路径无关。
        # 在此重置以保持默认值明显。
        call_approval_granted=False,
        # 解析后重放永远不会触发新的内联门控；对话路由在此无关。
        conversation_id=None,
    )

    active_leases = await state.thread_manager.leases.active_for_thread(thread.id)
    try:
        inventory = await state.effect_adapter.available_action_inventory(
            active_leases, exec_ctx
        )
        available_actions = list(inventory.inline)
        exec_ctx.available_actions_snapshot = available_actions
        exec_ctx.available_action_inventory_snapshot = inventory
    except Exception as error:
        logger.debug(
            "加载待处理门控恢复的操作清单失败, thread_id=%s, action=%s: %s",
            thread.id,
            pending.action_name,
            error,
        )

    state.effect_adapter.reset_call_count()
    try:
        result = await state.effect_adapter.execute_resolved_pending_action(
            pending.action_name,
            pending.parameters,
            lease,
            exec_ctx,
            approval_already_granted,
        )
        await state.thread_manager.resume_thread(
            pending.thread_id,
            message.user_id,
            resumed_action_result_message(
                resolved_call_id,
                pending.action_name,
                result.output,
            ),
            approval_event,
            resolved_call_id,
        )
        return await await_thread_outcome(
            agent,
            state,
            message,
            pending.conversation_id,
            pending.thread_id,
        )

    except EngineError.GatePaused as e:
        # 获取显示参数
        tool = await state.effect_adapter.tools().get(e.action_name)
        display_parameters = (
            redact_params(e.parameters, tool.sensitive_params())
            if tool
            else e.parameters
        )

        pending_gate = PendingGate(
            request_id=uuid.uuid4(),
            gate_name=e.gate_name,
            user_id=message.user_id,
            thread_id=pending.thread_id,
            scope_thread_id=pending.scope_thread_id,
            conversation_id=pending.conversation_id,
            source_channel=message.channel,
            action_name=e.action_name,
            call_id=e.call_id,
            parameters=e.parameters,
            display_parameters=display_parameters,
            description=f"工具 '{e.action_name}' 需要 {e.resume_kind.kind_name()}。",
            resume_kind=e.resume_kind,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            # 当恢复的门控立即链接到另一个门控时（例如批准后跟着认证），
            # 保留发起用户提示。OAuth 回调重放依赖于此作为原始请求，而不是批准负载。
            original_message=(
                pending.original_message
                if pending.original_message is not None
                else message.content
            ),
            resume_output=e.resume_output,
            paused_lease=e.paused_lease,
            approval_already_granted=(
                    approval_already_granted
                    or isinstance(pending.resume_kind, ResumeKind.Approval)
            ),
        )
        return await insert_and_notify_pending_gate(agent, state, message, pending_gate)

    except Exception as e:
        raise engine_err("执行待处理门控操作", e)


# ----------处理消息----------

async def handle_with_engine(
        agent: Agent,
        message: IncomingMessage,
        content: str,
) -> BridgeOutcome:
    """
    通过引擎 v2 管道处理用户消息。
    :param agent: 处理消息的Agent
    :param message: 要处理的用户消息
    :param content: 解析的文本消息
    :return:
    """
    return await handle_with_engine_inner(agent, message, content, 0)


# 认证重试递归的最大深度（存储凭证 → 重试原始消息）
MAX_AUTH_RETRY_DEPTH: int = 2


async def handle_with_engine_inner(
        agent: Agent,
        message: IncomingMessage,
        content: str,
        depth: int,
) -> BridgeOutcome:
    """通过引擎 v2 管道处理用户消息的内部实现。"""

    # 防止认证重试无限递归，最大深度为 2
    if depth > MAX_AUTH_RETRY_DEPTH:
        return BridgeRespondOutcome(
            "凭证已存储，但认证重试次数过多。请重新发送您的消息。"
        )

    # --------Step1: 确保引擎已初始化--------
    await init_engine(agent)

    lock = ENGINE_STATE.get()
    if lock is None:
        raise RuntimeError("init", "引擎状态未初始化")

    async with lock.read() as guard:
        state = guard
        if state is None:
            raise RuntimeError("init", "引擎状态为空")

        logger.debug(
            "engine v2: 正在处理消息, user_id=%s, channel=%s",
            message.user_id,
            message.channel,
        )

        thread_scope = message.conversation_scope
        # 解析引擎线程 ID
        scoped_thread_id = parse_engine_thread_id(thread_scope)

        # --------Step2: 检查是否有待处理的gate，并按优先级处理--------
        # --------Step2.1: 解析用户的待处理门控--------
        resolution = await resolve_pending_gate_for_user(
            state.pending_gates, message.user_id, thread_scope
        )
        # --------Step2.2: 处理gate--------
        match resolution.kind:
            # 处理解析的gate
            case PendingGateResolutionKind.Resolved:
                gate = resolution.gate
                # 处理需要用户提供凭证的情况
                if gate.resume_kind == ResumeKind.Authentication:
                    request_id = gate.request_id
                    # 用户/系统 取消了待处理的gate
                    if content.strip() == "" or content.strip().lower() == "cancel":
                        gate_resolution = GateResolution.Cancelled
                    # 用户/系统 提供了凭证
                    else:
                        gate_resolution = GateResolution.CredentialProvided(
                            token=content.strip()
                        )
                    # 释放读取锁后再调用 resolve_gate
                    guard.release()
                    return await resolve_gate(
                        agent, message, gate.thread_id, request_id, gate_resolution
                    )

                # 处理用户需要批准的情况
                elif gate.resume_kind == ResumeKind.Approval:
                    pending = gate.clone()
                    # 从状态中克隆 SSE arc 和工具注册表，
                    # 然后在等待 broadcast + channel I/O 之前释放引擎读取锁。
                    # 上面的认证分支执行相同操作，`notify_pending_gate` 的签名
                    # 接受 owned Option<Arc<SseManager>>，正是为了让此
                    # 终端返回分支可以释放锁。`notify_pending_gate` 需要
                    # 工具注册表句柄来解析认证门控显示名称，而无需持有
                    # 引擎状态锁。
                    sse = state.sse.clone()
                    tools = state.effect_adapter.tools()
                    auth_manager = state.auth_manager.clone()
                    extension_manager = state.extension_manager.clone()
                    guard.release()
                    return await notify_pending_gate(
                        agent,
                        sse,
                        tools,
                        auth_manager,
                        extension_manager,
                        message,
                        pending,
                    )

            # 对于多个gate的情况，直接退出
            case PendingGateResolutionKind.Ambiguous:
                return BridgeRespondOutcome(
                    text="多个待处理的批准或认证提示正在等待。请从原始线程回复。"
                )

        # --------Step3: 孤儿线程检查--------
        # 检查线程是否在等待批准或认证但状态丢失，如果是则标记为失败
        if scoped_thread_id is not None:
            orphaned = await fail_orphaned_waiting_thread_if_needed(
                state, message.user_id, scoped_thread_id
            )
            if orphaned:
                return BridgeRespondOutcome(
                    text="此线程正在等待批准或认证，但该待处理状态已丢失。"
                         "线程已标记为失败；请重新发送您的请求。"
                )

        # --------Step4: 对输入内容进行安全验证--------

        # 安全检查——在线程操作 ::process_user_input 中镜像 v1 管道，
        # 确保两条引擎路径执行相同的入站保护措施。
        # 当消息带有附件时，空的文本正文是合法的（附件即为有效载荷）；
        # 跳过验证器对空输入的拒绝，但仍对文本应用长度/策略检查。
        trimmed_content = content.strip()
        skip_empty_check = trimmed_content == "" and len(message.attachments) > 0

        # --------Step4.1: 验证content是否合法--------
        if not skip_empty_check:

            validation = agent.safety().validate_input(content)
            if not validation.is_valid:
                details = "; ".join(
                    f"{e.field}: {e.message}" for e in validation.errors
                )
                return BridgeOutcome.Respond(
                    f"输入被安全验证拒绝: {details}"
                )

        # --------Step4.2: 验证content否违反任何策略规则--------
        violations = agent.safety().check_policy(content)
        if any(rule.action == PolicyAction.Block for rule in violations):
            return BridgeOutcome.Respond(
                "输入被安全策略拒绝。"
            )

        # --------Step4.3: 输入中是否包含泄漏的密钥--------

        # 扫描入站消息中的密钥（API 密钥、令牌）。
        # 在此处捕获它们可以防止大语言模型将其回显，
        # 否则会触发外发泄漏检测器并造成错误循环。
        warning = agent.safety().scan_inbound_for_secrets(content)
        if warning is not None:
            logger.warning(
                "engine v2: 入站消息被阻止——包含泄漏的密钥, user_id=%s, channel=%s",
                message.user_id,
                message.channel,
            )
            return BridgeOutcome.Respond(warning)

        # 解析每用户项目（如果需要则创建）
        project_id = await resolve_user_project(
            state.store, message.user_id, state.default_project_id
        )

        # --------Step5: 处理消息附件--------

        persisted_attachments = list(message.attachments)
        # --------Step5.1: 保存附件内容--------
        attachment_notes = await persist_project_attachments(
            state.project_root,
            message,
            project_id,
            persisted_attachments,
        )

        # --------Step5.2: 将附件内容处理为标准化信息--------

        # 引擎 v2 线程目前仅支持文本，因此附件必须在路由到引擎之前合并到有效的用户内容中。
        # 这样可以在引擎线程和双写的网关历史记录中保留提取的文档文本、项目本地文件路径以及附件元数据。
        augmented = augment_with_attachments(content, persisted_attachments)
        effective_content = augmented.text if augmented is not None else content

        # --------Step6: 触发OnEvent 任务--------

        # 触发所有处于活动状态的 OnEvent 任务，其模式（以及可选的频道过滤器）与此入站消息匹配。
        # 此处触发的任务是消息的副作用——独立于下方生成的常规对话线程，且与之并行执行。
        # 错误会被记录，但绝不会阻塞面向用户的消息处理。
        #
        # 此路径不会触及 v1 创建的例程：它们存在于 v1 例程存储中，并由后台的 v1 RoutineEngine 触发。
        # 通过 routine_create 别名创建的任务存在于引擎存储中，并在此处触发。
        await fire_event_missions_for_message(state, message, effective_content)

        # 向通道发送"思考中..."状态
        try:
            await agent.channels.send_status(
                message.channel,
                StatusUpdate.Thinking("处理中..."),
                message.metadata,
            )
        except Exception:
            pass

        # 重置每步调用计数器，以便每个线程从头开始
        state.effect_adapter.reset_call_count()

        # --------Step7: 创建对话--------

        # --------Step7.1: 限定引擎对话的范围--------
        # 按（频道、用户、线程）限定引擎对话的范围。
        # 当前端发送 thread_id 时（用户创建了新对话），将其作为频道键的一部分，
        # 以便每个 v1 线程映射到独立的引擎对话。
        # 若不这样做，所有线程将共享同一个对话，消息会出现在错误的位置。
        scope = message.conversation_scope
        channel_key = (
            f"{message.channel}:{scope}" if scope is not None else message.channel
        )

        # --------Step7.2: 创建对话--------
        # 获取或创建频道+用户对对应的对话。
        conv_id = await state.conversation_manager.get_or_create_conversation(
            channel_key, message.user_id
        )

        # 在将频道提供的时区传递给引擎之前进行验证。
        # ValidTimezone::parse 会拒绝空字符串/无效字符串；
        # 我们发送规范的 IANA 时区名称（而非原始输入），以便下游消费者看到一个已知正确的值。
        # 必须在 spawn 时*传入*——线程启动后设置元数据对于首轮对话的内存执行器是不可见的。
        validated_tz = ValidTimezone.parse(message.timezone) if message.timezone else None

        # --------Step8: 规则检测显式执行意图--------
        # 检测执行意图并相应地配置义务
        thread_config = ThreadConfig.default()
        if user_signals_execution_intent(content):
            thread_config.require_action_attempt = True

        # --------Step9: 处理首轮对话丢失调用方工具的问题--------

        # 将对话范围（可解析为 Uuid）写入线程的 `initial_metadata` 中。
        # 引擎会将其读回到 `ThreadExecutionContext.conversation_scope` 中，
        # 这使得桥接器的 `EffectBridgeAdapter` 能够通过引擎 `thread_id` 或调用方范围来解析每个对话的状态
        # （目前为调用方提供的外部工具目录）。
        # 若不这样做，在 spawn 后立即启动的执行器任务将与桥接器 spawn 后的 `transfer` 产生竞态条件，
        # 从而导致首轮对话丢失调用方工具。
        scope_uuid = parse_engine_thread_id(scope)
        extra_metadata = None
        if scope_uuid is not None:
            extra_metadata = {
                "conversation_scope": str(scope_uuid),
            }

        # --------Step10: 为线程绑定每次执行的上下文--------

        # 在引擎生成线程之前预先绑定每次执行的上下文。
        # `handle_user_message` 在内部分配并启动引擎任务；
        # 如果快速工具门控在 `set_execution_context` 落地之前触发，
        # 则控制器的 `pause()` 将找不到对应条目并静默取消门控。
        # 预执行插槽以 user_id 为键，上游的每个对话锁确保每个对话最多只有一个桥接轮次正在执行。
        scope_thread_id = ExternalThreadId.new(scope) if scope else None
        per_exec_context = PerExecutionContext(
            conversation_id=conv_id,
            source_channel=message.channel,
            scope_thread_id=scope_thread_id,
            channel_metadata=message.metadata,
            original_message=message.content,
        )
        await state.gate_controller.set_pre_execution_context(
            message.user_id, conv_id, per_exec_context
        )

        # --------Step11: 处理用户消息--------

        # 处理消息——生成新线程或注入到活动线程中。
        # 出错时我们必须清除刚刚安装的预执行插槽：
        # 若不这样做，失败的 `handle_user_message`（在分配任何 thread_id 之前引擎生成/注入失败）
        # 会留下一个以 user_id 为键的过时条目，
        # 这将导致同一用户的下一个门控提示被错误路由。
        try:
            thread_id = await state.conversation_manager.handle_user_message(
                conv_id,
                effective_content,
                project_id,
                message.user_id,
                thread_config,
                validated_tz.name() if validated_tz else None,
                extra_metadata,
            )
        except Exception as e:
            await state.gate_controller.clear_pre_execution_context(
                message.user_id, conv_id
            )
            raise engine_err("线程错误", e)

        # 将预执行条目提升为以（用户、线程）为键的条目。
        # 此后，来自此线程的门控将首先落在线线程键的条目上；
        # 每用户回退覆盖在此提升落地之前触发的任何门控。
        await state.gate_controller.set_execution_context(
            message.user_id, thread_id, per_exec_context
        )

        # 将目录重新键控到引擎分配的 `thread_id` 上，
        # 以便 `await_thread_outcome` 中的终端状态清理钩子
        # 在规范键下找到条目。竞争窗口保护是上面的
        # conversation_scope 管道；此 transfer 是记账部分。
        if scope_uuid is not None:
            await state.external_tool_catalog.transfer(scope_uuid, thread_id)

        if attachment_notes:
            await save_attachment_index_notes(
                state.store,
                project_id,
                message.user_id,
                thread_id,
                attachment_notes,
            )

        # 双重写入 v1 数据库，以便网关历史 API 显示消息。
        # 在可用时使用限定范围的对话，回退到默认的助手对话。
        # 外部通道范围（例如 `wecom:group:*`）不是 UUID，
        # 因此它们被映射到稳定的 UUID 对话 ID，同时在
        # `conversations.thread_id` 中保留原始范围。
        if state.db is not None:
            try:
                cid = await resolve_v1_conversation_for_message(state.db, message)
                await state.db.add_conversation_message(cid, "user", effective_content)
            except Exception as e:
                logger.warning(
                    "无法为用户消息持久化解析 v1 对话, message_id=%s: %s",
                    message.id,
                    e,
                )

        logger.debug("engine v2: 线程已生成, thread_id=%s", thread_id)
        outcome = await await_thread_outcome(
            agent, state, message, conv_id, thread_id
        )

        # 删除每执行上下文。`PendingGate` 行（如果门控触发）
        # 携带了解析器从此处开始所需的一切。
        #
        # BridgeOutcome.Pending 意味着请求处理程序在引擎仍在运行时
        # 达到了截止时间（通常停在 `BridgeGateController::pause` 中等待批准）。
        # 在此处清除上下文会使暂停的线程搁浅——其最终解析将对任何后续门控
        # 调用 `pause()`，而没有注册的上下文，表现为静默的 `Cancelled`。
        # 将清理推迟到监视线程完成并在引擎实际完成后清除的后台任务。
        if (
                isinstance(outcome, BridgeOutcome)
                and outcome.is_pending()
                and await state.thread_manager.is_running(thread_id)
        ):
            spawn_deferred_context_cleanup(
                state.gate_controller,
                state.thread_manager,
                message.user_id,
                thread_id,
                conv_id,
            )
        else:
            await state.gate_controller.clear_execution_context(
                message.user_id, thread_id, conv_id
            )

        return outcome


# ----------辅助函数----------
def parse_engine_thread_id(scope: Optional[str]) -> Optional[ThreadId]:
    """从范围字符串解析引擎线程 ID。"""
    if scope is None:
        return None
    try:
        return ThreadId(uuid.UUID(scope))
    except (ValueError, AttributeError):
        return None


def parse_scope_uuid(scope: Optional[str]) -> Optional[uuid.UUID]:
    """从范围字符串解析 UUID。"""
    if scope is None:
        return None
    try:
        return uuid.UUID(scope)
    except (ValueError, AttributeError):
        return None


async def persist_always_allow_with_store(
        settings_store: Optional[SettingsStore],
        state: EngineState,
        pending: PendingGate,
) -> Optional[Dict[str, Any]]:
    """
    与 [`persist_always_allow`] 相同，但直接接收设置存储，
    而不是通过 `Agent` 访问。让网关 HTTP 快速路径
    (`try_resolve_inline_approval_gate`) 可以在没有 `Agent` 引用的情况下
    安装 AlwaysAllow 首选项，因为 agent-loop mpsc 正是该路径绕过的。
    """
    # 在将工具名称用作设置键之前验证它。拒绝包含点号或其他可能
    # 与点分路径设置命名空间冲突的字符的名称。
    if not is_valid_admin_tool_name(pending.action_name):
        logger.debug(
            "跳过 AlwaysAllow 持久化——无效的工具名称, tool=%s",
            pending.action_name,
        )
        return None

    # 纵深防御：跳过 `ApprovalRequirement::Always` 工具的持久化。
    # 使用实际的待处理参数，以便正确检测参数依赖的工具
    # （例如具有高风险命令的 shell）。
    tool = await state.effect_adapter.tools().get(pending.action_name)
    is_locked = False
    if tool is not None:
        is_locked = tool.requires_approval(pending.parameters) == ApprovalRequirement.Always

    if is_locked:
        logger.debug(
            "跳过 AlwaysAllow 持久化——工具声明了 ApprovalRequirement::Always, tool=%s",
            pending.action_name,
        )
        return None

    # 仅使用 CachedSettingsStore。原始 Database 回退绕过了缓存失效，
    # 导致 GET /api/settings/tools 在 5 分钟 TTL 过期之前提供过时数据。
    # 在生产环境中，设置存储在有数据库时始终可用；回退是死代码，
    # 在测试和边缘部署中积极破坏了缓存一致性。
    if settings_store is None:
        return None

    store = settings_store
    key = f"tool_permissions.{pending.action_name}"

    # 读取先前存在的值，以便在失败时恢复它，
    # 而不是盲目删除长期存在的用户首选项。
    try:
        prior = await store.get_setting(pending.user_id, key)
    except Exception as e:
        logger.debug(
            "resolve_gate: 读取先前权限失败，跳过持久化, tool=%s, error=%s",
            pending.action_name,
            e,
        )
        return None

    val = json.dumps("always_allow")

    # 调度豁免：引擎内部持久化镜像 v1 thread_ops 直写
    try:
        await store.set_setting(pending.user_id, key, val)
        logger.debug(
            "已将 AlwaysAllow 权限持久化到数据库设置 (engine v2), tool=%s, user_id=%s",
            pending.action_name,
            pending.user_id,
        )
    except Exception as e:
        logger.warning(
            "resolve_gate: 持久化 AlwaysAllow 失败, tool=%s, user_id=%s, error=%s",
            pending.action_name,
            pending.user_id,
            e,
        )

    return prior

async def persist_always_allow(
        agent: Agent,
        state: EngineState,
        pending: PendingGate,
) -> Optional[Dict[str, Any]]:
    """
    当用户点击"始终批准"时，将 `AlwaysAllow` 持久化到数据库。

    纵深防御：为实际待处理参数声明了 `ApprovalRequirement::Always` 的工具
    永远不会被持久化（UI 隐藏按钮，但精心构造的客户端可以发送它）。
    工具名称在用作设置键之前经过验证。

    返回先前存在的权限值（如果有），以便调用者可以通过
    [`revert_always_allow`] 在失败时恢复它。
    """
    return await persist_always_allow_with_store(
        agent.deps.settings_store, state, pending
    )

async def revert_always_allow(
        agent: Agent,
        pending: PendingGate,
        prior: Optional[Dict[str, Any]],
) -> None:
    """
    当恢复的工具执行失败时，从数据库回滚 `AlwaysAllow`。

    恢复在 [`persist_always_allow`] 写入 `AlwaysAllow` 之前存在的 `prior` 值。
    如果没有先前值，则删除该键。
    """
    await revert_always_allow_with_store(
        agent.deps.settings_store, pending, prior
    )


async def revert_always_allow_with_store(
        settings_store: Optional[SettingsStore],
        pending: PendingGate,
        prior: Optional[Dict[str, Any]],
) -> None:
    """
    与 [`revert_always_allow`] 相同，但直接接收设置存储。
    与 [`persist_always_allow_with_store`] 配对，用于绕过 agent-loop mpsc 的
    网关 HTTP 快速路径。
    """
    if settings_store is None:
        return

    store = settings_store
    key = f"tool_permissions.{pending.action_name}"

    try:
        if prior is not None:
            # 调度豁免：persist_always_allow 的引擎内部回滚
            await store.set_setting(pending.user_id, key, prior)
        else:
            # 调度豁免：persist_always_allow 的引擎内部回滚
            await store.delete_setting(pending.user_id, key)
    except Exception as e:
        logger.warning(
            "resolve_gate: 执行失败后回滚 AlwaysAllow 失败, tool=%s, user_id=%s, error=%s",
            pending.action_name,
            pending.user_id,
            e,
        )

def emit_gate_expired_dismissal(
        state: EngineState,
        message: IncomingMessage,
        pending: PendingGate,
) -> BridgeOutcome:
    """
    广播 `GateResolved { resolution: "expired" }` 事件并返回关闭结果。
    当目标线程在 `take_verified` 和恢复之间被删除时使用，
    因此没有活动线程可以执行。

    持久化副作用的调用者（例如 `Approved { always }` 将 `AlwaysAllow` 写入设置）
    必须使用 `state.store.load_thread` 进行预检，并在持久化之前调用此辅助函数，
    以免缺失的线程静默提交对从未运行的工具的长期首选项 (#2347)。
    """
    logger.debug(
        "未找到待处理门控的线程；发出过期解析, thread_id=%s, gate=%s, action=%s",
        pending.thread_id,
        pending.gate_name,
        pending.action_name,
    )
    if state.sse is not None:
        state.sse.broadcast_for_user(
            message.user_id,
            AppEvent.GateResolved(
                request_id=str(pending.request_id),
                gate_name=pending.gate_name,
                tool_name=pending.action_name,
                resolution="expired",
                message="线程不再存在。",
                thread_id=pending.effective_wire_thread_id(),
            ),
        )
    return BridgeOutcome.Respond("线程不再存在。批准已关闭。")


def clamp_always_to_resume_kind(always: bool, resume_kind: ResumeKind) -> bool:
    """
    将调用者提供的 `always` 批准标志限制为待处理门控的
    `ResumeKind` 实际允许的范围。

    受保护操作的门控（编排器自我修改写入）通告
    `ResumeKind::Approval { allow_always: false }`，因此 UI 隐藏
    "始终批准"按钮。但批准 HTTP 端点仍然接受用户提供的
    `always: true`，因此没有此限制，精心构造的请求可以为
    `memory_write` 安装会话范围的自动批准，并绕过每个后续的每次调用门控。
    待处理门控自己的 `allow_always` 是权威的服务器端策略。

    非批准恢复类型（auth、外部回调）不携带 "always" 语义，
    始终限制为 `false`。
    """
    if not always:
        return False
    return (
            isinstance(resume_kind, ResumeKindApproval)
            and resume_kind.allow_always
    )
