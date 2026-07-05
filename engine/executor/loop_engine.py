# 核心执行循环——`run_agentic_loop()` 的替代方案。
#
# `ExecutionLoop` 拥有一个线程，并驱动其经历大语言模型调用→动作执行→结果处理→重复循环。
# 与现有的委托模式不同，该循环是自包含的：线程类型之间的所有行为差异通过能力租约和策略处理，而非委托实现。

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Any, Dict

from engine.capability.lease import LeaseManager
from engine.capability.policy import PolicyEngine
from engine.capability.registry import CapabilityRegistry
from engine.runtime.messaging import SignalReceiver, ThreadOutcome
from engine.traits.effect import EffectExecutor
from engine.traits.llm import LlmBackend
from engine.types.error import EngineError
from engine.types.event import EventKind
from engine.types.step import Step, StepId
from engine.types.thread import Thread, ThreadState
from engine.types.memory import MemoryDoc
from engine.gate import GateController
from engine.traits.store import Store
from engine.memory import RetrievalEngine
from engine.executor.prompt import PlatformInfo

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────

RUNTIME_CHECKPOINT_METADATA_KEY = "runtime_checkpoint"


# ── 运行时检查点 ─────────────────────────────────────────────

@dataclass
class RuntimeCheckpoint:
    """先前执行的持久化状态，用于恢复线程。
    Python 编排器在内部管理循环计数器；Rust 只需要
    不透明的 `persisted_state` 块以便在恢复时传回
    """
    persisted_state: Dict[str, Any] = field(default_factory=dict)

    def has_working_messages_system_prompt(self) -> bool:
        """检查工作消息中是否包含引擎拥有的系统提示"""
        messages = self.persisted_state.get("working_messages")
        if not isinstance(messages, list):
            return False

        for message in messages:
            role = message.get("role") if isinstance(message, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if role in ("System", "system") and content and is_codeact_system_prompt(content):
                return True
        return False

    def update_working_messages_system_prompt(self, system_prompt: str) -> bool:
        """更新工作消息中的系统提示。返回是否发生了更改"""
        messages = self.persisted_state.get("working_messages")
        if not isinstance(messages, list):
            return False

        # 查找现有的引擎拥有的系统消息
        for i, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if role in ("System", "system") and content and is_codeact_system_prompt(content):
                refreshed = refresh_codeact_system_prompt(content, system_prompt)
                if content == refreshed:
                    return False
                messages[i] = {"role": "System", "content": refreshed}
                return True

        # 检查是否已存在其他系统消息
        for message in messages:
            if isinstance(message, dict) and message.get("role") in ("System", "system"):
                return False

        # 前置系统提示
        messages.insert(0, {"role": "System", "content": system_prompt})
        return True


# ── 执行循环 ─────────────────────────────────────────────────
from bridge.effect_adapter import EffectBridgeAdapter
@dataclass
class ExecutionLoop:
    """线程的核心执行循环"""
    thread: Thread
    llm: LlmBackend
    effects: EffectBridgeAdapter  # EffectExecutor
    leases: LeaseManager
    policy: PolicyEngine
    signal_rx: SignalReceiver
    # 存储以备将来使用（例如用户范围的提示覆盖）
    user_id: str
    gate_controller: GateController
    # 可选的能力注册表，用于解析能力级别的策略
    capabilities: Optional[CapabilityRegistry] = None
    # 可选的事件广播发送器，用于实时事件流
    event_tx: Optional[asyncio.Queue] = None
    # 可选的检索引擎，用于向上下文中注入先前知识
    retrieval: Optional[RetrievalEngine] = None
    # 可选的 Store，用于运行时提示覆盖加载和技能检索
    store: Optional[Store] = None
    # 运行时平台元数据，用于系统提示中的自我感知
    platform_info: Optional[PlatformInfo] = None

    def with_event_tx(self, tx: asyncio.Queue) -> "ExecutionLoop":
        """设置事件广播发送器用于实时状态更新"""
        self.event_tx = tx
        return self

    def with_capabilities(self, capabilities: CapabilityRegistry) -> "ExecutionLoop":
        """设置能力注册表用于解析能力级别的策略"""
        self.capabilities = capabilities
        return self

    def with_retrieval(self, retrieval: RetrievalEngine) -> "ExecutionLoop":
        """设置检索引擎用于向上下文中注入先前知识"""
        self.retrieval = retrieval
        return self

    def with_store(self, store: Store) -> "ExecutionLoop":
        """设置 Store 用于运行时提示覆盖加载和技能检索"""
        self.store = store
        return self

    def with_platform_info(self, info: PlatformInfo) -> "ExecutionLoop":
        """设置平台元数据用于系统提示中的自我感知"""
        self.platform_info = info
        return self

    def _emit_event(self, kind: EventKind) -> None:
        """向线程添加事件并广播用于实时状态更新"""
        event = ThreadEvent.new(self.thread.id, kind)
        if self.event_tx is not None:
            # 使用 put_nowait 进行非阻塞发送（与 broadcast::send 的尽力而为语义匹配）
            try:
                self.event_tx.put_nowait(event)
            except asyncio.QueueFull:
                pass
        self.thread.events.append(event)
        self.thread.updated_at = datetime.now(timezone.utc)

    def load_runtime_checkpoint(self) -> RuntimeCheckpoint:
        """从线程元数据中加载运行时检查点"""
        persisted_state = self.thread.metadata.get(
            RUNTIME_CHECKPOINT_METADATA_KEY, {}
        ).get("persisted_state", {})

        if not isinstance(persisted_state, dict):
            persisted_state = {}

        return RuntimeCheckpoint(persisted_state=persisted_state)

    def _clear_runtime_checkpoint(self) -> None:
        """清除线程元数据中的运行时检查点"""
        if isinstance(self.thread.metadata, dict):
            self.thread.metadata.pop(RUNTIME_CHECKPOINT_METADATA_KEY, None)
        self.thread.updated_at = datetime.now(timezone.utc)

    def _store_runtime_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        """将运行时检查点存储到线程元数据中"""
        if isinstance(self.thread.metadata, dict):
            self.thread.metadata[RUNTIME_CHECKPOINT_METADATA_KEY] = {
                "persisted_state": checkpoint.persisted_state.copy(),
            }
        self.thread.updated_at = datetime.now(timezone.utc)

    def _has_engine_owned_system_prompt(self, checkpoint: RuntimeCheckpoint) -> bool:
        """检查线程或检查点中是否存在引擎拥有的系统提示"""

        def messages_have_prompt(messages: List[ThreadMessage]) -> bool:
            for message in messages:
                if (message.role == MessageRole.System
                        and is_codeact_system_prompt(message.content)):
                    return True
            return False

        return (messages_have_prompt(self.thread.messages)
                or messages_have_prompt(self.thread.internal_messages)
                or checkpoint.has_working_messages_system_prompt())

    async def refresh_system_prompt(
            self,
            system_docs: List[MemoryDoc],
            system_docs_loaded: bool,
            checkpoint: RuntimeCheckpoint,
    ):
        """
        在线程启动或恢复时刷新 LLM 系统提示，确保工具列表和能力定义是最新的。
        它获取活跃租约、可用能力和动作，构建新的系统提示，然后更新线程消息和运行时检查点
        :param system_docs:
        :param system_docs_loaded:
        :param checkpoint:
        :return:
        """
        # 获取活跃租约
        # 查询该线程有哪些能力租约（如 "tools"、"slack" 等
        from engine.executor.thread_context import thread_execution_context
        active_leases = await self.leases.active_for_thread(self.thread.id)
        # 构建执行上下文
        # 为后续的能力和动作查询提供执行上下文
        prompt_context = thread_execution_context(
            self.thread,
            StepId(),
            None,
            self.gate_controller,
        )

        # 获取可用能力
        # 根据租约查询线程可以使用哪些能力
        capabilities_result = await self.effects.available_capabilities(active_leases, prompt_context)
        capabilities_loaded = True
        if isinstance(capabilities_result, Exception):
            capabilities = []
            capabilities_loaded = False
            logger.debug(f"线程 {self.thread.id}: 加载系统提示刷新所需能力失败: {capabilities_result}")
        else:
            capabilities = capabilities_result

        # 加载动作
        # 根据租约查询线程可以调用哪些具体工具动作
        actions_result = await self.effects.available_actions(active_leases, prompt_context)
        actions_loaded = True
        if isinstance(actions_result, Exception):
            compact_actions = []
            actions_loaded = False
            logger.debug(f"线程 {self.thread.id}: 加载系统提示刷新所需动作失败: {actions_result}")
        else:
            compact_actions = actions_result

        # 如果提示输入不完整且存在引擎拥有的系统提示，则跳过刷新
        if (not system_docs_loaded or not capabilities_loaded or not actions_loaded) \
                and self._has_engine_owned_system_prompt(checkpoint):
            logger.debug(
                f"线程 {self.thread.id}: 由于提示输入不完整，跳过系统提示刷新 "
                f"(system_docs_loaded={system_docs_loaded}, "
                f"capabilities_loaded={capabilities_loaded}, "
                f"actions_loaded={actions_loaded})"
            )
            return

        # 构建系统提示
        from engine.executor.prompt import build_codeact_system_prompt_with_docs
        system_prompt = build_codeact_system_prompt_with_docs(
            capabilities,
            compact_actions,
            system_docs,
            self.platform_info,
        )

        from engine.executor.prompt import upsert_codeact_system_prompt
        # 更新各种位置中的系统提示
        messages_updated = upsert_codeact_system_prompt(
            self.thread.messages,
            system_prompt,
        )

        internal_updated = False
        if self.thread.internal_messages:
            internal_updated = upsert_codeact_system_prompt(
                self.thread.internal_messages,
                system_prompt,
            )
        # 更新运行时检查点
        checkpoint_updated = checkpoint.update_working_messages_system_prompt(system_prompt)

        # 持久化更新
        if checkpoint_updated:
            self._store_runtime_checkpoint(checkpoint)
        elif messages_updated or internal_updated:
            self.thread.updated_at = datetime.now(timezone.utc)

    async def _persist_runtime_state(
            self,
            step: Optional[Step],
            persisted_event_count: int,
    ) -> int:
        """持久化运行时状态。返回更新后的事件计数"""
        if self.store is None:
            return persisted_event_count

        # 所有三个存储写入是独立的 — 并行运行它们
        async def save_step():
            if step is not None:
                await self.store.save_step(step)

        new_event_count = len(self.thread.events)

        async def save_events():
            if persisted_event_count < new_event_count:
                await self.store.append_events(
                    self.thread.events[persisted_event_count:]
                )

        async def save_thread():
            await self.store.save_thread(self.thread)

        # 并行执行
        await asyncio.gather(
            save_step(),
            save_events(),
            save_thread(),
        )

        return new_event_count

    async def run(self) -> ThreadOutcome:
        """运行执行循环直到完成"""
        persisted_event_count = len(self.thread.events)
        checkpoint = self.load_runtime_checkpoint()

        # 如果这是全新启动或从可恢复状态重新启动，则转换为 Running
        if self.thread.state != ThreadState.Running:
            self.thread.transition_to(ThreadState.Running, None)

        # 预获取共享记忆文档一次 — 由提示覆盖和编排器加载共同使用，
        # 避免重复的 Store 查询
        system_docs = []
        system_docs_loaded = True
        if self.store is not None:
            try:
                system_docs = await self.store.list_shared_memory_docs(self.thread.project_id)
            except Exception as e:
                logger.debug(f"加载编排器共享文档失败: {e}")
                system_docs_loaded = False

        await self.refresh_system_prompt(system_docs, system_docs_loaded, checkpoint)
        persisted_event_count = await self._persist_runtime_state(None, persisted_event_count)

        # 使用预获取的文档加载带版本的 Python 编排器。
        # 默认禁用自我修改 — 仅编译时的 v0 运行，除非通过
        # ORCHESTRATOR_SELF_MODIFY=true 显式选择加入。
        # 该标志从进程级快照读取（在首次调用时设置一次），
        # 因此运行时环境变更无法在任务中途切换门控
        from engine.runtime.internal_write import self_modify_enabled
        allow_self_modify = self_modify_enabled()
        from engine.executor.orchestrator import load_orchestrator_from_docs
        orchestrator_code, orchestrator_version = load_orchestrator_from_docs(
            system_docs,
            allow_self_modify,
        )

        logger.debug(
            f"线程 {self.thread.id}: 运行 Python 编排器 (版本 {orchestrator_version})"
        )

        # 将版本存储在线程元数据中以用于回滚跟踪
        if isinstance(self.thread.metadata, dict):
            self.thread.metadata["orchestrator_version"] = orchestrator_version

        # 通过主机函数调度执行 Python 编排器
        try:
            from engine.executor.orchestrator import execute_orchestrator
            orch_result = await execute_orchestrator(
                orchestrator_code,
                self.thread,
                self.llm,
                self.effects,
                self.leases,
                self.policy,
                self.signal_rx,
                self.event_tx,
                self.retrieval,
                self.store,
                self.platform_info,
                self.gate_controller,
                checkpoint.persisted_state,
            )

            # 成功后重置失败计数器
            if self.store is not None:
                await reset_orchestrator_failures(
                    self.store,
                    self.thread.project_id,
                )

            self._clear_runtime_checkpoint()
            persisted_event_count = await self._persist_runtime_state(None, persisted_event_count)
            return orch_result.outcome

        except Exception as e:
            logger.debug(
                f"线程 {self.thread.id}: 编排器执行失败 (版本 {orchestrator_version}): {e}"
            )

            # 记录失败以用于自动回滚跟踪
            if self.store is not None:
                await record_orchestrator_failure(
                    self.store,
                    self.thread.project_id,
                    orchestrator_version,
                )

                # 如果此版本下次将被跳过，则发出回滚事件
                # （失败计数刚刚增加，因此检查 >= threshold - 1）
                if orchestrator_version > 0:
                    self._emit_event(EventKind.OrchestratorRollback(
                        from_version=orchestrator_version,
                        to_version=orchestrator_version - 1,
                        reason=f"执行失败: {e}",
                    ))

            # 如果尚未处于终止状态，则转换为 Failed
            if self.thread.state not in (
                    ThreadState.Completed,
                    ThreadState.Failed,
                    ThreadState.Done,
            ):
                self.thread.transition_to(
                    ThreadState.Failed,
                    f"编排器错误: {e}",
                )

            self._clear_runtime_checkpoint()
            persisted_event_count = await self._persist_runtime_state(None, persisted_event_count)

            return ThreadOutcome.Failed(
                error=f"编排器错误: {e}",
                debug_detail=str(e),
            )
