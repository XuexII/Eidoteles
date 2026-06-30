# 技能置信度追踪。
#
# 追踪自动提取的技能的使用情况和成功/失败指标。
# 每个线程完成后，根据线程成功或失败更新活动技能的指标。

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Callable, List

from skills.v2 import SkillRevision, V2SkillMetadata

from ..traits.store import Store
from ..types.memory import DocId, DocType

logger = logging.getLogger(__name__)


# ── 内容哈希 ─────────────────────────────────────────────────

def compute_content_hash(content: str) -> str:
    """计算内容的 SHA256 哈希值"""
    hasher = hashlib.sha256()
    hasher.update(content.encode('utf-8'))
    return f"sha256:{hasher.hexdigest()}"


# ── 技能追踪器 ───────────────────────────────────────────────

@dataclass
class SkillTracker:
    """追踪技能使用并更新置信度指标"""
    store: Store

    async def record_usage(self, doc_id: DocId, success: bool) -> None:
        """记录在已完成的线程中使用了某个技能

        加载技能的 MemoryDoc，更新元数据 JSON 中的指标，并保存回去。
        如果文档缺失、不是 Skill 或具有无效的元数据，返回 `EngineError::Skill` —
        调用者决定是传播还是记录并吞掉
        """
        doc = await self.store.load_memory_doc(doc_id)
        if doc is None:
            raise EngineError(f"Skill: 未找到技能文档: {doc_id}")

        if doc.doc_type != DocType.Skill:
            raise EngineError(f"Skill: 文档 {doc_id} 不是技能（类型: {doc.doc_type}）")

        # 解析元数据
        try:
            meta = V2SkillMetadata.from_dict(doc.metadata) if isinstance(doc.metadata, dict) else V2SkillMetadata()
        except Exception as e:
            raise EngineError(f"Skill: 文档 {doc_id} 的技能元数据无效: {e}")

        # 更新指标
        meta.metrics.usage_count += 1
        if success:
            meta.metrics.success_count += 1
        else:
            meta.metrics.failure_count += 1
        meta.metrics.last_used = datetime.now(timezone.utc)

        # 更新文档
        updated_doc = doc.clone()
        updated_doc.metadata = meta.to_dict()
        updated_doc.updated_at = datetime.now(timezone.utc)

        await self.store.save_memory_doc(updated_doc)

    async def update_skill(
            self,
            doc_id: DocId,
            new_content: str,
            expected_version: Optional[int] = None,
            updater: Optional[Callable[[dict], None]] = None,
    ) -> None:
        """更新技能的内容并增加其版本

        在递增之前将 `parent_version` 设置为当前版本，
        以便在更新引起问题时进行回滚
        """
        doc = await self.store.load_memory_doc(doc_id)
        if doc is None:
            raise EngineError(f"Skill: 未找到技能文档: {doc_id}")

        # 解析元数据
        try:
            meta = V2SkillMetadata.from_dict(doc.metadata) if isinstance(doc.metadata, dict) else V2SkillMetadata()
        except Exception as e:
            raise EngineError(f"Skill: 无效的技能元数据: {e}")

        # 版本冲突检查
        if expected_version is not None and meta.version != expected_version:
            raise EngineError(
                f"Skill: 技能 {doc_id} 版本冲突: 期望 {expected_version}，实际 {meta.version}"
            )

        # 始终从实际内容重新计算 — 如果文档在此追踪器之外被更新
        # （例如直接 memory_write），meta.content_hash 可能已漂移
        archived_hash = compute_content_hash(doc.content)
        meta.revisions.append(SkillRevision(
            version=meta.version,
            content=doc.content,
            description=meta.description,
            activation=meta.activation,
            code_snippets=meta.code_snippets,
            content_hash=archived_hash,
            archived_at=datetime.now(timezone.utc),
        ))

        # 将内存中的修订版本限制在 10 个以内，以限制每次 load_memory_doc 的元数据大小。
        # 这是一个务实的权衡：嵌入在技能 JSON 中的完整提示快照
        # 每个修订版本可能增长到数 KB。较旧的修订版本被丢弃；
        # 如果需要长期保留，应将其外部化到单独的 MemoryDoc 中
        if len(meta.revisions) > 10:
            keep_from = len(meta.revisions) - 10
            meta.revisions = meta.revisions[keep_from:]

        meta.parent_version = meta.version
        meta.version += 1

        if updater is not None:
            updater(meta)

        meta.content_hash = compute_content_hash(new_content)

        # 更新文档
        updated_doc = doc.clone()
        updated_doc.content = new_content
        updated_doc.metadata = meta.to_dict()
        updated_doc.updated_at = datetime.now(timezone.utc)

        await self.store.save_memory_doc(updated_doc)

    async def rollback_skill(self, doc_id: DocId) -> None:
        """将技能回滚到其上一个版本

        如果存在 `parent_version` 的存档修订版本，恢复完整的内容和元数据快照。
        否则对于较旧的技能，回退到简单的版本递减而不恢复内容
        """
        doc = await self.store.load_memory_doc(doc_id)
        if doc is None:
            raise EngineError(f"Skill: 未找到技能文档: {doc_id}")

        # 解析元数据
        try:
            meta = V2SkillMetadata.from_dict(doc.metadata) if isinstance(doc.metadata, dict) else V2SkillMetadata()
        except Exception as e:
            raise EngineError(f"Skill: 无效的技能元数据: {e}")

        parent = meta.parent_version
        if parent is None:
            raise EngineError(f"Skill: 技能 {doc_id} 没有可回滚到的父版本")

        # 查找存档的修订版本
        revision_index = None
        for i, revision in enumerate(meta.revisions):
            if revision.version == parent:
                revision_index = i
                break

        if revision_index is not None:
            # 从存档修订版本恢复
            revision = meta.revisions[revision_index]
            rolled_content = revision.content
            meta.version = revision.version
            meta.description = revision.description
            meta.activation = revision.activation
            meta.code_snippets = revision.code_snippets
            meta.content_hash = revision.content_hash

            # 保留版本低于回滚目标的修订版本
            meta.revisions = [r for r in meta.revisions if r.version < revision.version]
            # 保留版本不高于回滚目标的修复
            meta.repairs = [r for r in meta.repairs if r.to_version <= revision.version]

            # 更新父版本为剩余修订版本中的最大值
            remaining_versions = [r.version for r in meta.revisions]
            meta.parent_version = max(remaining_versions) if remaining_versions else None
        else:
            # 简单回退：递减版本，保留内容
            rolled_content = doc.content
            meta.version = parent
            meta.parent_version = None

        # 更新文档
        updated_doc = doc.clone()
        updated_doc.content = rolled_content
        updated_doc.metadata = meta.to_dict()
        updated_doc.updated_at = datetime.now(timezone.utc)

        await self.store.save_memory_doc(updated_doc)


