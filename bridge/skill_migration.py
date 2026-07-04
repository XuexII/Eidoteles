# V1 → V2 技能迁移。
#
# 将 v1 `LoadedSkill` 实例（来自文件系统的 SKILL.md 文件）转换为 v2 `MemoryDoc`，其 `DocType` 为 `Skill` 并带有结构化的 `V2SkillMetadata`。
# 该迁移是幂等的：`content_hash` 未改变的技能会被跳过。
#
# **在 v1 迁移完成后移除。** 一旦所有用户都使用 `ENGINE_V2` 并且 SKILL.md 文件直接作为 v2 MemoryDoc 编写（或通过技能提取任务），此一次性迁移代码就不再需要。
# `migrate_v1_skills` / `migrate_v1_skill_list` 函数以及 `bridge/router.rs:init_engine()` 中的调用点都可以删除。

from engine.traits.store import Store
from engine.types.error import EngineError
from engine.types.memory import (DocType, MemoryDoc)
from engine.types.project import ProjectId
from engine.types import shared_owner_id

from skills import SkillRegistry
from skills.types import (LoadedSkill, SkillSource)
from skills.v2 import (SkillMetrics, V2SkillMetadata, V2SkillSource)

import logging
from typing import Optional, List

logger = logging.getLogger(__name__)


# ── 技能迁移 ─────────────────────────────────────────────────

async def migrate_v1_skills(
        v1_registry: "SkillRegistry",
        store: "Store",
        project_id: "ProjectId",
        owner_id: str,
) -> int:
    """将 v1 技能迁移到 v2 MemoryDocs

    从 v1 `SkillRegistry` 读取所有技能，将每个转换为带有 `DocType::Skill` 和
    `V2SkillMetadata` 的 `MemoryDoc`，并保存到 Store

    返回迁移或更新的技能数量
    """
    return await migrate_v1_skill_list(
        v1_registry.skills(), store, project_id, owner_id,
    )


async def migrate_v1_skill_list(
        v1_skills: List["LoadedSkill"],
        store: "Store",
        project_id: "ProjectId",
        owner_id: str,
) -> int:
    """将 v1 技能的快照迁移到 v2 MemoryDocs

    接受预克隆的技能切片（以避免在 await 期间持有锁）
    """
    if not v1_skills:
        return 0

    # 加载现有的技能文档（所有者特定和共享的）以按 content_hash 检查重复。
    # 用户/工作区技能以 owner_id 迁移，因此我们必须包含所有者文档 —
    # 否则它们会在每次启动时重新迁移
    existing_docs = await store.list_shared_memory_docs(project_id)
    owner_docs = await store.list_memory_docs(project_id, owner_id)
    existing_docs.extend(owner_docs)

    existing_hashes = set()
    for doc in existing_docs:
        if doc.doc_type == DocType.Skill:
            try:
                meta = V2SkillMetadata.from_dict(doc.metadata) if isinstance(doc.metadata, dict) else None
                if meta is not None and meta.content_hash:
                    existing_hashes.add(meta.content_hash)
            except Exception:
                pass

    migrated = 0

    for skill in v1_skills:
        # 如果内容未更改则跳过（幂等）
        if skill.content_hash in existing_hashes:
            logger.debug(
                f"跳过 v1 技能迁移: 内容未更改 (skill={skill.name()})"
            )
            continue

        doc = await v1_skill_to_memory_doc(skill, project_id, owner_id)
        await store.save_memory_doc(doc)
        migrated += 1

        logger.debug(
            f"已将 v1 技能迁移到 v2 MemoryDoc (skill={skill.name()}, doc_id={doc.id})"
        )

    if migrated > 0:
        logger.debug(f"已将 {migrated} 个 v1 技能迁移到 v2 引擎")

    return migrated


async def sync_v1_skill_to_store(
        skill: "LoadedSkill",
        store: "Store",
        project_id: "ProjectId",
) -> "MemoryDoc":
    """将单个刚安装的 v1 技能同步到 v2 存储中，当存在现有的 `skill:<name>` 文档时原地更新

    由 `EffectBridgeAdapter` 中的 `skill_install` 后钩子调用，以便运行时安装的技能
    立即对 v2 引擎可见。幂等：如果已存在具有相同 content_hash 的文档，则原样返回

    共享技能文档位于共享所有者下，并通过 `list_skills_global()` 对所有项目可见；
    将查找范围限定为 `project_id` 会在不同项目之间创建重复的共享文档
    （在每用户项目中常见）。我们使用全局技能列表，
    以便已存在于不同项目的 `project_id` 下的共享技能被原地更新
    """
    title = f"skill:{skill.manifest.name}"
    all_skills = await store.list_skills_global()

    existing = None
    for doc in all_skills:
        if doc.doc_type == DocType.Skill and doc.title == title:
            existing = doc
            break

    if existing is not None:
        try:
            meta = V2SkillMetadata.from_dict(existing.metadata) if isinstance(existing.metadata, dict) else None
            if (meta is not None
                    and existing.content == skill.prompt_content
                    and meta.content_hash == skill.content_hash):
                return existing
        except Exception:
            pass

    # 对实时安装的技能使用 shared_owner_id — 它们来自注册表
    doc = await v1_skill_to_memory_doc(skill, project_id, shared_owner_id())
    if existing is not None:
        doc.id = existing.id
        doc.project_id = existing.project_id
        doc.created_at = existing.created_at

    await store.save_memory_doc(doc)
    return doc


async def v1_skill_to_memory_doc(
        skill: "LoadedSkill",
        project_id: "ProjectId",
        owner_id: str,
) -> "MemoryDoc":
    """将单个 v1 `LoadedSkill` 转换为 v2 `MemoryDoc`"""
    # 用户和工作区安装的技能属于所有者。
    # 捆绑和注册表安装的技能在所有用户之间共享
    if skill.source.source_type in ("User", "Workspace"):
        user_id = owner_id
    else:
        user_id = shared_owner_id()

    # 提取捆绑路径和来源 URL
    bundle_path = str(skill.source.path) if skill.source.path is not None else None
    source_url = None
    if skill.source.path is not None:
        install_meta = await SkillRegistry.read_install_metadata(skill.source.path)
        if install_meta is not None:
            source_url = install_meta.source_url

    # 构建 V2SkillMetadata
    activation = skill.manifest.activation if hasattr(skill.manifest, 'activation') else None
    requires = skill.manifest.requires if hasattr(skill.manifest, 'requires') else None

    meta = V2SkillMetadata(
        version=1,
        description=skill.manifest.description if hasattr(skill.manifest, 'description') else "",
        activation=activation,
        metrics=SkillMetrics(),
        parent_version=None,
        revisions=[],
        repairs=[],
        content_hash=skill.content_hash if hasattr(skill, 'content_hash') else "",
    )

    doc = MemoryDoc.new(
        project_id,
        user_id,
        DocType.Skill,
        f"skill:{skill.manifest.name}",
        skill.prompt_content if hasattr(skill, 'prompt_content') else "",
    )
    doc.metadata = meta.to_dict()
    doc.tags = ["migrated_from_v1"]

    return doc