# 线程管理器——线程生命周期的顶层编排器。

from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from engine.runtime.messaging import (
SignalSender,
ThreadOutcome,
ThreadSignal
)

logger = logging.getLogger(__name__)

# 线程上有一个进行中的待处理批准门控时设置的元数据键。
# 携带此键的持久化线程跳过重启恢复，以便门控在进程重启后仍然存在。
PENDING_APPROVAL_METADATA_KEY = "pending_approval"

# 线程上有一个序列化的运行时检查点时设置的元数据键
# （CodeAct VM 状态、nudge 计数器、压缩计数）。携带此键的线程
# 在重启时挂起而不是失败。
RUNTIME_CHECKPOINT_METADATA_KEY = "runtime_checkpoint"

# 在线程因进程在完成前重启而被 [`ThreadManager::recover_project_threads`]
# 强制置为 `Failed` 时，在线程上设置的元数据键。线程并非因用户可见的原因失败；
# 项目的"需要关注"界面过滤掉这些，以便升级不会级联成一堵幽灵故障警告墙。
ENGINE_RESTART_RECOVERY_METADATA_KEY = "engine_restart_recovery"


@dataclass
class RunningThread:
    """用于检查结果的运行中线程的句柄。"""
    signal_tx: SignalSender
    handle: asyncio.Task

