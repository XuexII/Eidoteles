# 内联门控等待桥接控制器。
#
# 为桥接层实现 [`ironclaw_engine::GateController`]。
# 当引擎在实时执行中（第 0 层批量或第 1 层 CodeAct VM）遇到 `Approval` 门控时，它会调用
# [`BridgeGateController::pause`]，该函数：
#
# 1. 构建并持久化一个 [`PendingGate`]（现有的 UI 机制通过相同的存储/SSE/频道流程发现提示）。
# 2. 在与解析端点共享的进程级注册表中，注册一个以 `request_id` 为键的 [`oneshot::Sender`]。
# 3. 等待接收器。该 future 将在此处保持驻留，保持引擎调用栈处于打开状态，直到用户解析门控。
#
# 在解析侧，[`GateResolutions::try_deliver`] 通过 `request_id` 查找发送器，并将 [`GateResolution`] 传递回已挂起的引擎。
# 引擎从确切的挂起点继续执行——无需重新进入、无需重放、无需重复执行同一步骤中先前的副作用。
#
# ## 单一实例，按线程上下文
#
# 控制器是一个共享的单一实例（由 `EngineState` 持有，在启动时附加到 `ThreadManager`）。
# 每次执行的数据（会话 ID、频道元数据、原始消息、范围线程 ID）驻留在以 `(user_id, thread_id)` 为键的 `HashMap` 中。
# 桥接器在调用 `ConversationManager::handle_user_message` 之前填充一个条目；如果在该执行期间触发了门控，控制器会查找该条目以构建 `PendingGate`。
# 过时的条目（来自未触发门控的已完成轮次）会在调用后由桥接器移除。


from ironclaw_common import AppEvent
from ironclaw_common import ExternalThreadId
from engine import (
    ConversationId, GateController, GatePafromRequest, GateResolution, ResumeKind, ThreadId,
)

from auth.extension import AuthManager
from channels import ChannelManager
from channels import StatusUpdate
from channels.web.sse import SseManager
from extensions.ExtensionManager
from gate.pending import PendingGate
from gate.store import PendingGateStore
from tools import ToolRegistry

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Set, Any, Tuple
from enum import Enum
import logging

logger = logging.getLogger(__name__)


# ── 每执行上下文 ─────────────────────────────────────────────

@dataclass
class PerExecutionContext:
    """控制器构建 `PendingGate` 所需的每执行数据。
    在桥接调用引擎之前填充，之后移除
    """
    conversation_id: ConversationId
    source_channel: str
    scope_thread_id: Optional[ExternalThreadId] = None
    channel_metadata: dict = field(default_factory=dict)
    original_message: Optional[str] = None


# ── 执行键 ───────────────────────────────────────────────────

@dataclass(frozen=True)
class ExecutionKey:
    """执行注册表键。按 `(user_id, thread_id)` 索引"""
    user_id: str
    thread_id: ThreadId


@dataclass(frozen=True)
class PreExecKey:
    """预执行注册表键。按 `(user_id, conversation_id)` 索引，
    以便同一用户的两个并发对话（例如两个浏览器标签页）不会
    在每个轮次提升为其自己的 `(user_id, thread_id)` 条目之前相互覆盖
    """
    user_id: str
    conversation_id: ConversationId


# ── 门控解决方案注册表 ──────────────────────────────────────

