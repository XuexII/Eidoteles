from dataclasses import dataclass, field
from typing import Dict, Optional, Union
from agent.uodo import UndoManager
from agent.session import Session
from hooks import HookRegistry
import logging
import asyncio
logger = logging.getLogger(__name__)


# async def _run_session_start_hook(hooks, user_id, session_id):
#     event = HookEvent.SessionStart(user_id, session_id)
#     try:
#         await hooks.run(event)
#     except Exception as e:
#         logger.warning(f"OnSessionStart 钩子执行错误: {e}")


# 用于将外部线程 ID 映射到内部线程 ID 的键。
@dataclass
class ThreadKey:
    user_id: str
    channel: str
    external_thread_id: Optional[str] = None

@dataclass
class SessionManager:
    """
    管理所有用户的sessions，threads以及undo state
    """
    sessions: Dict[str, Session] = field(default_factory=dict)
    thread_map: Dict[ThreadKey, str] = field(default_factory=dict)
    undo_managers: Dict[str, UndoManager] = field(default_factory=dict)
    hooks: Optional[HookRegistry] = None

    async def get_or_create_session(self, user_id: str) -> Session:
        # 检查session是否存在
        if session := self.sessions.get(user_id):
            return session
        # 创建新的session
        session = Session(user_id=user_id)
        self.sessions[user_id] = session

        # 触发 OnSessionStart 钩子（即发即忘模式）
        # if self.hooks:
        #     asyncio.create_task(_run_session_start_hook(self.hooks, user_id, session.session_id))
        return session



    async def resolve_thread(self, user_id: str, channel: str, external_thread_id: Optional[str]=None) -> Union[Session, str]:
        """
        将外部线程 ID 解析为内部线程。
        返回会话 ID 和线程 ID。如果它们不存在，则创建。

        """

        session = await self.get_or_create_session(user_id)
        key = ThreadKey(user_id=user_id, channel=channel, external_thread_id=external_thread_id)
        # 检查key是否存在
        if thread_id := self.thread_map.get(key):
            # 检查线程依然存在于session
            if thread_id in session.threads:
                return session, thread_id

        # 检查 external_thread_id 本身是否是一个已知的线程 UUID，该 UUID 存在于会话中但从未在 thread_map 中注册过
        # 例如，由 chat_new_thread_handler 创建或从数据库加载得到）。
        # 仅当没有 thread_map 条目映射到此 UUID 时，我们才会采用它——
        # 否则它属于不同的频道范围。


        # 创建新的thread
        thread = session.create_thread()
        thread_id = thread.id
        self.thread_map[key] = thread_id

        # 为线程创建undomanger
        self.undo_managers[thread_id] = UndoManager()

        return session, thread_id