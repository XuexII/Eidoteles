# 对话管理器——将 UI 消息路由到线程。
#
# ConversationManager 是频道 I/O（用户消息、状态更新）与线程执行模型之间的桥梁。
# 它维护对话界面，并决定是生成新线程还是将消息注入现有线程。

from engine.runtime.manager import ThreadManager
from engine.runtime.messaging import ThreadOutcome
from engine.traits.store import Store
from engine.types.conversation import ConversationEntry, ConversationId, ConversationSurface
from engine.types.error import EngineError
from engine.types.message import ThreadMessage
from engine.types.project import ProjectId
from engine.types.thread import ThreadConfig, ThreadId, Thread, ThreadState, ThreadType

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple, Any
from enum import Enum
import logging
from engine.types.conversation import (
    EntrySender,
    EntrySenderUser,
    EntrySenderAgent,
    EntrySenderSystem
)

logger = logging.getLogger(__name__)


# ── 活跃前台线程状态 ─────────────────────────────────────────

class ActiveForegroundType(Enum):
    Running = "Running"
    Resumable = "Resumable"


@dataclass
class ActiveForeground:
    """活跃前台线程的标记"""
    state: ActiveForegroundType
    thread_id: ThreadId

    @classmethod
    def Running(cls, thread_id: ThreadId) -> "ActiveForeground":
        return cls(state=ActiveForegroundType.Running, thread_id=thread_id)

    @classmethod
    def Resumable(cls, thread_id: ThreadId) -> "ActiveForeground":
        return cls(state=ActiveForegroundType.Resumable, thread_id=thread_id)


