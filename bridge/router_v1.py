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
    ProjectId,
    Store,
    ThreadConfig,
    ThreadManager,
    ThreadOutcome,
    ThreadMessage,
    CapabilityLease,
    MessageRole,
StepId,
ThreadExecutionContext,
CancellingGateController
)

from ironclaw_common import AppEvent
from engine.types import (is_shared_owner, shared_owner_id)
from engine.types.thread import Thread, ThreadId, ThreadState

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
from typing import List, Dict, Set, Optional, Union, Any, Tuple
import os
import regex as re
from datetime import datetime, timezone
from pathlib import Path
import uuid
import json
import asyncio
from llm import user_signals_execution_intent

# 导入数据类型
# bridge
from types.bridge.bridge_outcome import (
    BridgeOutcome,
    BridgeRespondOutcome,
    BridgeNoResponseOutcome,
    BridgePendingOutcome
)

from types.gate.pending_gate_resolution import (
    PendingGateResolution,
    PendingGateResolutionNone,
    PendingGateResolutionResolved,
    PendingGateResolutionAmbiguous
)
from secrets.store import SecretsStore

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
    # 实际为bridge.store_adapter 的HybridStore
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
    external_tool_catalog: ExternalToolCatalog
    # 引擎 v2 能力注册表。保存在此处（除了 `effect_adapter` 的内部句柄），
    # 以便 Responses API 处理程序可以枚举内部操作名称并拒绝会遮蔽它们的调用者提供的工具。
    capability_registry: CapabilityRegistry
    # 内联门控等待控制器。让引擎在 `Approval` 和 `Authentication` 门控上
    # 原地暂停 Tier 0 和 Tier 1 执行，而不是回退到编排器并在恢复时重新进入
    # （这会重新执行先前的非幂等工具调用）。
    gate_controller: BridgeGateController
    # 进程范围内的进行中门控解析通道注册表。
    # 与 `gate_controller` 一起保存，以便 OAuth 回调路径可以按凭证名称
    # 唤醒暂停的 Authentication 等待者，而无需通过控制器的内部机制。
    gate_resolutions: GateResolutions


# 全局引擎状态，首次使用时初始化
# 为了确保 Engine v2 状态的线程安全访问和单例初始化
ENGINE_STATE: Optional[EngineState] = None
ENGINE_STATE_LOCK: asyncio.Lock | None = None


# ----------数据结构: 区分后端是否已存储凭证---------
# 职责说明:
#   1. 定义"完成有文本响应"、"完成无文本响应"、""
class PendingAuthCredentialSubmission(Enum):
    """
    `submit_pending_auth_credential` 的结果——
    区分"后端已存储凭证"和"没有后端配置为存储它"。
    调用者将后者映射为线程失败（生产环境）或静默继续（裸恢复测试夹具），
    参见 `resolve_gate` 中的匹配分支。
    """
    Stored = "stored"
    SkippedNoBackend = "skipped_no_backend"


# ----------流程1: 初始化引擎---------
# 职责说明:
#   1. 使用代理的依赖项获取或初始化引擎状态

async def init_engine(agent: Agent) -> None:
    pass