# ── 辅助数据结构 ─────────────────────────────────────────────

@dataclass
class SkillMetrics:
    """技能使用指标"""
    usage_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_used: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "usage_count": self.usage_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_used": self.last_used.isoformat() if self.last_used else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SkillMetrics":
        last_used = data.get("last_used")
        if last_used and isinstance(last_used, str):
            last_used = datetime.fromisoformat(last_used)
        return cls(
            usage_count=data.get("usage_count", 0),
            success_count=data.get("success_count", 0),
            failure_count=data.get("failure_count", 0),
            last_used=last_used,
        )


@dataclass
class SkillRevision:
    """技能修订版本"""
    version: int
    content: str
    description: str = ""
    activation: Optional[dict] = None
    code_snippets: List[str] = None
    content_hash: str = ""
    archived_at: Optional[datetime] = None

    def __post_init__(self):
        if self.code_snippets is None:
            self.code_snippets = []

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "content": self.content,
            "description": self.description,
            "activation": self.activation,
            "code_snippets": self.code_snippets,
            "content_hash": self.content_hash,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SkillRevision":
        archived_at = data.get("archived_at")
        if archived_at and isinstance(archived_at, str):
            archived_at = datetime.fromisoformat(archived_at)
        return cls(
            version=data.get("version", 0),
            content=data.get("content", ""),
            description=data.get("description", ""),
            activation=data.get("activation"),
            code_snippets=data.get("code_snippets", []),
            content_hash=data.get("content_hash", ""),
            archived_at=archived_at,
        )


@dataclass
class SkillRepair:
    """技能修复记录"""
    to_version: int
    description: str = ""
    applied_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "to_version": self.to_version,
            "description": self.description,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SkillRepair":
        applied_at = data.get("applied_at")
        if applied_at and isinstance(applied_at, str):
            applied_at = datetime.fromisoformat(applied_at)
        return cls(
            to_version=data.get("to_version", 0),
            description=data.get("description", ""),
            applied_at=applied_at,
        )


@dataclass
class V2SkillMetadata:
    """V2 技能元数据"""
    version: int = 0
    parent_version: Optional[int] = None
    description: str = ""
    activation: Optional[dict] = None
    code_snippets: List[str] = None
    content_hash: str = ""
    metrics: SkillMetrics = None
    revisions: List[SkillRevision] = None
    repairs: List[SkillRepair] = None

    def __post_init__(self):
        if self.code_snippets is None:
            self.code_snippets = []
        if self.metrics is None:
            self.metrics = SkillMetrics()
        if self.revisions is None:
            self.revisions = []
        if self.repairs is None:
            self.repairs = []

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "parent_version": self.parent_version,
            "description": self.description,
            "activation": self.activation,
            "code_snippets": self.code_snippets,
            "content_hash": self.content_hash,
            "metrics": self.metrics.to_dict(),
            "revisions": [r.to_dict() for r in self.revisions],
            "repairs": [r.to_dict() for r in self.repairs],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "V2SkillMetadata":
        return cls(
            version=data.get("version", 0),
            parent_version=data.get("parent_version"),
            description=data.get("description", ""),
            activation=data.get("activation"),
            code_snippets=data.get("code_snippets", []),
            content_hash=data.get("content_hash", ""),
            metrics=SkillMetrics.from_dict(data.get("metrics", {})),
            revisions=[SkillRevision.from_dict(r) for r in data.get("revisions", [])],
            repairs=[SkillRepair.from_dict(r) for r in data.get("repairs", [])],
        )