class GateResolutions:
    """进程中飞行门控解决方案通道的注册表

    每个挂起的飞行门控一个条目。插入来自 [`BridgeGateController::pause`]；
    移除来自 [`GateResolutions::try_deliver`]（解决方案端点）

    认证门控额外将其 `request_id` 注册在其等待的 `(user_id, credential_name)` 对下，
    以便 OAuth 回调路径可以按凭证名称唤醒暂停的 VM，而无需知道引擎内部的 request_id —
    范围限定为每用户，因此一个帐户下的凭证写入永远不会唤醒恰好共享相同凭证名称的
    不同帐户的暂停门控

    来自先前崩溃的滞留条目不存在 — 重新启动进程会丢弃注册表。
    在重新启动后存活的过时 `PendingGate` 行由 `router.rs` 中的启动扫描清理
    """

    def __init__(self):
        self._inner: Dict[uuid.UUID, asyncio.Future] = {}
        # 二级索引: `(user_id, credential_name)` → 在其上暂停的 request_ids
        self._by_credential: Dict[Tuple[str, str], Set[uuid.UUID]] = {}
        self._lock = asyncio.Lock()

    async def try_deliver(self, request_id: uuid.UUID, resolution: GateResolution) -> bool:
        """向暂停的调用者交付解决方案。如果为 `request_id` 注册了发送者则返回 `True`
        （引擎正在等待），否则返回 `False`（无活跃 VM — 回退到旧版重新进入）
        """
        async with self._lock:
            future = self._inner.pop(request_id, None)
            # 尽力而为：删除指向此 request_id 的任何凭证索引条目
            for key_set in self._by_credential.values():
                key_set.discard(request_id)
            # 清理空集合
            self._by_credential = {
                k: v for k, v in self._by_credential.items() if v
            }

        if future is not None and not future.done():
            future.set_result(resolution)
            return True
        return False

    async def deliver_for_credential(self, user_id: str, credential_name: str) -> int:
        """向等待 `(user_id, credential_name)` 的每个暂停认证门控交付 `Approved`。
        返回唤醒的等待者数量。由 OAuth 回调路径使用：当写入凭证时，
        属于 `user_id` 的每个在该凭证上被阻塞的暂停工具调用
        （Tier 0 或 Tier 1，前台或任务子线程）可以内联恢复并针对现在存在的
        密钥重试动作。同一凭证名称上的其他用户的暂停门控保持不变
        """
        key = (user_id, credential_name)
        async with self._lock:
            request_ids = list(self._by_credential.pop(key, set()))

        delivered = 0
        for request_id in request_ids:
            if await self.try_deliver(request_id, GateResolution.Approved(always=False)):
                delivered += 1
        return delivered

    async def register(self, request_id: uuid.UUID, future: asyncio.Future) -> None:
        """注册 request_id 和对应的 future"""
        async with self._lock:
            self._inner[request_id] = future

    async def register_credential(
            self, user_id: str, credential_name: str, request_id: uuid.UUID
    ) -> None:
        """将 `request_id` 注册在 `(user_id, credential_name)` 下，
        以便 OAuth 完成后可以按凭证名称唤醒它，范围限定为拥有者用户
        """
        key = (user_id, credential_name)
        async with self._lock:
            if key not in self._by_credential:
                self._by_credential[key] = set()
            self._by_credential[key].add(request_id)

    async def forget(self, request_id: uuid.UUID) -> None:
        """移除注册的 request_id"""
        async with self._lock:
            self._inner.pop(request_id, None)
            for key_set in self._by_credential.values():
                key_set.discard(request_id)
            self._by_credential = {
                k: v for k, v in self._by_credential.items() if v
            }


# ── 桥接门控控制器 ──────────────────────────────────────────

