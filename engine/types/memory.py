"""
记忆文档——持久化知识的单位

记忆文档是已完成线程反思后生成的结构化知识
它们限定在项目范围内，用于上下文构建（检索而非原始历史重放）
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from ironclaw_common.common import OwnerId, DEFAULT_USER_ID


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


def generate_doc_id() -> str:
    return str(uuid.uuid4())


@dataclass(kw_only=True)
class MemoryDoc:
    """内存文档——结构化的持久知识。"""
    _id: str = field(default_factory=generate_doc_id, init=False)
    # 所属项目，用于上下文隔离
    project_id: str
    # 租户隔离：拥有此文档的用户
    user_id: str = DEFAULT_USER_ID
    # 文档类型（Summary/Lesson/Issue/Spec/Note/Skill/Plan)
    doc_type: DocType
    # 文档标题，用于显示和检索
    title: str
    # 文档内容，存储实际知识
    content: str
    # 来源thread ID，用于追溯知识来源
    source_thread_id: Optional[str] = None
    # 标签，用于分类和检索
    tags: List[str] = field(default_factory=list)
    # 扩展元数据，存储自定义信息
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def id(self):
        return self._id

    def with_source_thread(self, thread_id: str):
        """设置来源线程。"""
        self.source_thread_id = thread_id

    def with_tags(self, tags: List[str]):
        """设置标签。"""
        self.tags = tags

    @property
    def owner_id(self) -> OwnerId:
        """获取所有者 ID。"""
        return OwnerId.from_user_id(self.user_id)

    def is_owned_by(self, user_id: str) -> bool:
        """检查是否由指定用户拥有。"""
        return self.owner_id.matches_user(user_id)
