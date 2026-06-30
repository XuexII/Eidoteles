# 对话界面——UI 层，与执行层分离。
#
# 对话是用户可见的条目流。线程（执行单元）独立运行，并产生出现在对话中的条目。
# 一个对话可以有多个活动线程；一个线程可以比其原始对话存活更久。

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from engine.types.thread import ThreadId


@dataclass(frozen=True)
class ConversationId:
    """强类型对话标识符。"""
    value: uuid.UUID = field(default_factory=uuid.uuid4)

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True)
class EntryId:
    """强类型条目标识符。"""
    value: uuid.UUID = field(default_factory=uuid.uuid4)

    def __str__(self) -> str:
        return str(self.value)


class EntrySender:
    """对话条目的发送者。"""
    pass


@dataclass
class EntrySenderUser(EntrySender):
    """人类用户。"""
    pass


@dataclass
class EntrySenderAgent(EntrySender):
    """代理（来自特定线程）。"""
    thread_id: ThreadId


@dataclass
class EntrySenderSystem(EntrySender):
    """系统通知（线程启动、完成等）。"""
    pass


@dataclass
class ConversationEntry:
    """对话中的单个条目——用户可见的消息。"""
    id: EntryId = field(default_factory=EntryId)
    sender: EntrySender = field(default_factory=EntrySenderUser)
    content: str = ""
    # 哪个线程产生了此条目（如果有）
    origin_thread_id: Optional[ThreadId] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # 可选元数据（通道特定格式、附件等）
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def user(cls, content: str) -> "ConversationEntry":
        """创建用户条目。"""
        return cls(
            id=EntryId(),
            sender=EntrySenderUser(),
            content=content,
            origin_thread_id=None,
            timestamp=datetime.now(timezone.utc),
            metadata={},
        )

    @classmethod
    def agent(cls, thread_id: ThreadId, content: str) -> "ConversationEntry":
        """从线程创建代理条目。"""
        return cls(
            id=EntryId(),
            sender=EntrySenderAgent(thread_id=thread_id),
            content=content,
            origin_thread_id=thread_id,
            timestamp=datetime.now(timezone.utc),
            metadata={},
        )

    @classmethod
    def system(cls, content: str) -> "ConversationEntry":
        """创建系统通知条目。"""
        return cls(
            id=EntryId(),
            sender=EntrySenderSystem(),
            content=content,
            origin_thread_id=None,
            timestamp=datetime.now(timezone.utc),
            metadata={},
        )

    @classmethod
    def system_for_thread(cls, thread_id: ThreadId, content: str) -> "ConversationEntry":
        """创建链接到线程的系统通知。"""
        return cls(
            id=EntryId(),
            sender=EntrySenderSystem(),
            content=content,
            origin_thread_id=thread_id,
            timestamp=datetime.now(timezone.utc),
            metadata={},
        )


@dataclass
class ConversationSurface:
    """
    对话表面——聊天记录的 UI 面向视图。

    对话不是执行边界。它们是可能来自多个并发线程的条目流。
    用户可以在另一个线程仍在运行时启动新线程，两者都在同一对话中产生条目。
    """
    id: ConversationId = field(default_factory=ConversationId)
    # 此对话所在的通道（例如 "telegram"、"web"、"cli"）
    channel: str = ""
    # 拥有此对话的用户
    user_id: str = ""
    # 按时间顺序排列的所有条目
    entries: List[ConversationEntry] = field(default_factory=list)
    # 当前活动（非终端）线程 ID
    active_threads: List[ThreadId] = field(default_factory=list)
    # 元数据（通道特定状态、外部线程 ID 等）
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def new(cls, channel: str, user_id: str) -> "ConversationSurface":
        """创建新的对话表面。"""
        now = datetime.now(timezone.utc)
        return cls(
            id=ConversationId(),
            channel=channel,
            user_id=user_id,
            entries=[],
            active_threads=[],
            metadata={},
            created_at=now,
            updated_at=now,
        )

    def add_entry(self, entry: ConversationEntry) -> None:
        """添加条目并更新时间戳。"""
        self.entries.append(entry)
        self.updated_at = datetime.now(timezone.utc)

    def track_thread(self, thread_id: ThreadId) -> None:
        """在线程中注册为活动状态。"""
        if thread_id not in self.active_threads:
            self.active_threads.append(thread_id)

    def untrack_thread(self, thread_id: ThreadId) -> None:
        """从活动列表中移除线程（它已完成或失败）。"""
        self.active_threads = [tid for tid in self.active_threads if tid != thread_id]

    def last_entry(self) -> Optional[ConversationEntry]:
        """获取最近的条目（如果有）。"""
        return self.entries[-1] if self.entries else None

    def entries_for_thread(self, thread_id: ThreadId) -> List[ConversationEntry]:
        """获取来自特定线程的所有条目。"""
        return [e for e in self.entries if e.origin_thread_id == thread_id]
