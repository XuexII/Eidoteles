# 引擎持久化的存储特质。
#
# 为所有引擎类型定义 CRUD 操作。主 crate 通过包装其双后端 `Database` 特质（PostgreSQL + libSQL）来实现它。

from ..types.capability import CapabilityLease, LeaseId
from ..types.conversation import ConversationId, ConversationSurface
from ..types.error import EngineError
from ..types.event import ThreadEvent
from ..types.memory import DocId, MemoryDoc
from ..types.mission import Mission, MissionId, MissionStatus
from ..types.project import Project, ProjectId
from ..types.step import Step
from ..types.thread import Thread, ThreadId, ThreadState
from ..types import is_shared_owner, shared_owner_candidates
from abc import ABC, abstractmethod
from typing import Optional, List


class Store(ABC):
    """引擎的持久化抽象"""

    # ── 线程操作 ───────────────────────────────────

    @abstractmethod
    async def save_thread(self, thread: Thread) -> None:
        """保存线程"""
        ...

    @abstractmethod
    async def load_thread(self, id: ThreadId) -> Optional[Thread]:
        """加载线程"""
        ...

    @abstractmethod
    async def list_threads(
            self,
            project_id: ProjectId,
            user_id: str,
    ) -> List[Thread]:
        """列出项目中的线程"""
        ...

    @abstractmethod
    async def update_thread_state(
            self,
            id: ThreadId,
            state: ThreadState,
    ) -> None:
        """更新线程状态"""
        ...

    # ── 步骤操作 ─────────────────────────────────────

    @abstractmethod
    async def save_step(self, step: Step) -> None:
        """保存步骤"""
        ...

    @abstractmethod
    async def load_steps(self, thread_id: ThreadId) -> List[Step]:
        """加载线程的步骤"""
        ...

    # ── 事件操作 ────────────────────────────────────

    @abstractmethod
    async def append_events(self, events: List[ThreadEvent]) -> None:
        """追加事件"""
        ...

    @abstractmethod
    async def load_events(self, thread_id: ThreadId) -> List[ThreadEvent]:
        """加载线程的事件"""
        ...

    # ── 项目操作 ──────────────────────────────────

    @abstractmethod
    async def save_project(self, project: Project) -> None:
        """保存项目"""
        ...

    @abstractmethod
    async def load_project(self, id: ProjectId) -> Optional[Project]:
        """加载项目"""
        ...

    async def list_projects(self, user_id: str) -> List[Project]:
        """列出用户的项目"""
        raise EngineError(f"Store: 未为用户 '{user_id}' 实现 list_projects")

    async def list_all_projects(self) -> List[Project]:
        """列出所有项目"""
        raise EngineError("Store: 未实现 list_all_projects")

    # ── 对话操作 ─────────────────────────────

    async def save_conversation(self, conversation: ConversationSurface) -> None:
        """保存对话"""
        raise EngineError(f"Store: 未为对话 '{conversation.id}' 实现 save_conversation")

    async def load_conversation(self, id: ConversationId) -> Optional[ConversationSurface]:
        """加载对话"""
        raise EngineError(f"Store: 未为 '{id}' 实现 load_conversation")

    async def list_conversations(self, user_id: str) -> List[ConversationSurface]:
        """列出用户的对话"""
        raise EngineError(f"Store: 未为用户 '{user_id}' 实现 list_conversations")

    # ── 记忆文档操作 ───────────────────────────────

    @abstractmethod
    async def save_memory_doc(self, doc: MemoryDoc) -> None:
        """保存记忆文档"""
        ...

    @abstractmethod
    async def load_memory_doc(self, id: DocId) -> Optional[MemoryDoc]:
        """加载记忆文档"""
        ...

    @abstractmethod
    async def list_memory_docs(
            self,
            project_id: ProjectId,
            user_id: str,
    ) -> List[MemoryDoc]:
        """列出项目中的记忆文档"""
        ...

    async def list_memory_docs_with_shared(
            self,
            project_id: ProjectId,
            user_id: str,
    ) -> List[MemoryDoc]:
        """列出用户可见的记忆文档：自己的文档 + 共享文档

        这是"共享空间"模式：管理员可以在共享所有者 ID 下安装技能和知识，
        它们与个人文档一起对所有用户可见。用于技能列表、上下文检索
        以及任何共享知识应可访问的地方
        """
        if is_shared_owner(user_id):
            return await self.list_shared_memory_docs(project_id)

        docs = await self.list_memory_docs(project_id, user_id)
        shared_docs = await self.list_shared_memory_docs(project_id)
        docs.extend(shared_docs)
        return docs

    async def list_shared_memory_docs(self, project_id: ProjectId) -> List[MemoryDoc]:
        """列出项目的共享记忆文档"""
        docs = []
        seen_ids = set()
        for owner_id in shared_owner_candidates():
            owner_docs = await self.list_memory_docs(project_id, owner_id)
            for doc in owner_docs:
                doc_id = doc.id.value if hasattr(doc.id, 'value') else str(doc.id)
                if doc_id not in seen_ids:
                    seen_ids.add(doc_id)
                    docs.append(doc)
        # 按 ID 排序
        docs.sort(key=lambda d: d.id.value if hasattr(d.id, 'value') else str(d.id))
        return docs

    async def list_skills_global(self) -> List[MemoryDoc]:
        """列出所有项目中共享/管理员拥有的所有技能文档

        与 `list_shared_memory_docs` 不同，此方法忽略 `project_id` —
        它返回每个具有共享所有者的 `DocType::Skill`，
        无论其位于哪个项目中。由 `__list_skills__` 使用，
        以便管理员安装的技能（迁移到所有者的默认项目中）
        对线程在按用户划分的项目中运行的门户用户可见

        调用者应与其他文档源（例如用户自己的文档）一起排序和去重合并结果，
        因此此方法不排序 — 仅按 `DocId` 去重以折叠跨所有者候选项的重复项
        """
        docs = []
        seen_ids = set()
        for owner_id in shared_owner_candidates():
            owner_docs = await self.list_memory_docs_by_owner(owner_id)
            for doc in owner_docs:
                if doc.doc_type == DocType.Skill:
                    doc_id = doc.id.value if hasattr(doc.id, 'value') else str(doc.id)
                    if doc_id not in seen_ids:
                        seen_ids.add(doc_id)
                        docs.append(doc)
        return docs

    async def list_memory_docs_by_owner(self, user_id: str) -> List[MemoryDoc]:
        """列出特定用户在所有项目中拥有的所有记忆文档

        默认实现通过 `list_all_projects` 遍历所有项目。
        这对于不拥有任何项目但文档存储在其他用户项目中的共享所有者
        （`__shared__`、`system`）是必需的

        **性能警告：** 默认实现是 O(项目数 × 每个项目的文档数)，
        并在每个线程步骤上通过 `list_skills_global` 从 `__list_skills__` 热路径调用。
        生产 `Store` 实现**必须**用扁平查询覆盖此方法
        （例如 `WHERE user_id = $1`）— 保留默认实现将在项目数量增长时
        使技能列表成为瓶颈
        """
        projects = await self.list_all_projects()
        docs = []
        for project in projects:
            project_docs = await self.list_memory_docs(project.id, user_id)
            docs.extend(project_docs)
        return docs

    # ── 能力租约操作 ─────────────────────────

    @abstractmethod
    async def save_lease(self, lease: CapabilityLease) -> None:
        """保存租约"""
        ...

    @abstractmethod
    async def load_active_leases(self, thread_id: ThreadId) -> List[CapabilityLease]:
        """加载线程的活跃租约"""
        ...

    @abstractmethod
    async def revoke_lease(self, lease_id: LeaseId, reason: str) -> None:
        """撤销租约"""
        ...

    # ── 任务操作 ───────────────────────────────────

    @abstractmethod
    async def save_mission(self, mission: Mission) -> None:
        """保存任务"""
        ...

    @abstractmethod
    async def load_mission(self, id: MissionId) -> Optional[Mission]:
        """加载任务"""
        ...

    @abstractmethod
    async def list_missions(
            self,
            project_id: ProjectId,
            user_id: str,
    ) -> List[Mission]:
        """列出项目中的任务"""
        ...

    @abstractmethod
    async def update_mission_status(
            self,
            id: MissionId,
            status: MissionStatus,
    ) -> None:
        """更新任务状态"""
        ...

    async def list_missions_with_shared(
            self,
            project_id: ProjectId,
            user_id: str,
    ) -> List[Mission]:
        """列出用户可见的任务：自己的 + 共享的任务

        共享学习任务（自我改进、技能提取等）在共享所有者 ID 下创建，
        应对所有用户通过 API 可见/可管理
        """
        if is_shared_owner(user_id):
            return await self.list_shared_missions(project_id)

        missions = await self.list_missions(project_id, user_id)
        shared = await self.list_shared_missions(project_id)
        missions.extend(shared)
        # 确定性排序，以便调用者（LLM 工具调度、重放测试、UI 列表）
        # 看到独立于 HashMap 迭代的稳定顺序。
        # 排序键：名称（稳定、人类可读），然后 ID（任何重复名称的决胜者）
        missions.sort(key=lambda m: (m.name, m.id.value if hasattr(m.id, 'value') else str(m.id)))
        return missions

    async def list_shared_missions(self, project_id: ProjectId) -> List[Mission]:
        """列出项目的共享任务"""
        missions = []
        seen_ids = set()
        for owner_id in shared_owner_candidates():
            owner_missions = await self.list_missions(project_id, owner_id)
            for mission in owner_missions:
                mission_id = mission.id.value if hasattr(mission.id, 'value') else str(mission.id)
                if mission_id not in seen_ids:
                    seen_ids.add(mission_id)
                    missions.append(mission)
        missions.sort(key=lambda m: m.id.value if hasattr(m.id, 'value') else str(m.id))
        return missions

    # ── 管理员操作（系统级，跨租户）──────────

    async def list_all_threads(self, project_id: ProjectId) -> List[Thread]:
        """列出项目中所有线程，无论用户。
        用于：恢复、启动时的后台线程恢复
        """
        raise EngineError(f"Store: 未为项目 '{project_id}' 实现 list_all_threads")

    async def list_all_missions(self, project_id: ProjectId) -> List[Mission]:
        """列出项目中所有任务，无论用户。
        用于：cron 计时器、事件监听器、引导
        """
        raise EngineError(f"Store: 未为项目 '{project_id}' 实现 list_all_missions")