class BridgeGateController:
    """单一共享控制器。线程化到引擎为活跃执行构建的每个 `ThreadExecutionContext` 中"""

    def __init__(
            self,
            pending_gates: PendingGateStore,
            sse: Optional[SseManager] = None,
            tools: ToolRegistry = None,
            auth_manager: Optional[AuthManager] = None,
            extension_manager: Optional[ExtensionManager] = None,
            channels: ChannelManager = None,
            resolutions: GateResolutions = None,
    ):
        self.pending_gates = pending_gates
        self.sse = sse
        self.tools = tools
        self.auth_manager = auth_manager
        self.extension_manager = extension_manager
        self.channels = channels
        self.resolutions = resolutions or GateResolutions()
        # 每 (user, thread) 注册表。在桥接知道引擎为某个轮次生成的线程后填充
        self._per_execution: Dict[ExecutionKey, PerExecutionContext] = {}
        # 预执行注册表，在 `handle_user_message` 返回 thread_id *之前* 填充
        self._pre_execution: Dict[PreExecKey, PerExecutionContext] = {}
        # 每 (user, thread) `pause()` 序列化锁
        self._gate_locks: Dict[ExecutionKey, asyncio.Lock] = {}
        # 每 ThreadId 的飞行中暂停 request_ids 注册表
        self._active_pauses: Dict[ThreadId, Set[uuid.UUID]] = {}
        self._lock = asyncio.Lock()

    async def _track_active_pause(self, thread_id: ThreadId, request_id: uuid.UUID) -> None:
        """跟踪活跃的暂停"""
        async with self._lock:
            if thread_id not in self._active_pauses:
                self._active_pauses[thread_id] = set()
            self._active_pauses[thread_id].add(request_id)

    async def _untrack_active_pause(self, thread_id: ThreadId, request_id: uuid.UUID) -> None:
        """取消跟踪活跃的暂停"""
        async with self._lock:
            if thread_id in self._active_pauses:
                self._active_pauses[thread_id].discard(request_id)
                if not self._active_pauses[thread_id]:
                    del self._active_pauses[thread_id]

    async def _gate_lock_for(self, key: ExecutionKey) -> asyncio.Lock:
        """查找（或创建）每 (user, thread) 门控序列化锁"""
        async with self._lock:
            if key not in self._gate_locks:
                self._gate_locks[key] = asyncio.Lock()
            return self._gate_locks[key]

    async def set_pre_execution_context(
            self, user_id: str, conversation_id: ConversationId, context: PerExecutionContext
    ) -> None:
        """在引擎生成线程之前绑定 `(user_id, conversation_id)` 的每执行数据。
        关闭快速工具门控在 (user, thread) 键控条目被写入之前到达 `pause()` 的竞态窗口

        按 `conversation_id`（而不是仅 `user_id`）索引，防止同一用户的并发对话 —
        多个浏览器标签页、与前台轮次同时运行的后台任务 — 相互覆盖槽位
        """
        key = PreExecKey(user_id=user_id, conversation_id=conversation_id)
        async with self._lock:
            self._pre_execution[key] = context

    async def set_execution_context(
            self, user_id: str, thread_id: ThreadId, context: PerExecutionContext
    ) -> None:
        """一旦引擎分配了 thread_id，绑定 `(user_id, thread_id)` 的每执行数据。
        在 [`set_pre_execution_context`] 之后调用；取代后续查找的 (user, conversation_id) 键控条目
        """
        conv_id = context.conversation_id
        key = ExecutionKey(user_id=user_id, thread_id=thread_id)
        pre_key = PreExecKey(user_id=user_id, conversation_id=conv_id)
        async with self._lock:
            self._per_execution[key] = context
            self._pre_execution.pop(pre_key, None)

    async def clear_pre_execution_context(
            self, user_id: str, conversation_id: ConversationId
    ) -> None:
        """丢弃预执行 `(user, conversation)` 键控条目，不触及任何 `(user, thread)` 键控条目。
        在桥接错误路径上使用，当 `handle_user_message` 在分配 thread_id 之前失败时 —
        没有此步骤，槽位将泄漏并可能错误路由同一对话的下一个门控提示
        """
        key = PreExecKey(user_id=user_id, conversation_id=conversation_id)
        async with self._lock:
            self._pre_execution.pop(key, None)

    async def clear_execution_context(
            self, user_id: str, thread_id: ThreadId, conversation_id: ConversationId
    ) -> None:
        """丢弃每执行数据。幂等。`conversation_id` 是此轮次的发起对话，
        因此任何剩余的预执行槽位（例如当桥接在提升之前退出时）也会被清除
        """
        exec_key = ExecutionKey(user_id=user_id, thread_id=thread_id)
        pre_key = PreExecKey(user_id=user_id, conversation_id=conversation_id)
        async with self._lock:
            self._per_execution.pop(exec_key, None)
            self._pre_execution.pop(pre_key, None)
            self._gate_locks.pop(exec_key, None)

    async def try_deliver(self, request_id: uuid.UUID, resolution: GateResolution) -> bool:
        """将解决方案转发到内联等待注册表。如果引擎正在主动等待则返回 `True`"""
        return await self.resolutions.try_deliver(request_id, resolution)

    async def _lookup_per_execution(
            self,
            user_id: str,
            thread_id: ThreadId,
            conversation_id: Optional[ConversationId] = None,
    ) -> Optional[PerExecutionContext]:
        """查找每执行上下文。首先匹配最具体的：(user, thread)。
        回退到 (user, conversation) 键控的预执行条目，
        以便在 `set_execution_context` 落地之前触发的门控仍然找到其上下文
        """
        exec_key = ExecutionKey(user_id=user_id, thread_id=thread_id)
        async with self._lock:
            if exec_key in self._per_execution:
                return self._per_execution[exec_key]

            if conversation_id is not None:
                pre_key = PreExecKey(user_id=user_id, conversation_id=conversation_id)
                return self._pre_execution.get(pre_key)

        return None

    async def _build_pending_gate(
            self,
            request_id: uuid.UUID,
            per_exec: PerExecutionContext,
            user_id: str,
            thread_id: ThreadId,
            req: GatePauseRequest,
    ) -> PendingGate:
        """构建挂起门控"""
        # 编辑敏感参数用于显示
        display_parameters = req.parameters
        if self.tools is not None:
            tool = await self.tools.get(req.action_name) if hasattr(self.tools, 'get') else None
            if tool is not None:
                display_parameters = redact_params(req.parameters, tool.sensitive_params())

        now = datetime.now(timezone.utc)
        return PendingGate(
            request_id=request_id,
            gate_name=req.gate_name,
            user_id=user_id,
            thread_id=thread_id,
            scope_thread_id=per_exec.scope_thread_id,
            conversation_id=per_exec.conversation_id,
            source_channel=per_exec.source_channel,
            action_name=req.action_name,
            call_id=req.call_id,
            parameters=req.parameters,
            display_parameters=display_parameters,
            description=f"工具 '{req.action_name}' 需要 {req.resume_kind.kind_name()}（门控: {req.gate_name}）",
            resume_kind=req.resume_kind,
            created_at=now,
            expires_at=now + timedelta(minutes=30),
            original_message=per_exec.original_message,
            resume_output=None,
            paused_lease=None,
            approval_already_granted=False,
        )

    async def _emit_gate_prompt(self, pending: PendingGate, channel_metadata: dict) -> None:
        """发出门控提示到 SSE 和频道"""
        extension_name = await resolve_auth_gate_extension_name(
            self.auth_manager,
            self.extension_manager,
            self.tools,
            pending,
        )

        display_parameters = gate_display_parameters(pending)

        # 广播到 SSE
        if self.sse is not None:
            self.sse.broadcast_for_user(
                pending.user_id,
                AppEvent.GateRequired(
                    request_id=str(pending.request_id),
                    gate_name=pending.gate_name,
                    tool_name=pending.action_name,
                    description=pending.description,
                    parameters=json.dumps(display_parameters, indent=2, ensure_ascii=False),
                    extension_name=extension_name,
                    resume_kind=pending.resume_kind.to_dict() if hasattr(pending.resume_kind, 'to_dict') else str(
                        pending.resume_kind),
                    thread_id=pending.effective_wire_thread_id(),
                ),
            )

        # 发送到频道
        if isinstance(pending.resume_kind, ResumeKind.Approval):
            await self.channels.send_status(
                pending.source_channel,
                StatusUpdate.ApprovalNeeded(
                    request_id=str(pending.request_id),
                    tool_name=pending.action_name,
                    description=pending.description,
                    parameters=display_parameters,
                    allow_always=pending.resume_kind.allow_always,
                ),
                channel_metadata,
            )
        elif isinstance(pending.resume_kind, ResumeKind.Authentication):
            if extension_name is None:
                logger.debug(
                    f"认证门控在没有解析扩展名称的情况下到达 emit_gate_prompt: "
                    f"gate={pending.gate_name}, request_id={pending.request_id}"
                )
                return
            await self.channels.send_status(
                pending.source_channel,
                StatusUpdate.AuthRequired(
                    extension_name=extension_name,
                    instructions=pending.resume_kind.instructions,
                    auth_url=pending.resume_kind.auth_url,
                    setup_url=None,
                    request_id=str(pending.request_id),
                ),
                channel_metadata,
            )

    # ── GateController 接口实现 ────────────────────────────

    async def pause(self, request: GatePauseRequest) -> GateResolution:
        """暂停执行等待门控解决方案。
        内联门控等待处理 Approval 和 Authentication。
        External 恢复类型保留旧版 `ThreadOutcome::GatePaused` 重新进入路径，
        因为其解决方案安装的回调负载状态无法在不展开的情况下传回暂停的调用
        """
        # External 恢复类型作为 Cancelled 处理
        if isinstance(request.resume_kind, ResumeKind.External):
            logger.debug(
                f"BridgeGateController: External 恢复类型到达内联等待；取消: "
                f"kind={request.resume_kind.kind_name()}"
            )
            return GateResolution.Cancelled()

        # 查找每执行上下文
        per_exec = await self._lookup_per_execution(
            request.user_id, request.thread_id, request.conversation_id,
        )
        if per_exec is None:
            # 没有注册每执行上下文。当通过 `handle_with_engine` 调用时不应发生，
            # 该方法始终在调用引擎之前填充它。任务/后台线程今天也会到达此处；
            # 它们回退到旧版 `ThreadOutcome::GatePaused` 展开路径
            logger.debug(
                f"BridgeGateController: 没有每执行上下文 — 取消（任务/后台路径）: "
                f"user={request.user_id}, thread={request.thread_id}, "
                f"kind={request.resume_kind.kind_name()}"
            )
            return GateResolution.Cancelled()

        # 按 (user, thread) 序列化并发内联门控
        exec_key = ExecutionKey(user_id=request.user_id, thread_id=request.thread_id)
        gate_lock = await self._gate_lock_for(exec_key)
        async with gate_lock:
            request_id = uuid.uuid4()
            pending = await self._build_pending_gate(
                request_id, per_exec, request.user_id, request.thread_id, request,
            )

            # 插入挂起门控存储
            try:
                await self.pending_gates.insert(pending)
            except Exception as e:
                logger.debug(
                    f"BridgeGateController: pending_gates.insert 被拒绝；视为取消: "
                    f"user={request.user_id}, thread={request.thread_id}, error={e}"
                )
                return GateResolution.Cancelled()

            # 注册 future 等待解决方案
            future = asyncio.get_running_loop().create_future()
            await self.resolutions.register(request_id, future)

            # 对于认证门控，也按凭证名称索引此 request_id
            if isinstance(request.resume_kind, ResumeKind.Authentication):
                await self.resolutions.register_credential(
                    request.user_id,
                    request.resume_kind.credential_name,
                    request_id,
                )

            # 跟踪此飞行中暂停以便 `cancel_thread()` 可以唤醒它
            await self._track_active_pause(request.thread_id, request_id)

            # 发出门控提示
            await self._emit_gate_prompt(pending, per_exec.channel_metadata)

            # 等待解决方案或过期
            timeout_seconds = max(
                0.0,
                (pending.expires_at - datetime.now(timezone.utc)).total_seconds(),
            )

            try:
                resolution = await asyncio.wait_for(future, timeout=timeout_seconds)
            except asyncio.TimeoutError:
                # 过期：清理并作为 Cancelled 返回
                await self.resolutions.forget(request_id)
                try:
                    await self.pending_gates.discard(pending.key())
                except Exception:
                    pass
                logger.debug(
                    f"BridgeGateController: 暂停在解决方案之前过期；取消: "
                    f"user={request.user_id}, thread={request.thread_id}, request_id={request_id}"
                )
                resolution = GateResolution.Cancelled()
            except Exception:
                # Future 被取消或其他错误
                await self.resolutions.forget(request_id)
                resolution = GateResolution.Cancelled()

            # 退出时始终取消跟踪
            await self._untrack_active_pause(request.thread_id, request_id)

        return resolution

    async def cancel_thread(self, thread_id: ThreadId) -> None:
        """取消线程的所有挂起门控"""
        async with self._lock:
            request_ids = list(self._active_pauses.pop(thread_id, set()))

        if not request_ids:
            return

        logger.debug(
            f"BridgeGateController::cancel_thread: 唤醒暂停的门控: "
            f"thread={thread_id}, count={len(request_ids)}"
        )

        for request_id in request_ids:
            await self.resolutions.try_deliver(request_id, GateResolution.Cancelled())

        # 丢弃此线程的任何挂起数据库行
        try:
            await self.pending_gates.discard_for_thread(thread_id)
        except Exception:
            pass