class ThreadManager:
    """
    线程生命周期的顶层编排器。

    管理线程生成、监督、信号和树关系。
    """

    def __init__(
        self,
        llm: LlmBackend,
        effects: EffectExecutor,
        store: Store,
        capabilities: CapabilityRegistry,
        leases: LeaseManager,
        policy: PolicyEngine,
    ):
        self.llm = llm
        self.effects = effects
        self.store = store
        self.capabilities = capabilities
        self.leases = leases
        self.policy = policy
        self.lease_planner = LeasePlanner()
        self.tree: Dict[ThreadId, List[ThreadId]] = {}
        self._tree_parents: Dict[ThreadId, ThreadId] = {}
        self._tree_lock = asyncio.Lock()
        self.running: Dict[ThreadId, RunningThread] = {}
        self._running_lock = asyncio.Lock()
        self._completed: Dict[ThreadId, ThreadOutcome] = {}
        self._completed_lock = asyncio.Lock()
        # 用于线程事件的广播通道（用于实时状态更新）
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        # 主机提供的回调，将 `Approval` 门控转换为内联等待而不是展开调用栈。
        # 引擎将其附加到每个 `ThreadExecutionContext`，以便 Tier 0 和 Tier 1
        # 执行器都可以原地暂停活动 VM。
        #
        # 默认为 [`CancellingGateController`]——每个门控都以类型化拒绝取消。
        # 想要真正内联等待的主机在引导期间调用 [`set_gate_controller`]。
        self.gate_controller: GateController = CancellingGateController()
        self._gate_controller_lock = asyncio.Lock()

    async def set_gate_controller(self, controller: GateController) -> None:
        """
        安装（或替换）主机提供的门控控制器。

        在桥接引导期间调用一次。后续线程生成会拾取控制器
        并将其传播到它们构造的每个 `ThreadExecutionContext` 中。
        """
        async with self._gate_controller_lock:
            self.gate_controller = controller

    async def gate_controller(self) -> GateController:
        """获取当前门控控制器的快照。"""
        async with self._gate_controller_lock:
            return self.gate_controller

    def subscribe_events(self) -> asyncio.Queue:
        """订阅线程事件以获取实时状态更新。"""
        # 每个订阅者获得自己的队列
        queue: asyncio.Queue = asyncio.Queue()
        return queue

    async def _broadcast_event(self, event: ThreadEvent) -> None:
        """向所有订阅者广播事件。"""
        # 注意：简化实现，使用列表存储订阅者
        pass

    async def spawn_thread(
        self,
        goal: str,
        thread_type: ThreadType,
        project_id: ProjectId,
        config: ThreadConfig,
        parent_id: Optional[ThreadId],
        user_id: str,
    ) -> ThreadId:
        """生成新线程并开始执行。"""
        return await self.spawn_thread_with_history(
            goal=goal,
            title=None,
            thread_type=thread_type,
            project_id=project_id,
            config=config,
            parent_id=parent_id,
            user_id=user_id,
            initial_messages=[],
            initial_metadata={},
        )

    async def spawn_thread_with_title(
        self,
        goal: str,
        title: Optional[str],
        thread_type: ThreadType,
        project_id: ProjectId,
        config: ThreadConfig,
        parent_id: Optional[ThreadId],
        user_id: str,
    ) -> ThreadId:
        """使用显式侧边栏标题生成新线程。"""
        return await self.spawn_thread_with_history(
            goal=goal,
            title=title,
            thread_type=thread_type,
            project_id=project_id,
            config=config,
            parent_id=parent_id,
            user_id=user_id,
            initial_messages=[],
            initial_metadata={},
        )

    async def spawn_thread_with_history(
        self,
        goal: str,
        title: Optional[str],
        thread_type: ThreadType,
        project_id: ProjectId,
        config: ThreadConfig,
        parent_id: Optional[ThreadId],
        user_id: str,
        initial_messages: List[ThreadMessage],
        initial_metadata: Dict[str, Any],
    ) -> ThreadId:
        """
        使用初始对话历史生成线程。

        `initial_metadata` 在后台执行任务启动**之前**应用于线程的元数据映射，
        因此执行器的内存中的 `Thread` 在第一步就能观察到这些键。
        这是标记编排器第一步需要读取的元数据（例如用于 `mission_create`
        通知通道默认值的 `source_channel`，或用于 cron 解析的 `user_timezone`）的
        唯一正确方式。在生成后通过 `set_thread_metadata` 设置元数据是竞态——
        生成的任务拥有其自己的内存 `Thread` 副本，后期更新仅落在运行中任务
        永远不会重新读取的持久化副本上。
        """
        thread = Thread.new(goal, thread_type, project_id, user_id, config)
        if parent_id is not None:
            thread = thread.with_parent(parent_id)
        # 在 save_thread + start_thread 之前设置标题，以便执行器的内存线程原子性地观察到它
        thread.title = title
        thread_id = thread.id

        # 在 save_thread + start_thread 之前应用初始元数据，以便执行器的内存线程在第一步观察到它
        if initial_metadata:
            if not isinstance(thread.metadata, dict):
                thread.metadata = {}
            thread.metadata.update(initial_metadata)

        # 在树中注册
        if parent_id is not None:
            async with self._tree_lock:
                if parent_id not in self.tree:
                    self.tree[parent_id] = []
                self.tree[parent_id].append(thread_id)
                self._tree_parents[thread_id] = parent_id

        # 根据线程类型授予显式能力租约
        for grant in self.lease_planner.plan_for_thread(thread_type, self.capabilities):
            lease = await self.leases.grant(
                thread_id, grant.capability_name, grant.granted_actions, None, None
            )
            await self.store.save_lease(lease)
            thread.capability_leases.append(lease.id)

        # 添加来自先前线程的对话历史（用于上下文连续性）
        for msg in initial_messages:
            thread.messages.append(msg)

        # 将目标添加为当前用户消息，以便 LLM 有上下文
        thread.add_message(ThreadMessage.user(thread.goal))

        # 持久化
        await self.store.save_thread(thread)

        return await self._start_thread(thread, user_id, False)

    async def resume_thread(
        self,
        thread_id: ThreadId,
        user_id: str,
        injected_message: Optional[ThreadMessage] = None,
        approval_event: Optional[Tuple[str, bool]] = None,
        resolved_call_id: Optional[str] = None,
    ) -> None:
        """恢复持久化的等待或挂起线程。"""
        if await self.is_running(thread_id):
            raise EngineError.Thread(ThreadError.AlreadyRunning(thread_id))

        thread = await self.store.load_thread(thread_id)
        if thread is None:
            raise EngineError.ThreadNotFound(thread_id)

        # 租户隔离：验证请求用户是否拥有此线程
        if not thread.is_owned_by(user_id):
            raise EngineError.AccessDenied(
                user_id=user_id,
                entity=f"thread {thread_id}",
            )

        if thread.state not in (ThreadState.Waiting, ThreadState.Suspended):
            raise EngineError.Store(
                reason=f"thread {thread_id} is not resumable from {thread.state}"
            )

        if approval_event is not None:
            call_id, approved = approval_event
            event = ThreadEvent.new(
                thread_id,
                EventKind.ApprovalReceived(call_id=call_id, approved=approved),
            )
            await self._broadcast_event(event)
            thread.events.append(event)
            thread.updated_at = datetime.now(timezone.utc)

        if resolved_call_id is not None:
            preserve_assistant_call = (
                injected_message is not None
                and injected_message.role == MessageRole.ActionResult
                and injected_message.action_call_id == resolved_call_id
            )
            thread.messages = [
                existing for existing in thread.messages
                if not (
                    _is_resolved_action_result_message(existing, resolved_call_id)
                    if preserve_assistant_call
                    else _is_resolved_call_message(existing, resolved_call_id)
                )
            ]

        if injected_message is not None:
            thread.add_internal_message(injected_message)
            thread.add_message(injected_message)

        # 在批准/认证上暂停的 Waiting 线程应从新注入的上下文恢复，
        # 而不是重放旧的检查点中断。Suspended 线程保留其检查点以重新启动。
        if thread.state == ThreadState.Waiting:
            thread.metadata.pop("runtime_checkpoint", None)

        await self.store.save_thread(thread)
        await self._start_thread(thread, user_id, True)

    async def _start_thread(
        self,
        thread: Thread,
        user_id: str,
        is_resume: bool,
    ) -> ThreadId:
        """启动线程执行的后台任务。"""
        thread_id = thread.id

        await _reconcile_dynamic_tool_lease(
            thread, self.effects, self.leases, self.store, self.lease_planner
        )

        # 创建信号通道
        signal_queue: asyncio.Queue = asyncio.Queue(maxsize=32)

        # 构建执行循环
        gate_controller = await self.gate_controller()
        retrieval = RetrievalEngine(self.store)
        exec_loop = ExecutionLoop(
            thread=thread,
            llm=self.llm,
            effects=self.effects,
            leases=self.leases,
            policy=self.policy,
            signal_rx=signal_queue,
            user_id=user_id,
            gate_controller=gate_controller,
        )
        exec_loop = exec_loop.with_capabilities(self.capabilities)
        exec_loop = exec_loop.with_event_tx(self.event_queue)
        exec_loop = exec_loop.with_retrieval(retrieval)
        exec_loop = exec_loop.with_store(self.store)

        # 生成后台任务
        async def _run_thread():
            result = await exec_loop.run()
            logger.debug("线程执行完成, thread_id=%s", thread_id)

            # 运行回溯跟踪分析（非 LLM，始终运行）。
            # 问题由自我改进任务通过事件监听器拾取。
            trace = build_trace(exec_loop.thread)
            if trace.issues:
                log_trace_summary(trace)

            # 转换 Completed → Done
            if exec_loop.thread.state == ThreadState.Completed:
                try:
                    exec_loop.thread.transition_to(ThreadState.Done, None)
                except Exception as e:
                    logger.debug(
                        "转换到 Done 失败, thread_id=%s: %s", thread_id, e
                    )

            # 跟踪录制由主机 crate 中的 `RecordingLlm` 集中处理
            # （由 `IRONCLAW_RECORD_TRACE` 控制）。引擎不再写入自己的 JSON 跟踪文件。

            try:
                await self.store.append_events(exec_loop.thread.events)
            except Exception as e:
                logger.debug(
                    "持久化线程事件失败, thread_id=%s: %s", thread_id, e
                )

            # 将最终线程状态保存到存储
            try:
                await self.store.save_thread(exec_loop.thread)
            except Exception as e:
                logger.debug(
                    "保存最终线程状态失败, thread_id=%s: %s", thread_id, e
                )

            if isinstance(result, Exception):
                outcome = ThreadOutcome.Failed(
                    error=str(result),
                    debug_detail=getattr(result, 'debug_detail', lambda: None)(),
                )
            else:
                outcome = result

            async with self._completed_lock:
                self._completed[thread_id] = outcome
            async with self._running_lock:
                self.running.pop(thread_id, None)

        handle = asyncio.create_task(_run_thread())

        async with self._running_lock:
            self.running[thread_id] = RunningThread(
                signal_tx=signal_queue,
                handle=handle,
            )

        if is_resume:
            logger.debug("已恢复线程, thread_id=%s", thread_id)

        return thread_id

    async def stop_thread(self, thread_id: ThreadId, user_id: str) -> None:
        """
        向运行中的线程发送停止信号。

        在发送 `ThreadSignal::Stop` 之前，先唤醒当前停在此线程上的
        任何 [`GateController::pause`] future，以便引擎任务能及时观察到停止。
        没有显式取消，停在 `pause()` 内部的线程（内联批准等待）不会轮询信号通道，
        将继续等待直到用户解析提示或门控过期。
        """
        # 在允许停止之前验证所有权
        thread = await self.store.load_thread(thread_id)
        if thread is not None and not thread.is_owned_by(user_id):
            raise EngineError.AccessDenied(
                user_id=user_id,
                entity=f"thread {thread_id}",
            )

        # 首先唤醒阻塞在此线程上的任何内联门控等待。
        # 控制器在生成的引擎任务之间共享；在执行器内部轮询的暂停 `pause()` future
        # 不会直接看到 `ThreadSignal::Stop`。
        controller = await self.gate_controller()
        await controller.cancel_thread(thread_id)

        async with self._running_lock:
            rt = self.running.get(thread_id)
        if rt is not None:
            try:
                await rt.signal_tx.put(ThreadSignal.Stop)
            except asyncio.QueueFull:
                pass
        else:
            raise EngineError.ThreadNotFound(thread_id)

    async def inject_message(
        self,
        thread_id: ThreadId,
        user_id: str,
        message: ThreadMessage,
    ) -> None:
        """向运行中的线程注入用户消息。"""
        # 在允许注入之前验证所有权
        thread = await self.store.load_thread(thread_id)
        if thread is not None and not thread.is_owned_by(user_id):
            raise EngineError.AccessDenied(
                user_id=user_id,
                entity=f"thread {thread_id}",
            )

        async with self._running_lock:
            rt = self.running.get(thread_id)
        if rt is not None:
            try:
                await rt.signal_tx.put(ThreadSignal.InjectMessage(message))
            except asyncio.QueueFull:
                pass
        else:
            raise EngineError.ThreadNotFound(thread_id)

    async def set_thread_metadata(
        self,
        thread_id: ThreadId,
        key: str,
        value: str,
    ) -> None:
        """
        在持久化的线程记录上设置元数据键。

        注意：这会更新**存储**，而不是已运行的 `ExecutionLoop` 正在读取的内存中的
        `Thread`。需要下一个执行器步骤观察到新值的调用者必须在线程执行器任务生成
        （初始创建路径）之前或 `resume_thread`（从存储重新加载）之前应用此设置。
        """
        thread = await self.store.load_thread(thread_id)
        if thread is None:
            raise EngineError.ThreadNotFound(thread_id)
        thread.metadata[key] = value
        await self.store.save_thread(thread)

    async def is_running(self, thread_id: ThreadId) -> bool:
        """检查线程是否仍在运行。"""
        async with self._running_lock:
            rt = self.running.get(thread_id)
            if rt is None:
                return False
            return not rt.handle.done()

    async def join_thread(self, thread_id: ThreadId) -> ThreadOutcome:
        """
        等待线程完成并返回其结果。
        从运行集合中移除线程。
        """
        async with self._completed_lock:
            outcome = self._completed.pop(thread_id, None)
        if outcome is not None:
            return outcome

        async with self._running_lock:
            rt = self.running.pop(thread_id, None)

        if rt is None:
            raise EngineError.ThreadNotFound(thread_id)

        try:
            result = await rt.handle
        except Exception as e:
            logger.error("线程任务异常, thread_id=%s: %s", thread_id, e)
            result = ThreadOutcome.Failed(
                error=f"thread task panicked: {e}",
                debug_detail=None,
            )

        async with self._completed_lock:
            self._completed.pop(thread_id, None)
        return result

    async def children_of(self, thread_id: ThreadId) -> List[ThreadId]:
        """获取线程的子线程。"""
        async with self._tree_lock:
            return list(self.tree.get(thread_id, []))

    async def parent_of(self, thread_id: ThreadId) -> Optional[ThreadId]:
        """获取线程的父线程。"""
        async with self._tree_lock:
            return self._tree_parents.get(thread_id)

    async def cleanup_finished(self) -> List[ThreadId]:
        """从运行集合中清理已完成的线程。"""
        async with self._running_lock:
            finished = [
                tid for tid, rt in self.running.items()
                if rt.handle.done()
            ]
            for tid in finished:
                self.running.pop(tid, None)
        return finished

    async def resume_background_threads(self, project_id: ProjectId) -> List[ThreadId]:
        """自动恢复已检查点的非前台线程。"""
        # 系统操作：恢复所有挂起的研究线程，无论用户是谁
        threads = await self.store.list_all_threads(project_id)
        resumed: List[ThreadId] = []

        for thread in threads:
            if thread.state != ThreadState.Suspended:
                continue
            if thread.thread_type != ThreadType.Research:
                continue
            if "runtime_checkpoint" not in thread.metadata:
                continue
            if not thread.user_id:
                continue

            await self.resume_thread(thread.id, thread.user_id)
            resumed.append(thread.id)

        return resumed

    async def recover_project_threads(self, project_id: ProjectId) -> List[ThreadId]:
        """
        在进程启动后协调持久化的非终端线程。

        当前引擎不支持线程中途重放/恢复，因此任何处于非终端状态的线程
        都被标记为故障安全。在此处转换为 `Failed` 的线程携带
        [`ENGINE_RESTART_RECOVERY_METADATA_KEY`] 标志，以便调用者可以
        将它们与真实的、用户可操作的故障区分开来。
        """
        # 系统操作：恢复所有非终端线程，无论用户是谁
        threads = await self.store.list_all_threads(project_id)
        recovered: List[ThreadId] = []

        for thread in threads:
            if thread.state.is_terminal() or thread.state == ThreadState.Completed:
                continue

            if (
                thread.state == ThreadState.Waiting
                and PENDING_APPROVAL_METADATA_KEY in thread.metadata
            ):
                continue

            if (
                RUNTIME_CHECKPOINT_METADATA_KEY in thread.metadata
                and thread.state in (ThreadState.Running, ThreadState.Suspended)
            ):
                if thread.state == ThreadState.Running:
                    thread.transition_to(
                        ThreadState.Suspended,
                        "引擎重启；可从检查点恢复",
                    )
                await self.store.append_events(thread.events)
                await self.store.save_thread(thread)
                recovered.append(thread.id)
                continue

            # 在转换之前标记线程，以便下游消费者
            # （项目"需要关注"提要、健康汇总）可以跳过重启恢复噪音，
            # 仅显示真实故障。
            thread.metadata[ENGINE_RESTART_RECOVERY_METADATA_KEY] = True

            try:
                thread.transition_to(
                    ThreadState.Failed,
                    "线程完成前引擎重启",
                )
                await self.store.append_events(thread.events)
                await self.store.save_thread(thread)
                recovered.append(thread.id)
            except Exception:
                pass

        return recovered


def _is_resolved_call_message(message: ThreadMessage, call_id: str) -> bool:
    """检查消息是否与已解析的调用 ID 相关。"""
    if message.role == MessageRole.ActionResult and message.action_call_id == call_id:
        return True

    if message.role == MessageRole.Assistant and message.action_calls:
        return any(call.id == call_id for call in message.action_calls)

    return False


def _is_resolved_action_result_message(message: ThreadMessage, call_id: str) -> bool:
    """检查消息是否为已解析的操作结果消息。"""
    return (
        message.role == MessageRole.ActionResult
        and message.action_call_id == call_id
    )
