# 会话管理器，用于处理多用户、多线程的对话。
# 将外部通道的线程 ID 映射到内部 UUID，并为每个线程管理撤销状态。

import asyncio
from typing import Dict, Optional
from dataclasses import dataclass
from uuid import UUID, uuid4
from agent.session import Session
from agent.undo import UndoManager
from hooks import HookRegistry, HookEvent
from typing import Any, Tuple
import logging

# 当会话数量超过此阈值时发出警告
SESSION_COUNT_WARNING_THRESHOLD = 1000

@dataclass(frozen=True)  # frozen=True 使实例不可变，对应 Rust 的 #[derive(Clone, Hash, Eq, PartialEq)]
class ThreadKey:
    """
    用于将外部线程 ID 映射到内部 ID 的键。
    包含用户 ID、通道名称和可选的通道外部线程 ID。
    """
    user_id: str
    channel: str
    external_thread_id: Optional[str]


class SessionManager:
    """
    管理所有会话、线程和撤销状态。
    对应 Rust 中的 SessionManager 结构体，使用锁保护内部映射表。
    """

    def __init__(
            self,
            sessions: Dict[UUID, Session] = {},
            thread_map: Dict[Any, UUID] = {},
            undo_managers: Dict[UUID, UndoManager] = {},
            hooks: Optional[HookRegistry] = None,
    ):
        self.sessions: Dict[str, Session] = sessions  # 普通字典，需手动加锁

        self.thread_map: Dict[Any, UUID] = thread_map

        self.undo_managers: Dict[UUID, UndoManager] = undo_managers

        self.hooks: Optional[HookRegistry] = hooks

    def with_hooks(self, hooks: HookRegistry) -> 'SessionManager':
        """
        添加会话生命周期事件的钩子注册表。
        """
        self.hooks = hooks
        return self

    async def _fire_session_start_hook(self, hooks, user_id: str, session_id: str) -> None:
        try:
            event = HookEvent.SessionStart(
                user_id,
                session_id
            )

            await hooks.run(event)
        except Exception as e:
            logging.warning(f"OnSessionStart hook error: {e}")

    async def get_or_create_session(self, user_id: str) -> Session:
        """
        获取或者生成session
        :param user_id:
        :return:
        """

        # 快速路径：检查会话是否存在
        sessions = await self.sessions.read()
        if sessions.get(user_id):
            return sessions.get(user_id)

        # 慢路径：创建新会话，需要写锁
        sessions = await self.sessions.write()
        # 获取锁之后再次检查是否存在
        if sessions.get(user_id):
            return sessions.get(user_id)

        new_session = Session(user_id)
        session_id = new_session.id
        session = new_session  # Arc::new(Mutex::new(new_session)); TODO 需要增加异步锁
        sessions[user_id] = session

        if len(sessions) >= SESSION_COUNT_WARNING_THRESHOLD and len(sessions) % 100 == 0:
            logging.warning(
                f"同时存活的session过多: {len(sessions)}个。每 10 分钟运行一次修剪；请考虑减少 session_idle_timeout。"
            )

        # 触发钩子（fire-and-forget）
        if self.hooks:
            # 创建后台任务，不等待
            asyncio.create_task(self._fire_session_start_hook(self.hooks, user_id, session_id))

        return session

    async def resolve_thread(
            self,
            user_id: str,
            channel: str,
            external_thread_id: Optional[str] = None,
    ) -> Tuple[Session, UUID]:
        """
        将外部线程 ID 解析为内部线程。
        返回会话和线程 ID。如果它们不存在，则创建。
        """

        session = await self.get_or_create_session(user_id)

        key = ThreadKey(user_id, channel, external_thread_id)

        # 检查映射表
        thread_map = await self.thread_map.read()

        if thread_id := thread_map.get(key):
            # 验证线程是否仍存在于会话中
            sess = await session.lock()
            if sess.threads.contains_key(thread_id):
                return (session, thread_id)

        # 如果 external_thread_id 本身是一个合法的 UUID，且该线程存在于会话中，但可能未注册到 thread_map 中（例如从数据库恢复）
        # （例如，由 chat_new_thread_handler 创建或从数据库获取）。
        # 仅当没有 thread_map 条目映射到此 UUID 时才采用它——否则它属于不同的频道范围。
        if external_thread_id:
            try:
                ext_uuid = UUID(external_thread_id)
                thread_map = await self.thread_map.read()
                mapped_elsewhere = thread_map.values().any(ext_uuid)
                drop(thread_map)

                if not mapped_elsewhere:
                    # 验证线程是否仍存在于会话中
                    sess = await session.lock()
                    if sess.threads.contains_key(ext_uuid):
                        drop(sess)
                        thread_map = await self.thread_map.write()
                        # 获取写锁后重新检查，以防止出现竞争条件，即其他任务在我们的读写操作之间映射了此UUID
                        if thread_map.values().any(ext_uuid):
                            thread_map[key] = ext_uuid
                            drop(thread_map)
                            # 确保undo manager存在
                            undo_managers = await self.undo_managers.write()
                            # undo_managers
                            # .entry(ext_uuid)
                            # .or_insert_with(|| Arc::new(Mutex::new(UndoManager::new())));

                            return (session, ext_uuid)

            except ValueError:
                pass

        # 4. 创建新线程, always create a new one for a new key
        sess = await session.lock()
        thread = sess.create_thread()
        thread_id = thread.id

        # 存储映射
        thread_map = await self.thread_map.write()
        thread_map[key] = thread_id

        # 创建撤销管理器
        undo_managers = await self.undo_managers.write()
        # undo_managers.insert(thread_id, Arc::new(Mutex::new(UndoManager::new())));

        return (session, thread_id)