# ----------流程2: 解析待处理gate----------
# 职责说明:
#   1. 解析用户有哪些待处理的gate
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
        return PendingGateResolutionNone()
    elif len(candidates) == 1:
        return PendingGateResolutionResolved(gate=candidates[0])
    elif hinted_uuid is not None:
        # 多个候选，选择最近创建的
        resolved_gate = max(candidates, key=lambda gate: gate.created_at)
        return PendingGateResolutionResolved(gate=resolved_gate)
    else:
        # 否则，返回 Ambiguous
        return PendingGateResolutionAmbiguous()


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
            # TODO 直接返回错误，暂不区分错误类型
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
                return BridgePendingOutcome()

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
            # 用户同意处理:
            #   1. 服务器端策略强制：通过 clamp_always_to_resume_kind 强制覆盖客户端提交的 always 标志，确保符合 gate 的 allow_always 策略
            #   2. 线程存在性检查：在持久化 auto-approve 前检查线程是否已删除，防止为不存在的工具留下永久偏好
            #   3. 内联等待快速路径：尝试通过 GateController::try_deliver 直接传递给等待中的引擎，成功则返回 BridgeOutcome::Pending
            #   4. 持久化 auto-approve：如果 always=true，调用 persist_always_allow 将偏好保存到数据库
            #   5. 执行工具调用：调用 execute_pending_gate_action 执行工具
            #   6. 失败回滚：如果执行失败，回滚 auto-approve 偏好和数据库持久化
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
            # 用户拒绝:
            #   1. 内联等待快速路径：尝试通过 try_deliver 传递拒绝结果
            #   2. SSE 事件广播：发送 GateResolved 事件，resolution 为 "denied"
            #   3. 通道状态更新：发送 "Tool call denied." 状态更新
            #   4. 注入拒绝消息：构造 carefully worded 的拒绝消息，避免触发执行意图检测（如 "execute it"、"run it" 等短语）
            #   5. 恢复线程：调用 thread_manager.resume_thread，传入拒绝消息和 call_id 设置为 false
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

                state.effect_adapter.reset_call_count()
                await state.thread_manager.resume_thread(
                    pending.thread_id,
                    message.user_id,
                    deny_msg,
                    (pending.call_id, False),
                    None,
                )
            # 用户取消:
            #   1. 内联等待快速路径：尝试通过 try_deliver 传递取消结果
            #   2. SSE 事件广播：发送 GateResolved 事件，resolution 为 "cancelled"
            #   3. 停止线程：调用 thread_manager.stop_thread 完全停止线程
            #   4. 返回响应：返回 BridgeOutcome::Respond("Cancelled.".into())
            case GateResolutionCancelled():
                if state.sse is not None:
                    state.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="cancelled",
                            message="门控已取消。",
                            thread_id=pending.effective_wire_thread_id,
                        ),
                    )

                try:
                    await state.thread_manager.stop_thread(pending.thread_id, message.user_id)
                except Exception as e:
                    logger.debug("取消时停止线程失败: %s", e)

                return BridgeRespondOutcome("已取消。")
            # 提供了凭证:
            #   1. 扩展名称解析：调用 resolve_extension_for_action 将工具动作映射到扩展名称（WASM 工具需要扩展名而非凭证名）
            #   2. SSE 事件广播：发送 GateResolved 事件，resolution 为 "credential_provided"
            #   3. 凭证提交：调用 submit_pending_auth_credential 存储凭证
            #   4. 结果分类处理：
            #       Ready：发送 AuthCompleted 状态更新，恢复线程
            #       PairingRequired：调用 requeue_pairing_pending_gate 创建新的配对 gate
            #       AuthRequired/RetryAuth：调用 requeue_auth_pending_gate 重新排队认证 gate
            #       SkippedNoBackend：如果有 resume_output 则恢复线程，否则失败等待线程
            #       ValidationFailed：重新排队认证 gate
            #   5. 恢复线程：成功后调用 thread_manager.resume_thread
            case GateResolutionCredentialProvided(token):
                if not isinstance(pending.resume_kind, ResumeKindAuthentication):
                    raise RuntimeError(
                        "解析不匹配",
                        "为非认证门控发送了 CredentialProvided",
                    )

                credential_name = pending.resume_kind.credential_name

                # 解决凭证名称与扩展名称的映射问题
                submit_target = await resolve_extension_for_action(
                    state.auth_manager,
                    state.extension_manager,
                    state.effect_adapter.tools(),
                    pending.action_name,
                    pending.parameters,
                    credential_name,
                    message.user_id,
                )
                display_name = submit_target

                if state.sse is not None:
                    state.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="credential_provided",
                            message="凭证已收到。正在恢复执行。",
                            thread_id=pending.effective_wire_thread_id,
                        ),
                    )

                # 尝试持久化用户提供的认证凭证
                submission = await submit_pending_auth_credential(
                    state,
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
                            state, pending, str(display_name)
                        )
                        if state.sse is not None:
                            state.sse.broadcast_for_user(
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
                        # 为同一 `(user, thread)` 重新排队认证门控
                        return await requeue_auth_pending_gate(
                            agent,
                            state,
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
                        return BridgeRespondOutcome(msg)

                # 处理认证回退重放
                if pending.action_name == "authentication_fallback" and pending.original_message is not None:
                    retry_content = pending.original_message
                    retry_msg = IncomingMessage(
                        content=retry_content,
                        channel=pending.source_channel,
                        user_id=pending.user_id,
                        metadata=message.metadata,
                    )
                    # 释放锁
                    # guard.release()
                    # 递归重新处理，需要修改
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
            # 收到了外部回调
            case GateResolutionExternalCallback(payload):
                if state.sse is not None:
                    state.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="external_callback",
                            message="外部回调已收到。正在恢复执行。",
                            thread_id=pending.effective_wire_thread_id,
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

        # 等待线程完成并返回桥接结果
        return await await_thread_outcome(
            agent,
            state,
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
        raise RuntimeError("加载线程", e)

    if thread is None:
        # 通知用户并退出程序
        return emit_gate_expired_dismissal(state, message, pending)

    # 返回最新的function call id
    resolved_call_id = await resolved_or_synthetic_call_id_for_pending_action(state, pending)

    lease = await resume_lease_for_pending_gate(pending, state.thread_manager.leases)
    if lease is None:
        raise RuntimeError(
            "恢复租约",
            f"没有活动租约覆盖操作 '{pending.action_name}'",
        )

    # 设置线程的上下文信息
    exec_ctx = ThreadExecutionContext(
        thread_id=pending.thread_id,
        thread_type=thread.thread_type,
        project_id=thread.project_id,
        user_id=thread.user_id,
        step_id=StepId(),
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
        gate_controller=CancellingGateController(),
        # 遗留的 resolved-pending 路径直接将其自己的 `approval_already_granted`
        # 传递给 `execute_resolved_pending_action`，因此此字段对该路径无关。
        # 在此重置以保持默认值明显。
        call_approval_granted=False,
        # 解析后重放永远不会触发新的内联门控；对话路由在此无关。
        conversation_id=None,
    )

    # 获取线程的所有活跃（有效）lease
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
        raise RuntimeError("执行待处理门控操作", e)


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
def snapshot_lease_still_valid(
        lease: CapabilityLease,
        pending: PendingGate,
) -> bool:
    """
    验证门控暂停时记录的 `paused_lease` 快照在恢复时是否仍代表可用租约。

    门控可以在待处理门控存储中停留数小时或跨进程重启；
    在此期间，原始租约可能已被撤销、过期，或者待处理记录可能已偏离其原始线程。
    未通过此检查的调用者不得使用快照——
    回退到 `LeaseManager.find_lease_for_action`（强制执行其自己的范围限定）或安全关闭。
    """
    if lease.thread_id != pending.thread_id:
        return False
    if not lease.granted_actions.covers(pending.action_name):
        return False
    if lease.revoked:
        return False
    if lease.expires_at is not None and lease.expires_at <= datetime.now(timezone.utc):
        return False
    return True


async def resume_lease_for_pending_gate(
        pending: PendingGate,
        leases: LeaseManager,
) -> Optional[CapabilityLease]:
    """
    选择用于恢复待处理门控操作的租约。如果门控记录的 `paused_lease` 快照仍然有效，
    则优先使用它；回退到 `LeaseManager` 中的实时查找。
    如果两条路径都没有产生租约，则返回 `None`——调用者将其映射到"无活动租约"错误。
    """
    if pending.paused_lease is not None:
        snapshot = pending.paused_lease
        if snapshot_lease_still_valid(snapshot, pending):
            return snapshot

    return await leases.find_lease_for_action(pending.thread_id, pending.action_name)


def synthetic_action_call_id(action_name: str) -> str:
    """
    当无法恢复历史 id 时，合成一个新的操作调用 id。

    用作最后手段，以便恢复的 `ActionResult` 消息仍然携带非空相关器，
    引擎不会静默丢弃回复。
    """
    return f"synthetic-{action_name}-{str(uuid.uuid4())}"


def resolved_call_id_for_pending_action(
        thread: Thread,
        pending: PendingGate,
) -> Optional[str]:
    """
    解析与待处理门控对应的助手操作 `call_id`。

    当持久化的 `call_id` 和历史扫描都无法产生匹配时，返回 `None`。
    调用者必须将 `None` 视为真正的未命中，并合成一个新的 id，
    而不是将其折叠为空字符串——`ThreadMessage::action_result` 上的空
    `action_call_id` 会破坏引擎的调用/结果配对，
    并导致助手丢弃恢复的回复。
    """
    # 新的待处理门控在插入时持久化了确切的 call_id。
    # 仅在 call_id 存储之前创建的遗留行中从历史推断。
    if pending.call_id:
        return pending.call_id

    # 扫描用户可见的 `messages` 和 `internal_messages`
    # （编排器的工作转录）。在生产环境中，编排器通过
    # `sync_runtime_state` 将 ActionResult 消息写入 `internal_messages`，
    # 因此仅扫描 `messages` 会使已解析的 id 集合为空，
    # 回退将永远不会匹配。
    all_messages = list(thread.messages) + list(thread.internal_messages)

    resolved_ids: Set[str] = set()
    for message in all_messages:
        if message.role == MessageRole.ActionResult:
            if message.action_call_id:
                resolved_ids.add(message.action_call_id)

    # 倒序扫描以找到最近未解析的匹配
    for message in reversed(all_messages):
        if message.role != MessageRole.Assistant:
            continue
        if message.action_calls:
            for call in message.action_calls:
                if (
                        call.action_name == pending.action_name
                        and call.id not in resolved_ids
                ):
                    return call.id

    return None


async def resolved_or_synthetic_call_id_for_pending_action(
        state: EngineState,
        pending: PendingGate,
) -> str:
    """解析待处理操作的操作调用 id，或在无法解析时合成。"""
    try:
        thread = await state.store.load_thread(pending.thread_id)
    except Exception as e:
        raise RuntimeError("加载线程", e)

    if thread is None:
        raise RuntimeError("加载线程", "线程未找到")

    result = resolved_call_id_for_pending_action(thread, pending)
    if result is not None:
        return result

    logger.warning(
        "待处理门控没有历史 call_id；合成一个以保持 ActionResult 相关器非空, action=%s, thread_id=%s",
        pending.action_name,
        pending.thread_id,
    )
    return synthetic_action_call_id(pending.action_name)


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
                thread_id=pending.effective_wire_thread_id,
            ),
        )
    return BridgeRespondOutcome("线程不再存在。批准已关闭。")


async def await_thread_outcome(
        agent: Agent,
        state: EngineState,
        message: IncomingMessage,
        conv_id: ConversationId,
        thread_id: ThreadId,
) -> BridgeOutcome:
    """
    等待线程完成并返回桥接结果。

    处理事件转发、内联门控检测、超时和所有线程结果类型。
    """
    event_rx = state.thread_manager.subscribe_events()
    channels = agent.channels
    channel_name = message.channel
    metadata = message.metadata
    sse = state.sse
    tid_str = str(thread_id)

    # 安全超时：如果线程在 5 分钟内未完成，则跳出以避免永远挂起用户会话
    # （例如，在拒绝批准后线程无法恢复）
    deadline = asyncio.get_event_loop().time() + 300
    timed_out = False
    gate_parked = False
    pending_key = PendingGateKey(user_id=message.user_id, thread_id=thread_id)

    while True:
        try:
            event = await asyncio.wait_for(event_rx.recv(), timeout=0.5)
            if getattr(event, 'thread_id', None) == thread_id:
                await forward_event_to_channel(event, channels, channel_name, metadata)
                if sse is not None:
                    skip_verbose = not sse.has_verbose_receivers()
                    leak_detector = state.effect_adapter.safety().leak_detector()
                    for app_event in thread_event_to_app_events(event, tid_str):
                        if skip_verbose and app_event.is_verbose_only():
                            continue
                        # 引擎 crate 原始发出 CodeExecuted——它不依赖 `ironclaw_safety`。
                        # 在此处的桥接边界，在事件到达任何 SSE 订阅者之前，
                        # 清除 code/stdout/return_value 负载中的密钥
                        # （bearer 令牌、API 密钥等）。
                        redact_code_executed_secrets(app_event, leak_detector)
                        sse.broadcast_for_user(message.user_id, app_event)
        except asyncio.TimeoutError:
            pass
        except Exception:
            break

        # 检查线程是否仍在运行
        if not await state.thread_manager.is_running(thread_id):
            break

        # 内联门控检测：如果在线程仍在运行时已为 (user, thread) 注册了待处理门控，
        # 则引擎在 `BridgeGateController::pause` 内暂停等待用户解析。
        # 在此处持有 `handle_message` 会使每用户代理循环串行化在暂停之后——
        # 排队在 `msg_tx` 中的第二个线程的 `UserInput` 无法分派，
        # 直到用户解析此门控或下面的 5 分钟截止时间触发。
        # 移交给后台继续任务（保留事件转发 + 最终响应投递）
        # 并显示为 `Pending`，以便代理循环解除阻塞。
        if await state.pending_gates.peek(pending_key) is not None:
            gate_parked = True
            break

        if asyncio.get_event_loop().time() >= deadline:
            logger.warning(
                "await_thread_outcome 在 5 分钟后超时——跳出以避免挂起, thread_id=%s",
                thread_id,
            )
            timed_out = True
            break

    # 如果我们因为线程在内联门控处暂停而退出，将生命周期的其余部分
    # （事件转发 + 完成时的最终响应广播 + 每执行上下文清理）
    # 移交给后台任务并返回 `Pending`。join_thread 不能在前台任务上运行，
    # 因为它会在暂停的 future 上阻塞长达门控的 30 分钟过期时间。
    if gate_parked and await state.thread_manager.is_running(thread_id):
        spawn_post_park_continuation(
            state,
            agent.channels,
            message,
            conv_id,
            thread_id,
        )
        return BridgeOutcome.Pending

    # 如果我们达到截止时间且线程仍在运行（通常是因为它在
    # `BridgeGateController::pause` 中暂停等待用户尚未操作的批准），
    # 不要调用 `join_thread`——这会在同一暂停任务上阻塞请求处理程序
    # 长达门控的 `expires_at`（30 分钟）。显示为 `Pending`：
    # 活动的 `PendingGate` 行保持可用，用户仍可以解析它，
    # 解析器路径将把解析投递到暂停的 oneshot 中。
    if timed_out and await state.thread_manager.is_running(thread_id):
        return BridgeOutcome.Pending

    outcome = await state.thread_manager.join_thread(thread_id)

    # 在终端结果上丢弃外部工具目录条目——线程永远无法从
    # `Completed`、`Stopped`、`MaxIterations` 或 `Failed` 恢复，
    # 因此条目将永远泄漏。`GatePaused` 有意保留条目：
    # 后续恢复请求需要目录仍然知道此线程的调用者提供的工具。
    if not isinstance(outcome, ThreadOutcome.GatePaused):
        await state.external_tool_catalog.clear(thread_id)

    await state.conversation_manager.record_thread_outcome(
        conv_id, thread_id, outcome
    )

    # 为所有产生响应的结果写入 v1 数据库响应
    async def _write_v1_response(text: str) -> None:
        if state.db is not None:
            try:
                cid = await resolve_v1_conversation_for_message(state.db, message)
                await state.db.add_conversation_message(cid, "assistant", text)
            except Exception as e:
                logger.warning(
                    "解析 v1 对话以持久化助手响应失败, message_id=%s: %s",
                    message.id,
                    e,
                )

    # SSE 响应广播（web）
    if (
            state.sse is not None
            and isinstance(outcome, ThreadOutcome.Completed)
            and outcome.response is not None
    ):
        state.sse.broadcast_for_user(
            message.user_id,
            AppEvent.Response(
                content=outcome.response,
                thread_id=tid_str,
            ),
        )

    result: BridgeOutcome

    if isinstance(outcome, ThreadOutcome.Completed):
        logger.debug("engine v2: 已完成, thread_id=%s", thread_id)

        response = outcome.response

        # 基于文本的认证回退：检测响应中的 authentication_required 并进入认证模式。
        # 这是纵深防御安全网——飞行前认证门控应在执行前捕获大多数情况。
        if response is not None and "authentication_required" in response:
            logger.debug(
                "基于文本的认证回退触发——飞行前门控未捕获到, thread_id=%s",
                thread_id,
            )

            parsed_cred_name = parse_credential_name(response)

            # 防御凭证名称注入：仅当解析的名称是实际注册的凭证时，
            # 才启用回退认证门控。使用选定凭证名称构造
            # `authentication_required` 消息的工具不能强制用户提供不相关的密钥。
            # 没有凭证注册表就无法验证名称，因此门控不得触发——
            # 没有注册表的测试/嵌入夹具有意丢失回退路径，而不是获得提示注入向量。
            cred_name = None
            if parsed_cred_name is not None:
                cred_reg = agent.tools().credential_registry()
                if cred_reg is not None and cred_reg.has_secret(parsed_cred_name):
                    cred_name = parsed_cred_name

            if cred_name is None:
                logger.warning(
                    "基于文本的认证回退拒绝未知或缺失的凭证名称, thread_id=%s",
                    thread_id,
                )
                return BridgeOutcome.Respond(response)

            # 通过 AuthManager 查找设置说明（或回退到内联查找）
            setup_hint = f"提供您的 {cred_name} 令牌"
            if state.auth_manager is not None:
                hint = state.auth_manager.get_setup_instructions(cred_name)
                if hint is not None:
                    setup_hint = hint

            pending = PendingGate(
                request_id=uuid.uuid4(),
                gate_name="authentication",
                user_id=message.user_id,
                thread_id=thread_id,
                scope_thread_id=(
                    ExternalThreadId.new(scope)
                    if (scope := message.conversation_scope())
                    else None
                ),
                conversation_id=conv_id,
                source_channel=message.channel,
                action_name="authentication_fallback",
                call_id=f"fallback-auth-{thread_id}",
                parameters={"credential_name": cred_name},
                display_parameters=None,
                description=f"需要为 '{cred_name}' 进行认证。",
                resume_kind=ResumeKind.Authentication(
                    credential_name=CredentialName.from_trusted(cred_name),
                    instructions=setup_hint,
                    auth_url=None,
                ),
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
                original_message=message.content,
                resume_output=None,
                paused_lease=None,
                approval_already_granted=False,
            )
            pending_request_id = str(pending.request_id)
            try:
                await state.pending_gates.insert(pending)
            except Exception as e:
                logger.debug("存储回退认证门控失败: %s", e)

            # 通过通道显示认证提示（仅卡片，无文本）
            try:
                await agent.channels.send_status(
                    message.channel,
                    StatusUpdate.AuthRequired(
                        extension_name=ExtensionName.from_trusted(cred_name),
                        instructions=setup_hint,
                        auth_url=None,
                        setup_url=None,
                        request_id=pending_request_id,
                    ),
                    message.metadata,
                )
            except Exception:
                pass

            return BridgeOutcome.Pending

        # 仅为已完成的线程持久化 tool_calls——不为 GatePaused 持久化
        # （部分工具，在恢复时会产生孤立行）
        if state.db is not None:
            await persist_v2_tool_calls(state.store, state.db, thread_id, message)

        if response is not None:
            result = BridgeOutcome.Respond(response)
        else:
            result = BridgeOutcome.NoResponse

    elif isinstance(outcome, ThreadOutcome.Stopped):
        result = BridgeOutcome.Respond("线程已停止。")

    elif isinstance(outcome, ThreadOutcome.MaxIterations):
        result = BridgeOutcome.Respond("达到最大迭代次数但未完成。")

    elif isinstance(outcome, ThreadOutcome.Failed):
        sanitized = user_facing_thread_failure(outcome.error)
        sse_will_deliver_to_user = (
                state.sse is not None and message.channel == GATEWAY_CHANNEL_NAME
        )
        if state.sse is not None:
            state.sse.broadcast_for_user(
                message.user_id,
                AppEvent.Error(
                    message=sanitized,
                    thread_id=tid_str,
                ),
            )
        result = bridge_outcome_for_failed_thread(
            outcome.error,
            outcome.debug_detail,
            message.user_id,
            message.channel,
            sse_will_deliver_to_user,
        )

    elif isinstance(outcome, ThreadOutcome.GatePaused):
        # 在存储/广播之前编辑敏感参数
        tool = await state.effect_adapter.tools().get(outcome.action_name)
        redacted_params = (
            redact_params(outcome.parameters, tool.sensitive_params())
            if tool
            else outcome.parameters
        )

        # 存储在统一的 PendingGateStore 中（按 user_id + thread_id 键控）
        pending = PendingGate(
            request_id=uuid.uuid4(),
            gate_name=outcome.gate_name,
            user_id=message.user_id,
            thread_id=thread_id,
            scope_thread_id=(
                ExternalThreadId.new(scope)
                if (scope := message.conversation_scope())
                else None
            ),
            conversation_id=conv_id,
            source_channel=message.channel,
            action_name=outcome.action_name,
            call_id=outcome.call_id,
            parameters=outcome.parameters,
            display_parameters=redacted_params,
            description=(
                f"工具 '{outcome.action_name}' 需要 {outcome.resume_kind.kind_name()}"
                f" (门控: {outcome.gate_name})"
            ),
            resume_kind=outcome.resume_kind,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            original_message=message.content,
            resume_output=outcome.resume_output,
            paused_lease=outcome.paused_lease,
            approval_already_granted=False,
        )

        try:
            await state.pending_gates.insert(pending)
        except Exception as e:
            logger.debug(
                "存储待处理门控失败（可能重复）, gate=%s, error=%s",
                outcome.gate_name,
                e,
            )

        # 来自 Responses API 的调用者提供的外部工具：
        # 显示为 `AppEvent::ExternalToolCall`，以便 /v1/responses 处理程序
        # 可以发出 `function_call` ResponseOutputItem 并完成回合。
        if (
                isinstance(pending.resume_kind, ResumeKind.External)
                and is_external_tool_callback_id(pending.resume_kind.callback_id)
        ):
            if state.sse is not None:
                arguments = json.dumps(pending.parameters)
                state.sse.broadcast_for_user(
                    message.user_id,
                    AppEvent.ExternalToolCall(
                        request_id=str(pending.request_id),
                        call_id=pending.call_id,
                        name=pending.action_name,
                        arguments=arguments,
                        thread_id=pending.effective_wire_thread_id(),
                    ),
                )
            else:
                logger.debug(
                    "外部工具门控已暂停（CodeAct 后）但没有连接广播器；调用者将不会被通知, user_id=%s, callback=%s, request_id=%s",
                    message.user_id,
                    pending.resume_kind.callback_id,
                    pending.request_id,
                )
            return BridgeOutcome.Pending

        # 通过源通道发送批准/认证卡片
        extension_name = await resolve_auth_gate_extension_name(
            state.auth_manager,
            state.extension_manager,
            state.effect_adapter.tools(),
            pending,
        )
        await send_pending_gate_status(agent, message, pending, extension_name)
        result = BridgeOutcome.Pending

    else:
        result = BridgeOutcome.NoResponse

    # 为所有结果写入 v1 数据库响应，以便历史端点显示正确状态
    if isinstance(result, BridgeOutcome.Respond):
        await _write_v1_response(result.text)

    return result


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
