from __future__ import annotations

from ironclaw_engine import (
    Capability, CapabilityRegistry, ConversationManager, EffectExecutor, LeaseManager,
    MissionManager, PolicyEngine, Project, Store, ThreadConfig, ThreadManager, ThreadOutcome,
)

from ironclaw_common import AppEvent
from ironclaw_engine.types import (is_shared_owner, shared_owner_id)

from agent import Agent
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
import logging

logger = logging.getLogger(__name__)

# 认证重试递归的最大深度（存储凭证 → 重试原始消息）
MAX_AUTH_RETRY_DEPTH: int = 2


async def handle_with_engine(
        agent: Agent,
        message: IncomingMessage,
        content: str,
) -> BridgeOutcome:
    """通过引擎 v2 管道处理用户消息。"""
    return await handle_with_engine_inner(agent, message, content, 0)


async def handle_with_engine_inner(
        agent: Agent,
        message: IncomingMessage,
        content: str,
        depth: int,
) -> BridgeOutcome:
    """通过引擎 v2 管道处理用户消息的内部实现。"""
    if depth > MAX_AUTH_RETRY_DEPTH:
        return BridgeOutcome.Respond(
            "凭证已存储，但认证重试次数过多。请重新发送您的消息。"
        )

    # 确保引擎已初始化
    await init_engine(agent)

    lock = ENGINE_STATE.get()
    if lock is None:
        raise engine_err("init", "引擎状态未初始化")

    async with lock.read() as guard:
        state = guard
        if state is None:
            raise engine_err("init", "引擎状态为空")

        logger.debug(
            "engine v2: 正在处理消息, user_id=%s, channel=%s",
            message.user_id,
            message.channel,
        )

        thread_scope = message.conversation_scope()
        scoped_thread_id = parse_engine_thread_id(thread_scope)

        # 解析用户的待处理门控
        resolution = await resolve_pending_gate_for_user(
            state.pending_gates, message.user_id, thread_scope
        )

        if isinstance(resolution, PendingGateResolution.Resolved):
            gate = resolution.gate
            if gate.resume_kind == ResumeKind.Authentication:
                request_id = gate.request_id
                if content.strip() == "" or content.strip().lower() == "cancel":
                    gate_resolution = GateResolution.Cancelled
                else:
                    gate_resolution = GateResolution.CredentialProvided(
                        token=content.strip()
                    )
                # 释放读取锁后再调用 resolve_gate
                guard.release()
                return await resolve_gate(
                    agent, message, gate.thread_id, request_id, gate_resolution
                )

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

        elif isinstance(resolution, PendingGateResolution.Ambiguous):
            return BridgeOutcome.Respond(
                "多个待处理的批准或认证提示正在等待。请从原始线程回复。"
            )

        # PendingGateResolution.Resolved(其他) 或 PendingGateResolution.None：继续

        # 检查孤立的等待线程
        if scoped_thread_id is not None:
            orphaned = await fail_orphaned_waiting_thread_if_needed(
                state, message.user_id, scoped_thread_id
            )
            if orphaned:
                return BridgeOutcome.Respond(
                    "此线程正在等待批准或认证，但该待处理状态已丢失。"
                    "线程已标记为失败；请重新发送您的请求。"
                )

        # 安全检查——镜像 v1 管道中的 thread_ops::process_user_input，
        # 以便两个引擎路径强制执行相同的入站保护。当消息携带附件时，
        # 空文本主体是合法的（附件是有效载荷）；跳过验证器的空输入拒绝，
        # 但仍对文本应用长度/策略检查。
        trimmed_content = content.strip()
        skip_empty_check = trimmed_content == "" and len(message.attachments) > 0

        if not skip_empty_check:
            validation = agent.safety().validate_input(content)
            if not validation.is_valid:
                details = "; ".join(
                    f"{e.field}: {e.message}" for e in validation.errors
                )
                return BridgeOutcome.Respond(
                    f"输入被安全验证拒绝: {details}"
                )

        violations = agent.safety().check_policy(content)
        if any(rule.action == PolicyAction.Block for rule in violations):
            return BridgeOutcome.Respond(
                "输入被安全策略拒绝。"
            )

        # 扫描入站消息中的密钥（API 密钥、令牌）。
        # 在此处捕获它们可防止 LLM 回显它们，否则会触发出站泄漏检测器
        # 并创建错误循环。
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

        persisted_attachments = list(message.attachments)
        attachment_notes = await persist_project_attachments(
            state.project_root,
            message,
            project_id,
            persisted_attachments,
        )

        # 引擎 v2 线程目前仅支持文本，因此附件必须折叠到
        # 有效的用户内容中，然后再路由到引擎。这会在引擎线程
        # 和双重写入的网关历史记录中保留提取的文档文本、
        # 项目本地文件路径和附件元数据。
        augmented = augment_with_attachments(content, persisted_attachments)
        effective_content = augmented.text if augmented is not None else content

        # 触发任何活动的 OnEvent 任务，其模式（和可选的通道过滤器）
        # 匹配此入站消息。此处的任务触发是消息的副作用——
        # 独立于且并行于下面生成的正常对话线程。
        # 错误会被记录，但永远不会阻塞面向用户的消息处理。
        #
        # v1 创建的例行任务不会被此路径触及：它们存在于 v1 例行任务存储中，
        # 并由 v1 RoutineEngine 在后台触发。通过 routine_create 别名创建的
        # 任务存在于引擎存储中，并在此处触发。
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

        # 按 (通道, 用户, 线程) 限定引擎对话范围。
        # 当前端发送 thread_id 时（用户创建了新对话），
        # 将其用作通道键的一部分，以便每个 v1 线程映射到不同的
        # 引擎对话。没有这个，所有线程共享一个对话，
        # 消息将出现在错误的位置。
        scope = message.conversation_scope()
        channel_key = (
            f"{message.channel}:{scope}" if scope is not None else message.channel
        )

        # 为此限定范围的 channel+user 获取或创建对话
        conv_id = await state.conversation_manager.get_or_create_conversation(
            channel_key, message.user_id
        )

        # 在将时区传递给引擎之前验证通道提供的时区。
        # ValidTimezone.parse 拒绝空/无效字符串；我们发送规范的
        # IANA 名称（而不是原始输入），以便下游消费者看到已知良好的值。
        # 必须在 spawn 时传入——在线程启动后设置元数据
        # 对内存执行器在第一轮中不可见。
        validated_tz = ValidTimezone.parse(message.timezone) if message.timezone else None

        # 检测执行意图并相应地配置义务
        thread_config = ThreadConfig.default()
        if user_signals_execution_intent(content):
            thread_config.require_action_attempt = True

        # 将对话范围（可解析为 Uuid）标记到线程的
        # `initial_metadata` 中。引擎将其读回
        # `ThreadExecutionContext.conversation_scope`，这让桥接的
        # `EffectBridgeAdapter` 可以通过引擎 `thread_id` 或调用方范围
        # 解析每对话状态（目前：调用方提供的外部工具目录）。
        # 没有这个，spawn 后立即启动的执行器任务将与桥接的
        # spawn 后 `transfer` 竞争，并在第一轮中错过调用方工具。
        scope_uuid = parse_engine_thread_id(scope)
        extra_metadata = None
        if scope_uuid is not None:
            extra_metadata = {
                "conversation_scope": str(scope_uuid),
            }

        # 在引擎生成线程之前预绑定每执行上下文。
        # `handle_user_message` 在内部分配并启动引擎任务；
        # 如果在 `set_execution_context` 落地之前触发了快速工具门控，
        # 控制器的 `pause()` 将找不到条目并静默取消门控。
        # 预执行槽按 user_id 和上游的每对话锁保证
        # 每个对话最多只有一个桥接回合在执行中。
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

        # 处理消息——生成新线程或注入活动线程。
        # 出错时，我们必须清除刚刚安装的预执行槽：
        # 没有这个，失败的 `handle_user_message`（引擎 spawn/inject
        # 在分配任何 thread_id 之前失败）会留下一个按 user_id 键控的
        # 过期条目，该条目会错误路由同一用户的下一个门控提示。
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

        # 将预执行条目提升为 (user, thread) 键控。从此处开始，
        # 来自此线程的门控首先落在 thread-keyed 条目上；
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
