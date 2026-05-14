from enum import Enum, auto
from typing import TypedDict, ClassVar, Optional, Dict, List, Tuple
from dataclasses import dataclass, field
import logging
from utils.async_schems import RWLockDict
from agent.session import Session
from agent.undo import UndoManager
from hooks import HookRegistry

logger = logging.getLogger(__name__)

# 当会话数量超过此阈值时发出警告。
SESSION_COUNT_WARNING_THRESHOLD = 1000


@dataclass(frozen=True)
class ThreadKey:
    """
    用于将外部线程 ID 映射到内部线程 ID 的键。
    """
    user_id: str
    channel: str
    external_thread_id: Optional[str] = None

@dataclass
class SessionManager:
    """
    管理所有用户的会话、线程和撤销状态。
    """
    sessions: RWLockDict[str, Session] = field(default_factory=RWLockDict)
    thread_map: RWLockDict[ThreadKey, str] = field(default_factory=RWLockDict)
    undo_managers: RWLockDict[str, UndoManager] = field(default_factory=RWLockDict)
    hooks: Optional[HookRegistry] = None

    async def get_or_create_session(self, user_id: str) -> Session:
        """
        获取或创建session
        """
        async with self.sessions.read():
            if session := self.sessions.get(user_id, None):
                return session

        async with self.sessions.write():
            if session := self.sessions.get(user_id, None):
                return session

            new_session = Session(user_id=user_id)
            session_id = new_session.id
            self.sessions[user_id] = new_session
            if len(self.sessions) >= SESSION_COUNT_WARNING_THRESHOLD:
                logger.warning(f"会话数量过高：{len(self.sessions)} 个活跃会话。")

            # 触发 OnSessionStart 钩子（即发即忘模式）。
            if self.hooks:
                pass

            return new_session

    async def resolve_thread(self, user_id: str, channel: str, external_thread_id: Optional[str] = None)-> Tuple[Session, str]:
        """
        将外部线程 ID 解析为内部线程。

        返回会话 ID 和线程 ID。如果它们不存在，则创建。
        """
        session = await self.get_or_create_session(user_id)
        key = ThreadKey(user_id=user_id, channel=channel, external_thread_id=external_thread_id)

        async with self.thread_map.read():
            if thread_id := self.thread_map.get(key, None):
                if thread_id in session.threads:
                    return session, thread_id

        # 检查 external_thread_id 本身是否是一个已知的线程 UUID，该 UUID 存在于会话中但从未在 thread_map 中注册过
        # （例如，由 chat_new_thread_handler 创建或从数据库加载得到）。
        # 仅当没有 thread_map 条目映射到此 UUID 时，我们才会采用它——
        # 否则它属于不同的频道范围。
        if external_thread_id:
            pass

        # 创建一个新thread，对于新key永远创建一个新的thread
        thread = session.create_thread()
        thread_id = thread.id
        # 保存thread
        async with self.thread_map.write():
            self.thread_map[key] = thread_id

        async with self.undo_managers.write():
            self.undo_managers[thread_id] = UndoManager()

        return session, thread_id

    async def get_undo_manager(self, thread_id: str):
        """
        获取线程的撤销管理器。
        """
        async with self.undo_managers.read():
            if mgr := self.undo_managers.get(thread_id, None):
                return mgr

        async with self.undo_managers.write():
            if mgr := self.undo_managers.get(thread_id, None):
                return mgr

            mgr = UndoManager()
            self.undo_managers[thread_id] = mgr
            return mgr