# ── 对话管理器 ───────────────────────────────────────────────
@dataclass
class ConversationManager:
    """管理对话表面并将消息路由到线程

    每个频道消息到达此处。管理器决定是否：
    1. 为消息生成新的前台线程
    2. 将消息注入现有活跃线程
    3. 如果此频道+用户不存在对话则创建新对话

    ## 锁定策略

    `conversations` 是一个*目录*：全局锁仅用于 HashMap 查找/插入，
    永远不会跨 `.await` 持有。每个 `ConversationSurface` 包装在
    异步锁中，因此到*不同*对话的并发消息完全并行运行

    **锁排序不变量：** 永远不要同时持有全局锁和每个对话的锁。
    `get_conversation_lock()` 强制执行此规则 — 它在返回之前释放读守卫
    """
    thread_manager: ThreadManager
    store: Store
    # 锁顺序：当同时获取两个写锁时，始终先获取 `conversations`，再获取 `channel_user_index`。
    # 颠倒此顺序将在并发访问时导致死锁。
    conversations: Dict[ConversationId, ConversationSurface] = field(default_factory=dict, init=False)
    # 将（channel， user_id）映射到 conversation ID 以供查找。
    channel_user_index: Dict[Tuple[str, str], ConversationId] = field(default_factory=dict, init=False)

    async def get_conversation_lock(
            self, conversation_id: ConversationId
    ) -> ConversationSurface:
        """获取每个对话的锁。仅短暂持有全局锁（HashMap 查找），然后释放。
        如果对话不存在则返回错误
        """
        async with self._conv_lock:
            if conversation_id not in self.conversations:
                raise EngineError(f"Store: 未找到对话 {conversation_id}")
            return self.conversations[conversation_id]

    async def bootstrap_user(self, user_id: str) -> int:
        """将用户的持久化对话恢复到内存索引中"""
        conversations = await self.store.list_conversations(user_id)
        inserted = 0

        async with self._conv_lock:
            async with self._index_lock:
                for conv in conversations:
                    if conv.id in self.conversations:
                        # 仍然更新索引 — 如果之前的 get_or_create_conversation 插入了对话
                        # 但随后在失败的 save_conversation 上回滚了索引条目，索引可能缺失
                        key = (conv.channel, conv.user_id)
                        if key not in self.channel_user_index:
                            self.channel_user_index[key] = conv.id
                        continue

                    key = (conv.channel, conv.user_id)
                    self.channel_user_index[key] = conv.id
                    self.conversations[conv.id] = conv
                    inserted += 1

        return inserted

    async def get_or_create_conversation(
            self, channel: str, user_id: str
    ) -> ConversationId:
        """获取或创建频道+用户对的对话"""
        key = (channel, user_id)

        # 首先检查索引
        async with self._index_lock:
            if key in self.channel_user_index:
                return self.channel_user_index[key]

        # 检查此用户/频道的持久化对话
        conversations = await self.store.list_conversations(user_id)
        for conv in conversations:
            if conv.channel == channel:
                conv_id = conv.id
                async with self._conv_lock:
                    async with self._index_lock:
                        # 双重检查：另一个任务可能在我们进行 I/O 时已插入
                        if key in self.channel_user_index:
                            return self.channel_user_index[key]
                        self.conversations[conv_id] = conv
                        self.channel_user_index[key] = conv_id
                return conv_id

        # 创建新对话
        conv = ConversationSurface.new(channel, user_id)
        conv_id = conv.id

        async with self._conv_lock:
            async with self._index_lock:
                # 双重检查：另一个任务可能在我们进行 I/O 时已插入
                if key in self.channel_user_index:
                    return self.channel_user_index[key]
                self.conversations[conv_id] = conv.clone()
                self.channel_user_index[key] = conv_id

        # 在异步保存之前释放写锁
        try:
            await self.store.save_conversation(conv)
        except Exception as e:
            # 已知限制：通过双重检查快速路径观察到新 conv_id 的并发调用者
            # （在我们的插入和此回滚之间）将持有一个现已删除、从未持久化的 ConversationId。
            # 此竞态需要来自相同用户+频道的同时首次登录 AND 存储写入失败 —
            # 在实践中不太可能，被接受为乐观内存缓存与异步持久化的结构权衡。
            # 替代方案（在异步保存期间持有写锁）将重新引入跨租户序列化。
            # 回滚内存插入，以便下一个调用者不会收到未持久化的 ConversationId
            async with self._conv_lock:
                self.conversations.pop(conv_id, None)
            async with self._index_lock:
                self.channel_user_index.pop(key, None)
            raise EngineError(f"Store: {e}")

        logger.debug(f"已创建对话: conversation_id={conv_id}, channel={channel}, user_id={user_id}")
        return conv_id

    async def handle_user_message(
            self,
            conversation_id: ConversationId,
            content: str,
            project_id: ProjectId,
            user_id: str,
            thread_config: ThreadConfig,
            user_timezone: Optional[str] = None,
            extra_initial_metadata: Optional[Dict[str, Any]] = None,
    ) -> ThreadId:
        """处理传入的用户消息

        如果对话有活跃的前台线程，消息被注入其中。
        否则，生成新的前台线程

        返回处理消息的线程 ID

        每个对话的锁在整个操作期间持有 — 从活跃线程检查到 `save_conversation`。
        这消除了旧 5 阶段拆分中存在的 TOCTOU 双重生成窗口

        **`extra_initial_metadata` 仅在生成时生效。** 仅当此调用分配新线程时
        （下面的 `None` 活跃前台分支），它才合并到线程的 `metadata` 映射中。
        在 `Running`（注入）和 `Resumable`（恢复）路径上，调用者提供的元数据被*忽略* —
        这些线程已存在且有自己的元数据
        """
        # 获取conversation: ConversationSurface
        conv = await self.get_conversation_lock(conversation_id)

        # 租户隔离：验证请求用户拥有此对话
        if conv.user_id != user_id:
            raise RuntimeError(f"AccessDenied: 用户 '{user_id}' 不能访问对话 {conversation_id}")

        # 快照 find_active_foreground 在异步调用之前需要的内容
        active_thread_ids = list(conv.active_threads)
        channel_name = conv.channel

        # 异步 I/O 查找活跃前台线程
        active_foreground = await self.find_active_foreground(active_thread_ids)

        if active_foreground and active_foreground.state == ActiveForegroundType.Running:
            # 将消息注入活跃线程
            thread_id = active_foreground.thread_id
            logger.debug(
                f"将消息注入活跃线程: conversation_id={conversation_id}, thread_id={thread_id}"
            )
            await self.thread_manager.inject_message(
                thread_id, user_id, ThreadMessage.user(content),
            )
        elif active_foreground and active_foreground.state == ActiveForegroundType.Resumable:
            # 恢复暂停的前台线程
            thread_id = active_foreground.thread_id
            logger.debug(
                f"恢复暂停的前台线程: conversation_id={conversation_id}, thread_id={thread_id}"
            )
            # 恢复从存储重新加载线程，因此在 resume_thread 之前将新的 user_timezone
            # 写入持久化记录意味着恢复的执行看到最新的值
            if user_timezone is not None:
                try:
                    await self.thread_manager.set_thread_metadata(
                        thread_id, "user_timezone", user_timezone,
                    )
                except Exception as e:
                    logger.debug(
                        f"恢复时刷新 user_timezone 失败；线程将使用先前的值: "
                        f"thread_id={thread_id}, error={e}"
                    )
            await self.thread_manager.resume_thread(
                thread_id, user_id, ThreadMessage.user(content), None, None,
            )
        else:
            # 生成新的前台线程
            # 从先前的条目构建对话历史以保持上下文连续性
            history = build_history_from_entries(conv.entries)

            # 构建初始线程元数据。必须在执行器后台任务启动*之前*应用 —
            # `set_thread_metadata` 仅更新持久化记录，不更新循环正在读取的内存中 Thread，
            # 因此第一步否则会错过 `user_timezone` / `source_channel`
            base_channel = channel_name.split(':')[0] if ':' in channel_name else channel_name
            initial_metadata = {
                "source_channel": base_channel,
            }
            if user_timezone is not None:
                initial_metadata["user_timezone"] = user_timezone
            # 存储发起对话的 conversation_id，以便 `thread_execution_context`
            # 可以通过 `ThreadExecutionContext.conversation_id` 将其显示给主机
            initial_metadata["conversation_id"] = str(conversation_id)
            # 合并调用者提供的元数据（例如来自 responses_api 桥接的 `conversation_scope`，
            # 以便 EffectExecutor 可以解析每个对话的状态）
            if extra_initial_metadata:
                for k, v in extra_initial_metadata.items():
                    if k not in initial_metadata:
                        initial_metadata[k] = v

            # 使用对话历史生成新前台线程
            title = Thread.derive_title_from_message(content)
            thread_id = await self.thread_manager.spawn_thread_with_history(
                content,  # 使用消息作为目标
                title,
                ThreadType.Foreground,
                project_id,
                thread_config,
                None,
                user_id,
                history,
                initial_metadata,
            )

        # 在线程操作成功后添加用户条目 — 防止如果上面的操作返回错误时出现孤儿条目
        conv.add_entry(ConversationEntry.user(content))

        if active_foreground is not None and active_foreground.state == ActiveForegroundType.Running:
            # 除了上面的用户条目外，不需要额外的内存变更
            pass
        elif active_foreground is not None and active_foreground.state == ActiveForegroundType.Resumable:
            conv.add_entry(ConversationEntry.system_for_thread(thread_id, "线程已恢复"))
        else:
            conv.track_thread(thread_id)
            conv.add_entry(ConversationEntry.system_for_thread(thread_id, "线程已启动"))
            logger.debug(
                f"生成了新的前台线程: conversation_id={conversation_id}, thread_id={thread_id}"
            )

        # 在全局锁之外持久化
        await self.store.save_conversation(conv)

        return thread_id

    async def record_thread_outcome(
            self,
            conversation_id: ConversationId,
            thread_id: ThreadId,
            outcome: ThreadOutcome,
    ) -> None:
        """在线程的对话中记录线程结果"""
        conv = await self.get_conversation_lock(conversation_id)

        if isinstance(outcome, ThreadOutcome) and hasattr(outcome, 'response'):
            # Completed
            if outcome.response is not None:
                conv.add_entry(ConversationEntry.agent(thread_id, outcome.response))
            conv.untrack_thread(thread_id)
        elif hasattr(outcome, 'type'):
            if outcome.type == "Stopped":
                conv.add_entry(ConversationEntry.system_for_thread(thread_id, "线程已停止"))
                conv.untrack_thread(thread_id)
            elif outcome.type == "MaxIterations":
                conv.add_entry(ConversationEntry.system_for_thread(thread_id, "线程达到最大迭代次数"))
                conv.untrack_thread(thread_id)
            elif outcome.type == "Failed":
                error = getattr(outcome, 'error', '未知错误')
                conv.add_entry(ConversationEntry.system_for_thread(thread_id, f"线程失败: {error}"))
                conv.untrack_thread(thread_id)
            elif outcome.type == "GatePaused":
                gate_name = getattr(outcome, 'gate_name', 'unknown')
                action_name = getattr(outcome, 'action_name', 'unknown')
                conv.add_entry(ConversationEntry.system_for_thread(
                    thread_id, f"门控 '{gate_name}' 暂停了动作执行: {action_name}",
                ))
                # 线程保持活跃 — 等待门控解决方案

        # 已知限制：如果 save_conversation 失败，内存变更（add_entry、untrack_thread）
        # 已应用但未持久化。内存和数据库在下次成功保存之前会分歧
        await self.store.save_conversation(conv)

    async def record_external_agent_message(
            self,
            conversation_id: ConversationId,
            thread_id: ThreadId,
            user_id: str,
            content: str,
    ) -> None:
        """将代理消息追加到源自对话自身线程树*之外*的对话中
        （例如任务的通知线程）

        租户隔离：拒绝 `user_id` 不拥有对话的调用，镜像 `handle_user_message`
        """
        conv = await self.get_conversation_lock(conversation_id)
        if conv.user_id != user_id:
            raise EngineError(f"AccessDenied: 用户 '{user_id}' 不能访问对话 {conversation_id}")
        conv.add_entry(ConversationEntry.agent(thread_id, content))
        await self.store.save_conversation(conv)

    async def clear_conversation(
            self, conversation_id: ConversationId, user_id: str
    ) -> None:
        """清除对话的条目和活跃线程

        停止跟踪所有线程并移除对话历史，
        以便下一条用户消息生成没有先前上下文的新线程
        """
        conv = await self.get_conversation_lock(conversation_id)
        # 租户隔离：验证所有权
        if conv.user_id != user_id:
            raise EngineError(f"AccessDenied: 用户 '{user_id}' 不能访问对话 {conversation_id}")
        conv.active_threads.clear()
        conv.entries.clear()
        conv.updated_at = datetime.now(timezone.utc)
        await self.store.save_conversation(conv)
        logger.debug(f"已清除对话: conversation_id={conversation_id}")

    async def get_conversation(self, conversation_id: ConversationId) -> Optional[ConversationSurface]:
        """获取对话的快照"""
        async with self._conv_lock:
            if conversation_id not in self.conversations:
                return None
            return self.conversations[conversation_id].clone()

    async def list_conversations(self, user_id: str) -> List[ConversationSurface]:
        """返回给定用户的对话

        使用 `channel_user_index` 在获取任何每个对话的锁之前按用户预过滤，
        保持锁范围最小。这是最佳努力的快照：每个对话单独锁定和读取，
        因此锁之间的并发变更可能部分可见
        """
        convs_to_read = []
        async with self._conv_lock:
            async with self._index_lock:
                for (channel, uid), conv_id in self.channel_user_index.items():
                    if uid == user_id and conv_id in self.conversations:
                        convs_to_read.append(self.conversations[conv_id])

        result = []
        for conv in convs_to_read:
            result.append(conv.clone())
        return result

    async def find_active_foreground(
            self, active_thread_ids: List[ThreadId]
    ) -> Optional[ActiveForeground]:
        """给定活跃线程 ID 的快照，查找活跃前台线程

        接受普通列表而不是 `ConversationSurface` 引用，以便调用者
        可以在调用此方法之前释放对话写锁 — 它执行异步 I/O
        （is_running、load_thread），不能在任何锁下持有
        """
        for tid in active_thread_ids:
            if await self.thread_manager.is_running(tid):
                return ActiveForeground.Running(tid)
            try:
                thread = await self.store.load_thread(tid)
                if (thread is not None
                        and thread.thread_type == ThreadType.Foreground
                        and thread.state == ThreadState.Suspended):
                    return ActiveForeground.Resumable(tid)
            except Exception:
                continue
        return None


# ── 从条目构建历史 ──────────────────────────────────────────

def build_history_from_entries(
        entries: List[ConversationEntry],
) -> List[ThreadMessage]:
    """从对话条目构建 ThreadMessage 历史

    将用户和代理条目转换为 ThreadMessages，以便新线程
    继承同一对话中先前轮次的上下文

    调用者传递在当前用户消息追加*之前*拍摄的快照，
    因此此处的所有条目都是先前轮次的历史 — 全部包含它们。
    系统条目（线程生命周期通知）被跳过，因为它们不是有用的 LLM 上下文
    """
    history = []
    for entry in entries:
        match entry.sender:
            case EntrySenderUser():
                history.append(ThreadMessage.user(entry.content))
            case EntrySenderAgent():
                history.append(ThreadMessage.assistant(entry.content))
            case EntrySenderSystem():
                # 跳过系统通知。
                pass
    return history
