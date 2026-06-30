# 记忆文档——持久化知识的单位。
#
# 记忆文档是已完成线程反思后生成的结构化知识。
# 它们限定在项目范围内，用于上下文构建（检索而非原始历史重放）。


import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .project import ProjectId
from .thread import ThreadId
from ..types import OwnerId, default_user_id


@dataclass(frozen=True)
class DocId:
    """强类型文档标识符。"""
    value: uuid.UUID = field(default_factory=uuid.uuid4)

    def __str__(self) -> str:
        return str(self.value)


class DocType(str, Enum):
    """内存文档捕获的知识类型。"""
    # 线程完成了什么
    Summary = "summary"
    # 来自经验的持久学习
    Lesson = "lesson"
    # 检测到的待跟进问题
    Issue = "issue"
    # 缺失的能力请求
    Spec = "spec"
    # 工作内存 / 草稿笔记
    Note = "note"
    # 具有激活元数据和可选代码片段的可重用技能
    Skill = "skill"
    # 具有步骤、状态和进度跟踪的结构化执行计划
    Plan = "plan"


def _default_user_id() -> str:
    """默认用户 ID（用于 serde 默认值）。"""
    return "legacy"


@dataclass(kw_only=True)
class MemoryDoc:
    """内存文档——结构化的持久知识。"""
    id: DocId = field(default_factory=DocId)
    project_id: ProjectId
    # 租户隔离：拥有此文档的用户
    user_id: str = default_user_id
    doc_type: DocType
    title: str
    content: str
    source_thread_id: Optional[ThreadId] = None
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def with_source_thread(self, thread_id: ThreadId) -> "MemoryDoc":
        """设置来源线程。"""
        self.source_thread_id = thread_id
        return self

    def with_tags(self, tags: List[str]) -> "MemoryDoc":
        """设置标签。"""
        self.tags = tags
        return self

    @property
    def owner_id(self) -> OwnerId:
        """获取所有者 ID。"""
        return OwnerId.from_user_id(self.user_id)

    def is_owned_by(self, user_id: str) -> bool:
        """检查是否由指定用户拥有。"""
        return self.owner_id().matches_user(user_id)
