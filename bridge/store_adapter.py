"""
混合存储适配器——引擎状态的基于工作区的持久化。

知识文档使用 frontmatter+markdown 以提升人类可读性。
运行时状态使用 `runtime/` 下的 JSON 格式，避免干扰。

所有 v2 引擎状态都存放在 `.system/engine/` 下，与其他机器管理的状态（`.system/settings/`、`.system/extensions/`、`.system/skills/`）并列。

## 工作区布局

```
text
.system/engine/
├── README.md                                   （自动生成的索引）
├── knowledge/{类型}/{slug}--{id8}.md           （frontmatter + 内容）
├── orchestrator/v{N}.py                        （Python 编排器版本）
├── orchestrator/failures.json
├── orchestrator/codeact-preamble-overlay.md    （运行时提示词补丁）
├── projects/{slug}--{id8}.json
├── projects/{slug}/missions/{slug}--{id8}/mission.json
└── runtime/                                    （内部使用，不供浏览）
    ├── threads/active/{id}.json
    ├── threads/archive/{slug}.json             （压缩后的摘要）
    ├── conversations/{id}.json
    ├── leases/{id}.json
    ├── events/{thread_id}.json
    └── steps/{thread_id}.json
```
"""

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any

import yaml

from engine import (
    CapabilityLease,
    ConversationId,
    ConversationSurface,
    DocId,
    DocType,
    EngineError,
    LeaseId,
    MemoryDoc,
    Project,
    ProjectId,
    Step,
    Store,
    Thread,
    ThreadEvent,
    ThreadId,
    ThreadState
)
from engine.types.mission import Mission, MissionId, MissionStatus
from workspace import Workspace, WorkspaceEntry

logger = logging.getLogger(__name__)

# ── 路径常量 ──────────────────────────────────────────────────
#
# 所有 v2 引擎状态都存放在 `.system/engine/` 下，与其他机器管理的状态
# （`.system/settings/`、`.system/extensions/`、`.system/skills/`）并列。
# `.system/` 上的点前缀是隐藏标记；`runtime/` 不需要内部点

KNOWLEDGE_PREFIX = ".system/engine/knowledge"
ORCHESTRATOR_PREFIX = ".system/engine/orchestrator"
# #2049 之前的编排器前缀；与规范前缀一起匹配，以便针对旧路径的新写入仍然触发保护检查
LEGACY_ORCHESTRATOR_PREFIX = "engine/orchestrator"
# 仅用于任务存储的引擎拥有的项目目录。项目元数据存放在面向用户的
# `projects/<slug>/.project.json` 下 — 任务保持隐藏在此处，
# 这样用户的工作区视图不会在其自己的文档旁边出现机器管理的任务 JSON
PROJECTS_PREFIX = ".system/engine/projects"
# 面向用户的项目根目录。在 `projects/<slug>/...` 下写入文件是声明项目存在的动作 —
# 不需要单独的 schema，不需要 `project_create` 工具
PROJECTS_ROOT = "projects"
# 每个项目的元数据文件（名称、描述、目标、指标）。可选：
# 缺失意味着项目仅通过其 slug 命名，元数据为空
PROJECT_METADATA_FILENAME = ".project.json"

THREADS_PREFIX = ".system/engine/runtime/threads/active"
THREAD_ARCHIVE_PREFIX = ".system/engine/runtime/threads/archive"
STEPS_PREFIX = ".system/engine/runtime/steps"
EVENTS_PREFIX = ".system/engine/runtime/events"
LEASES_PREFIX = ".system/engine/runtime/leases"
CONVERSATIONS_PREFIX = ".system/engine/runtime/conversations"

# 在 #2049 将引擎状态移动到 `.system/engine/...` 之前使用的旧版 `engine/...` 根目录。
# `migrate_legacy_engine_paths` 在启动时将在此前缀下找到的任何文档重写到新位置。
# 无限期保留，以便在长时间暂停后升级的旧工作区仍然可以迁移
LEGACY_ENGINE_ROOT = "engine"
NEW_ENGINE_ROOT = ".system/engine"

# 用于特殊路由的知名标题（必须匹配引擎 crate 常量）
ORCHESTRATOR_MAIN_TITLE = "orchestrator:main"
ORCHESTRATOR_FAILURES_TITLE = "orchestrator:failures"
PREAMBLE_OVERLAY_TITLE = "prompt:codeact_preamble"
ORCHESTRATOR_CODE_TAG = "orchestrator_code"
FIX_PATTERN_TITLE = "fix_pattern_database"


@dataclass
class HybridStore(Store):
    """基于工作区的引擎存储"""
    threads: Dict[ThreadId, Thread] = field(default_factory=dict)
    steps: Dict[ThreadId, List[Step]] = field(default_factory=dict)
    events: Dict[ThreadId, List[ThreadEvent]] = field(default_factory=dict)
    projects: Dict[ProjectId, Project] = field(default_factory=dict)
    conversations: Dict[ConversationId, ConversationSurface] = field(default_factory=dict)
    leases: Dict[LeaseId, CapabilityLease] = field(default_factory=dict)
    missions: Dict[MissionId, Mission] = field(default_factory=dict)
    docs: Dict[DocId, MemoryDoc] = field(default_factory=dict)
    # 跟踪每个文档的当前工作区路径，以便重命名时可以删除旧文件
    doc_paths: Dict[DocId, str] = field(default_factory=dict)
    workspace: Optional[Workspace] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ── 状态加载 ──────────────────────────────────────────

    async def load_state_from_workspace(self) -> None:
        """在启动时从工作区加载持久化的引擎状态"""
        if self.workspace is None:
            return

        ws = self.workspace

        # 在加载器运行之前，将仍然存在于旧 `engine/...` 前缀下的任何状态
        # 迁移到 `.system/engine/...` 中，以便下面的加载看到单个规范位置，
        # 并且孤立的旧文档不会累积
        await self.migrate_legacy_engine_paths(ws)
        await self.load_knowledge_docs(ws)

        # 将仍然存在于旧引擎路径的任何项目 JSON 迁移到面向用户的
        # `projects/<slug>/.project.json` 布局中，然后从新位置加载
        await self.migrate_legacy_project_jsons(ws)
        await self.load_projects_from_workspace(ws)

        await self.load_map(ws, CONVERSATIONS_PREFIX, self._on_conversation)
        await self.load_map(ws, THREADS_PREFIX, self._on_thread)
        await self.load_map(ws, STEPS_PREFIX, self._on_steps)
        await self.load_map(ws, EVENTS_PREFIX, self._on_events)
        await self.load_map(ws, LEASES_PREFIX, self._on_lease)

        # 任务位于每个项目下: .system/engine/projects/{slug}/missions/{slug}/mission.json
        await self.load_missions_from_projects(ws)

        # 回填被任务引用但在活跃线程映射中缺失的线程
        await self.backfill_archived_threads(ws)

        logger.debug(
            f"从工作区加载引擎状态: "
            f"projects={len(self.projects)}, conversations={len(self.conversations)}, "
            f"threads={len(self.threads)}, steps={len(self.steps)}, "
            f"events={len(self.events)}, leases={len(self.leases)}, "
            f"missions={len(self.missions)}, docs={len(self.docs)}"
        )

    async def migrate_legacy_engine_paths(self, ws: Any) -> None:
        """一次性启动迁移：将存储在旧 `engine/...` 前缀下的任何文档重写到 `.system/engine/...` 中"""
        # 廉价的预检：大多数启动没有旧路径，不能为完整的 `list_all()` 遍历付出代价
        try:
            entries = await ws.list(LEGACY_ENGINE_ROOT)
            if not entries:
                return
        except Exception as e:
            logger.debug(f"旧引擎迁移: 预检列表失败: {e}")
            return

        # 预检看到了什么 — 回退到完整的 `list_all()` 以获取嵌套路径
        try:
            all_paths = await ws.list_all()
        except Exception as e:
            logger.debug(f"旧引擎迁移: list_all 失败: {e}")
            return

        legacy = []
        for p in all_paths:
            trimmed = p.lstrip('/')
            if trimmed == LEGACY_ENGINE_ROOT or trimmed.startswith(f"{LEGACY_ENGINE_ROOT}/"):
                legacy.append(p)

        if not legacy:
            return

        logger.debug(f"将 {len(legacy)} 个旧引擎路径迁移到 .system/engine/")

        migrated = 0
        failed = 0
        for old_path in legacy:
            trimmed = old_path.lstrip('/')
            suffix = trimmed[len(LEGACY_ENGINE_ROOT):].lstrip('/')
            new_path = NEW_ENGINE_ROOT if not suffix else f"{NEW_ENGINE_ROOT}/{suffix}"

            try:
                doc = await ws.read(old_path)
            except Exception as e:
                logger.debug(f"旧引擎迁移: 读取失败: old={old_path}, error={e}")
                failed += 1
                continue

            try:
                already_present = await ws.exists(new_path)
            except Exception as e:
                logger.debug(f"旧引擎迁移: 存在性检查失败: old={old_path}, new={new_path}, error={e}")
                failed += 1
                continue

            if not already_present:
                try:
                    new_doc = await ws.write(new_path, doc.content)
                    # 保留旧文档的元数据到新文档上
                    if doc.metadata is not None:
                        try:
                            await ws.update_metadata(new_doc.id, doc.metadata)
                        except Exception as e:
                            logger.debug(
                                f"旧引擎迁移: 元数据复制失败: old={old_path}, new={new_path}, error={e}"
                            )
                except Exception as e:
                    logger.debug(f"旧引擎迁移: 写入失败: old={old_path}, new={new_path}, error={e}")
                    failed += 1
                    continue

            try:
                await ws.delete(old_path)
            except Exception as e:
                logger.debug(f"旧引擎迁移: 删除失败: old={old_path}, error={e}")
                failed += 1
                continue

            migrated += 1

        logger.debug(f"旧引擎迁移: 完成 (migrated={migrated}, failed={failed})")

    async def cleanup_terminal_state(self, min_age: timedelta) -> int:
        """从内存缓存中驱逐终端（Done/Failed）线程。
        完整的线程数据（消息、事件、步骤）始终保留在磁盘上 — LLM 输出永远不会被删除。
        此方法仅从内存映射中移除旧的终端线程以保持 RAM 有界
        """
        cleaned = 0
        now = datetime.now(timezone.utc)

        # 1. 从内存映射中驱逐终端线程（磁盘文件保留）
        terminal = []
        for t in self.threads.values():
            if t.state in (ThreadState.Done, ThreadState.Failed, ThreadState.Completed):
                at = t.completed_at or t.updated_at
                if at is not None and (now - at) > min_age:
                    terminal.append(t)

        for thread in terminal:
            # 写入紧凑的存档摘要（用于人类可读的浏览）
            slug = slugify(thread.goal, str(thread.id))
            archive_path = f"{THREAD_ARCHIVE_PREFIX}/{slug}.json"
            summary = compact_thread_summary(thread)
            await self.persist_json(archive_path, summary)

            # 仅从内存映射中驱逐 — 磁盘文件永远不会被删除
            self.threads.pop(thread.id, None)
            self.events.pop(thread.id, None)
            self.steps.pop(thread.id, None)
            cleaned += 1

        # 2. 从内存中清理已撤销/过期的租约
        dead_leases = [lid for lid, l in self.leases.items() if l.revoked or not l.is_valid()]
        for lid in dead_leases:
            self.leases.pop(lid, None)
            cleaned += 1

        if cleaned > 0:
            logger.debug(
                f"从内存中驱逐了终端状态（磁盘保留）: "
                f"threads_evicted={len(terminal)}, leases_cleaned={len(dead_leases)}"
            )

        return cleaned

    async def generate_engine_readme(self) -> None:
        """生成带有当前引擎状态摘要的 `.system/engine/README.md`"""
        def count_by_type(dt):
            return sum(1 for d in self.docs.values() if d.doc_type == dt)

        active_threads = sum(
            1 for t in self.threads.values()
            if t.state not in (ThreadState.Done, ThreadState.Failed)
        )
        active_leases = sum(1 for l in self.leases.values() if l.is_valid())

        orch_versions = sum(
            1 for d in self.docs.values()
            if d.title == ORCHESTRATOR_MAIN_TITLE and ORCHESTRATOR_CODE_TAG in d.tags
        )

        readme = f"# Engine State\n\n最后更新: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n"

        readme += "## 知识 (`.system/engine/knowledge/`)\n\n"
        readme += f"- **{count_by_type(DocType.Lesson)} 个经验教训** — 学到的规则\n"
        readme += f"- **{count_by_type(DocType.Skill)} 个技能** — 提取的过程\n"
        readme += f"- **{count_by_type(DocType.Summary)} 个摘要** — 线程完成记录\n"
        readme += f"- **{count_by_type(DocType.Spec)} 个规范** — 规格说明\n"
        readme += f"- **{count_by_type(DocType.Issue)} 个问题** — 已知问题\n"

        readme += f"\n## 编排器 (`.system/engine/orchestrator/`)\n\n- 存储了 {orch_versions} 个版本\n"

        readme += "\n## 任务 (`.system/engine/projects/<project>/missions/`)\n\n"
        for m in self.missions.values():
            goal_preview = truncate_for_readme(m.goal, 80)
            readme += f"- **{m.name}** ({m.status}) — {goal_preview}\n"

        readme += f"\n## 运行时 (`.system/engine/runtime/`)\n\n"
        readme += f"- {active_threads} 个活跃线程\n"
        readme += f"- {active_leases} 个活跃租约\n"

        await self.persist_text(".system/engine/README.md", readme)

    # ── 内部辅助方法 ─────────────────────────────────────

    async def load_knowledge_docs(self, ws: Any) -> None:
        """加载知识文档"""
        search_prefixes = [KNOWLEDGE_PREFIX, ORCHESTRATOR_PREFIX]

        for prefix in search_prefixes:
            for entry in await self.file_entries(ws, prefix, [".md", ".json", ".py"]):
                try:
                    doc = await ws.read(entry.path)
                except Exception as e:
                    logger.debug(f"读取引擎文档失败: path={entry.path}, error={e}")
                    continue

                parsed = deserialize_knowledge_doc(doc.content)
                if parsed is None:
                    try:
                        parsed = MemoryDoc.from_dict(json.loads(doc.content))
                    except Exception:
                        parsed = synthesize_orchestrator_doc_from_py(entry.path, doc.content)

                if parsed is not None:
                    self.doc_paths[parsed.id] = entry.path
                    self.docs[parsed.id] = parsed
                else:
                    logger.debug(f"跳过引擎中的非文档文件: path={entry.path}")

    async def load_map(self, ws: Any, directory: str, on_value: Callable) -> None:
        """从目录加载 JSON 文件并应用回调"""
        for entry in await self.file_entries(ws, directory, [".json"]):
            try:
                doc = await ws.read(entry.path)
                value = json.loads(doc.content)
                await on_value(value)
            except Exception as e:
                logger.debug(f"解析引擎状态失败: path={entry.path}, error={e}")

    async def file_entries(self, ws: Any, directory: str, extensions: List[str]) -> List[Any]:
        """列出目录下的文件，递归一级到子目录"""
        try:
            top = await ws.list(directory)
        except Exception:
            return []

        files = []
        for entry in top:
            if entry.is_directory:
                try:
                    children = await ws.list(entry.path)
                    for child in children:
                        if not child.is_directory and any(child.path.endswith(ext) for ext in extensions):
                            files.append(child)
                except Exception:
                    pass
            elif any(entry.path.endswith(ext) for ext in extensions):
                files.append(entry)
        return files

    async def persist_json(self, path: str, value: Any) -> None:
        """将值持久化为 JSON 到工作区"""
        if self.workspace is None:
            return
        try:
            json_str = json.dumps(value, ensure_ascii=False, indent=2, default=str)
            await self.workspace.write(path, json_str)
        except Exception as e:
            logger.debug(f"持久化引擎状态失败: path={path}, error={e}")

    async def persist_text(self, path: str, content: str) -> None:
        """将文本内容持久化到工作区"""
        if self.workspace is None:
            return
        try:
            await self.workspace.write(path, content)
        except Exception as e:
            logger.debug(f"持久化引擎文本失败: path={path}, error={e}")

    async def load_projects_from_workspace(self, ws: Any) -> None:
        """从面向用户的工作区中的 `projects/<slug>/.project.json` 加载项目"""
        try:
            entries = await ws.list(PROJECTS_ROOT)
        except Exception:
            return

        for entry in entries:
            if not entry.is_directory:
                continue
            raw_slug = entry.name()
            meta_path = f"{entry.path}/{PROJECT_METADATA_FILENAME}"
            try:
                doc = await ws.read(meta_path)
                project = Project.from_dict(json.loads(doc.content))
            except Exception:
                project = synth_bare_project(raw_slug, ws.user_id())
                if project is None:
                    continue

            self.projects[project.id] = project

    async def migrate_legacy_project_jsons(self, ws: Any) -> None:
        """一次性迁移：将仍然存在于旧引擎内部路径的项目 JSON 迁移到面向用户的布局中"""
        try:
            project_dirs = await ws.list(PROJECTS_PREFIX)
        except Exception:
            return

        for entry in project_dirs:
            if not entry.is_directory:
                continue
            legacy_path = f"{entry.path}/project.json"
            try:
                doc = await ws.read(legacy_path)
            except Exception:
                continue

            try:
                project = Project.from_dict(json.loads(doc.content))
            except Exception as e:
                broken_path = f"{entry.path}/project.broken.json"
                logger.warning(f"旧项目元数据无法解析: {e} — 移动到 {broken_path}: path={legacy_path}")
                try:
                    await ws.write(broken_path, doc.content)
                except Exception as we:
                    logger.warning(f"写入 {broken_path} 失败: {we}")
                    continue
                try:
                    await ws.delete(legacy_path)
                except Exception as de:
                    logger.warning(f"移除旧项目路径失败 {legacy_path}: {de}")
                continue

            new_path = project_path(project.name)
            # 如果用户可能已编辑的新元数据文件，不要覆盖
            try:
                await ws.read(new_path)
                await ws.delete(legacy_path)
                continue
            except Exception:
                pass

            try:
                await ws.write(new_path, doc.content)
            except Exception as e:
                logger.warning(f"迁移项目元数据失败: legacy={legacy_path}, new={new_path}, error={e}")
                continue

            try:
                await ws.delete(legacy_path)
            except Exception as e:
                logger.warning(f"移除旧项目路径失败 {legacy_path}: {e}")

    async def load_missions_from_projects(self, ws: Any) -> None:
        """从每个项目目录中加载任务"""
        try:
            project_dirs = await ws.list(PROJECTS_PREFIX)
        except Exception:
            return

        for proj_entry in project_dirs:
            if not proj_entry.is_directory:
                continue
            missions_dir = f"{proj_entry.path}/missions"
            try:
                mission_dirs = await ws.list(missions_dir)
            except Exception:
                continue

            for mission_entry in mission_dirs:
                if not mission_entry.is_directory:
                    continue
                mission_file = f"{mission_entry.path}/mission.json"
                try:
                    doc = await ws.read(mission_file)
                    mission = Mission.from_dict(json.loads(doc.content))
                    self.missions[mission.id] = mission
                except Exception as e:
                    logger.debug(f"解析任务失败: path={mission_file}, error={e}")

    async def backfill_archived_threads(self, ws: Any) -> None:
        """回填被任务引用但尚未在内存映射中的线程"""
        # 收集被任务引用但在线程映射中缺失的线程 ID
        missing = []
        for m in self.missions.values():
            for tid in m.history_thread_ids:
                if tid not in self.threads:
                    missing.append(tid)

        if not missing:
            return

        backfilled = 0

        # 第一次遍历：尝试从数据库中的活跃路径加载完整线程
        still_missing = []
        for tid in missing:
            try:
                doc = await ws.read(thread_path(tid))
                thread = Thread.from_dict(json.loads(doc.content))
                self.threads[thread.id] = thread
                backfilled += 1
            except Exception:
                still_missing.append(str(tid))

        # 第二次遍历：对于旧删除的线程回退到存档摘要
        if still_missing:
            missing_set = set(still_missing)
            try:
                archive_entries = await ws.list(THREAD_ARCHIVE_PREFIX)
                for entry in archive_entries:
                    if entry.is_directory:
                        continue
                    try:
                        doc = await ws.read(entry.path)
                        summary = ThreadArchiveSummary.from_dict(json.loads(doc.content))
                        if summary.thread_id in missing_set:
                            thread = thread_from_archive(summary)
                            if thread is not None:
                                self.threads[thread.id] = thread
                                backfilled += 1
                    except Exception:
                        continue
            except Exception:
                pass

        if backfilled > 0:
            logger.debug(f"从数据库回填了 {backfilled} 个任务线程")

    async def project_slug(self, project_id: Any) -> str:
        """获取引擎内部的任务路径 slug"""
        if project_id in self.projects:
            p = self.projects[project_id]
            return slugify(p.name, str(p.id)[:8])
        short = str(project_id)[:8]
        return f"unknown--{short}"

    async def delete_workspace_file(self, path: str) -> None:
        """删除工作区文件"""
        if self.workspace is None:
            return
        try:
            await self.workspace.delete(path)
        except Exception as e:
            logger.debug(f"删除引擎文件失败: path={path}, error={e}")

    async def persist_doc(self, doc: Any) -> None:
        """将 MemoryDoc 持久化到工作区"""
        new_path = doc_workspace_path(doc)

        # 如果文档之前存在于不同的路径，删除旧的
        old_path = self.doc_paths.get(doc.id)
        if old_path is not None and old_path != new_path:
            await self.delete_workspace_file(old_path)

        # 根据路径选择序列化格式
        if is_orchestrator_code_path(new_path):
            content = doc.content
        elif new_path.lower().endswith(".md"):
            content = serialize_knowledge_doc(doc)
        else:
            try:
                content = json.dumps(doc.to_dict(), ensure_ascii=False, indent=2, default=str)
            except Exception as e:
                logger.debug(f"序列化文档失败: path={new_path}, error={e}")
                return

        await self.persist_text(new_path, content)
        self.doc_paths[doc.id] = new_path

    # ── Store 接口实现 ────────────────────────────────────

    async def save_thread(self, thread: Any) -> None:
        """保存线程"""
        self.threads[thread.id] = thread.clone() if hasattr(thread, 'clone') else thread
        await self.persist_json(thread_path(thread.id), thread)

    async def load_thread(self, id: Any) -> Optional[Any]:
        """加载线程"""
        if id in self.threads:
            t = self.threads[id]
            return t.clone() if hasattr(t, 'clone') else t

        if self.workspace is not None:
            try:
                doc = await self.workspace.read(thread_path(id))
                thread = Thread.from_dict(json.loads(doc.content))
                self.threads[thread.id] = thread
                return thread
            except Exception:
                pass
        return None

    async def list_threads(self, project_id: Any, user_id: str) -> List[Any]:
        """列出项目中的线程"""
        return [
            t for t in self.threads.values()
            if t.project_id == project_id and t.user_id == user_id
        ]

    async def update_thread_state(self, id: Any, state: Any) -> None:
        """更新线程状态"""
        if id in self.threads:
            self.threads[id].state = state
            await self.persist_json(thread_path(id), self.threads[id])

    async def save_step(self, step: Any) -> None:
        """保存步骤"""
        if step.thread_id not in self.steps:
            self.steps[step.thread_id] = []
        thread_steps = self.steps[step.thread_id]

        found = False
        for i, existing in enumerate(thread_steps):
            if existing.id == step.id:
                thread_steps[i] = step
                found = True
                break
        if not found:
            thread_steps.append(step)
            thread_steps.sort(key=lambda s: s.sequence)

        await self.persist_json(step_path(step.thread_id), thread_steps)

    async def load_steps(self, thread_id: Any) -> List[Any]:
        """加载线程的步骤"""
        if thread_id in self.steps:
            return list(self.steps[thread_id])

        if self.workspace is not None:
            try:
                doc = await self.workspace.read(step_path(thread_id))
                steps_data = json.loads(doc.content)
                steps = [Step.from_dict(s) for s in steps_data]
                self.steps[thread_id] = steps
                return list(steps)
            except Exception:
                pass
        return []

    async def append_events(self, events: List[Any]) -> None:
        """追加事件"""
        grouped = {}
        for event in events:
            if event.thread_id not in grouped:
                grouped[event.thread_id] = []
            grouped[event.thread_id].append(event)

        for thread_id, new_events in grouped.items():
            if thread_id not in self.events:
                self.events[thread_id] = []
            thread_events = self.events[thread_id]

            for event in new_events:
                if not any(e.id == event.id for e in thread_events):
                    thread_events.append(event)

            thread_events.sort(key=lambda e: e.timestamp)
            await self.persist_json(event_path(thread_id), thread_events)

    async def load_events(self, thread_id: Any) -> List[Any]:
        """加载线程的事件"""
        if thread_id in self.events:
            return list(self.events[thread_id])

        if self.workspace is not None:
            try:
                doc = await self.workspace.read(event_path(thread_id))
                events_data = json.loads(doc.content)
                events = [ThreadEvent.from_dict(e) for e in events_data]
                self.events[thread_id] = events
                return list(events)
            except Exception:
                pass
        return []

    async def save_project(self, project: Any) -> None:
        """保存项目"""
        self.projects[project.id] = project
        await self.persist_json(project_path(project.name), project)

    async def load_project(self, id: Any) -> Optional[Any]:
        """加载项目"""
        return self.projects.get(id)

    async def list_projects(self, user_id: str) -> List[Any]:
        """列出用户的项目"""
        return [p for p in self.projects.values() if p.user_id == user_id]

    async def list_all_projects(self) -> List[Any]:
        """列出所有项目"""
        return list(self.projects.values())

    async def save_conversation(self, conversation: Any) -> None:
        """保存对话"""
        self.conversations[conversation.id] = conversation
        await self.persist_json(conversation_path(conversation.id), conversation)

    async def load_conversation(self, id: Any) -> Optional[Any]:
        """加载对话"""
        return self.conversations.get(id)

    async def list_conversations(self, user_id: str) -> List[Any]:
        """列出用户的对话"""
        return [c for c in self.conversations.values() if c.user_id == user_id]

    async def save_memory_doc(self, doc: Any) -> None:
        """保存记忆文档"""
        # 深度防御：即使调用者绕过了工具级别的检查，也门控编排器/提示写入
        if is_protected_orchestrator_doc(doc):
            trusted = is_trusted_internal_write_active()

            if not trusted and doc.title == ORCHESTRATOR_FAILURES_TITLE:
                raise EngineError(
                    f"AccessDenied: 用户 '{doc.user_id}' 不能访问编排器文档 "
                    f"'{doc.title}'（系统内部跟踪器）"
                )

            if not self_modify_enabled():
                if not trusted:
                    raise EngineError(
                        f"AccessDenied: 用户 '{doc.user_id}' 不能访问编排器文档 "
                        f"'{doc.title}'（自我修改已禁用）"
                    )
            elif not trusted:
                validate_orchestrator_content(doc)

        stamped = doc.clone() if hasattr(doc, 'clone') else doc

        # 规范化物理全局文档的 project_id
        if (is_globally_shared(stamped)
            and is_shared_owner(stamped.user_id)
            and not _is_nil_uuid(stamped.project_id)):
            stamped.project_id = ProjectId(uuid.UUID(int=0))

        # 在所有受保护文档上标记内容哈希用于审计追踪
        if is_protected_orchestrator_doc(doc):
            hash_val = hashlib.sha256(doc.content.encode('utf-8')).hexdigest()
            if not isinstance(stamped.metadata, dict):
                stamped.metadata = {}
            stamped.metadata["content_hash"] = hash_val

        self.docs[stamped.id] = stamped
        await self.persist_doc(stamped)

    async def load_memory_doc(self, id: Any) -> Optional[Any]:
        """加载记忆文档"""
        return self.docs.get(id)

    async def list_memory_docs(self, project_id: Any, user_id: str) -> List[Any]:
        """列出项目中的记忆文档"""
        return [
            d for d in self.docs.values()
            if d.project_id == project_id and d.user_id == user_id
        ]

    async def list_shared_memory_docs(self, project_id: Any) -> List[Any]:
        """列出对任何项目查询可见的共享文档"""
        out = []
        seen = set()
        for doc in self.docs.values():
            if not is_shared_owner(doc.user_id):
                continue
            if doc.project_id == project_id or (
                _is_nil_uuid(doc.project_id) and is_globally_shared(doc)
            ):
                doc_id = str(doc.id)
                if doc_id not in seen:
                    seen.add(doc_id)
                    out.append(doc)

        out.sort(key=lambda d: str(d.id))
        return out

    async def list_memory_docs_by_owner(self, user_id: str) -> List[Any]:
        """按所有者列出记忆文档"""
        return [d for d in self.docs.values() if d.user_id == user_id]

    async def save_lease(self, lease: Any) -> None:
        """保存租约"""
        self.leases[lease.id] = lease
        await self.persist_json(lease_path(lease.id), lease)

    async def load_active_leases(self, thread_id: Any) -> List[Any]:
        """加载线程的活跃租约"""
        return [
            l for l in self.leases.values()
            if l.thread_id == thread_id and l.is_valid()
        ]

    async def revoke_lease(self, lease_id: Any, reason: str) -> None:
        """撤销租约"""
        if lease_id in self.leases:
            self.leases[lease_id].revoked = True
            await self.persist_json(lease_path(lease_id), self.leases[lease_id])

    async def save_mission(self, mission: Any) -> None:
        """保存任务"""
        proj_slug = await self.project_slug(mission.project_id)
        self.missions[mission.id] = mission
        await self.persist_json(
            mission_path(proj_slug, mission.name, mission.id), mission,
        )

    async def load_mission(self, id: Any) -> Optional[Any]:
        """加载任务"""
        return self.missions.get(id)

    async def list_missions(self, project_id: Any, user_id: str) -> List[Any]:
        """列出项目中的任务"""
        missions = [
            m for m in self.missions.values()
            if m.project_id == project_id and m.user_id == user_id
        ]
        missions.sort(key=lambda m: (m.name, str(m.id)))
        return missions

    async def list_all_threads(self, project_id: Any) -> List[Any]:
        """列出项目中所有线程（无论用户）"""
        return [t for t in self.threads.values() if t.project_id == project_id]

    async def list_all_missions(self, project_id: Any) -> List[Any]:
        """列出项目中所有任务（无论用户）"""
        missions = [m for m in self.missions.values() if m.project_id == project_id]
        missions.sort(key=lambda m: (m.name, str(m.id)))
        return missions

    async def update_mission_status(self, id: Any, status: Any) -> None:
        """更新任务状态"""
        if id in self.missions:
            self.missions[id].status = status
            self.missions[id].updated_at = datetime.now(timezone.utc)
            mission = self.missions[id]
            proj_slug = await self.project_slug(mission.project_id)
            await self.persist_json(
                mission_path(proj_slug, mission.name, id), mission,
            )

    # ── 加载回调 ──────────────────────────────────────────

    async def _on_conversation(self, conversation: Any) -> None:
        self.conversations[conversation.id] = conversation

    async def _on_thread(self, thread: Any) -> None:
        self.threads[thread.id] = thread

    async def _on_steps(self, steps: List[Any]) -> None:
        if steps:
            thread_id = steps[0].thread_id
            self.steps[thread_id] = steps

    async def _on_events(self, events: List[Any]) -> None:
        if events:
            thread_id = events[0].thread_id
            self.events[thread_id] = events

    async def _on_lease(self, lease: Any) -> None:
        self.leases[lease.id] = lease


# ── 辅助函数 ─────────────────────────────────────────────────

def _is_nil_uuid(project_id: ProjectId) -> bool:
    """检查 ProjectId 是否为零 UUID"""
    if hasattr(project_id, 'value'):
        return project_id.value == uuid.UUID(int=0)
    return str(project_id) == str(uuid.UUID(int=0))


def _nil_project_id() -> ProjectId:
    """返回零 UUID 的 ProjectId"""
    return ProjectId(uuid.UUID(int=0))


def thread_path(thread_id: ThreadId) -> str:
    """获取线程的存储路径"""
    return f".system/engine/threads/{thread_id}.json"


def step_path(thread_id: ThreadId) -> str:
    """获取步骤的存储路径"""
    return f".system/engine/steps/{thread_id}.json"


def event_path(thread_id: ThreadId) -> str:
    """获取事件的存储路径"""
    return f".system/engine/events/{thread_id}.json"


def project_path(project_name: str) -> str:
    """获取项目的存储路径"""
    slug = slugify_simple(project_name)
    return f"projects/{slug}/.project.json"


def lease_path(lease_id: LeaseId) -> str:
    """获取租约的存储路径"""
    return f".system/engine/leases/{lease_id}.json"


def conversation_path(conversation_id: ConversationId) -> str:
    """获取对话的存储路径"""
    return f".system/engine/conversations/{conversation_id}.json"


def _mission_path(proj_slug: str, mission_name: str, mission_id: MissionId) -> str:
    """获取任务的存储路径"""
    mission_slug = slugify_simple(mission_name)
    return f".system/engine/projects/{proj_slug}/missions/{mission_slug}/{mission_id}.json"


# ── 路径辅助函数 ─────────────────────────────────────────────

def doc_workspace_path(doc: MemoryDoc) -> str:
    """根据标题和类型将 MemoryDoc 映射到其工作区路径"""
    id_str = str(doc.id.value) if hasattr(doc.id, 'value') else str(doc.id)

    # 编排器代码版本 → .system/engine/orchestrator/v{N}.py
    if doc.title == ORCHESTRATOR_MAIN_TITLE and ORCHESTRATOR_CODE_TAG in doc.tags:
        version = doc.metadata.get("version", 0) if isinstance(doc.metadata, dict) else 0
        return f"{ORCHESTRATOR_PREFIX}/v{version}.py"

    # 编排器故障跟踪器 → .system/engine/orchestrator/failures.json
    if doc.title == ORCHESTRATOR_FAILURES_TITLE:
        return f"{ORCHESTRATOR_PREFIX}/failures.json"

    # 提示覆盖 → .system/engine/orchestrator/codeact-preamble-overlay.md
    if doc.title == PREAMBLE_OVERLAY_TITLE:
        return f"{ORCHESTRATOR_PREFIX}/codeact-preamble-overlay.md"

    # 修复模式数据库 → .system/engine/knowledge/notes/{slug}.md
    if doc.title == FIX_PATTERN_TITLE:
        slug = slugify(doc.title, id_str)
        return f"{KNOWLEDGE_PREFIX}/notes/{slug}.md"

    # 知识文档 → .system/engine/knowledge/{type}/{slug}.md
    type_dir = {
        DocType.Summary: "summaries",
        DocType.Lesson: "lessons",
        DocType.Issue: "issues",
        DocType.Spec: "specs",
        DocType.Note: "notes",
        DocType.Skill: "skills",
        DocType.Plan: "plans",
    }.get(doc.doc_type, "notes")

    slug = slugify(doc.title, id_str)
    return f"{KNOWLEDGE_PREFIX}/{type_dir}/{slug}.md"


def is_orchestrator_code_path(path: str) -> bool:
    """检查 `path` 是否解析为编排器 `.py` 版本文件

    在匹配之前规范化路径，以便点/双斜杠/遍历组件无法绕过检查
    （例如 `engine/./orchestrator/v3.py`、`.system/engine//orchestrator/v3.py`、
    `engine/knowledge/../orchestrator/v3.py`）。
    遍历尝试（`..` 段）被保守地拒绝（返回 `False`）—
    它们不可能是合法的编排器代码路径
    """
    canonical = normalize_path(path)
    if canonical is None:
        return False
    if not canonical.endswith(".py"):
        return False
    return (canonical.startswith(f"{ORCHESTRATOR_PREFIX}/")
            or canonical.startswith(f"{LEGACY_ORCHESTRATOR_PREFIX}/"))


def normalize_path(path: str) -> Optional[str]:
    """去除 `.` 段并折叠 `//`，在 `..` 遍历时返回 `None`

    镜像 `tools::builtin::memory` 中的 `normalize_workspace_path` —
    在此处保持本地化，以便存储适配器不依赖于工具层
    """
    segments = []
    for seg in path.split('/'):
        if seg == '' or seg == '.':
            continue
        if seg == '..':
            return None
        segments.append(seg)
    return '/'.join(segments)


def synthesize_orchestrator_doc_from_py(path: str, content: str) -> Optional[MemoryDoc]:
    """从磁盘上找到的原始 `.py` 编排器文件合成 MemoryDoc

    编排器版本持久化为 `.system/engine/orchestrator/v{N}.py`（原始 Python）。
    重启时，这些需要重新构建为 MemoryDoc，以便 `load_orchestrator_from_docs()` 可以找到它们。
    版本号从文件名中提取

    **项目作用域**：编排器代码是*物理上全局的* — 每个工作区只有一个 `v{N}.py` 存在，
    无论有多少项目共享该工作区。合成的文档使用 `ProjectId::nil()` 作为全局标记，
    并且 `HybridStore::list_shared_memory_docs`（下面覆盖）为任何项目查询提供它们，
    以便执行器的每个项目 `load_orchestrator(project_id)` 在重启后始终能找到它们
    """
    if not is_orchestrator_code_path(path):
        return None

    # 从文件名提取版本：.system/engine/orchestrator/v3.py → 3
    filename = path.rsplit('/', 1)[-1] if '/' in path else path
    if not filename.startswith('v') or not filename.endswith('.py'):
        return None

    try:
        version = int(filename[1:-3])
    except (ValueError, TypeError):
        return None

    now = datetime.now(timezone.utc)
    return MemoryDoc(
        id=DocId(uuid.uuid4()),
        project_id=ProjectId(uuid.UUID(int=0)),  # nil UUID
        user_id=shared_owner_id(),
        doc_type=DocType.Note,
        title=ORCHESTRATOR_MAIN_TITLE,
        content=content,
        source_thread_id=None,
        tags=[ORCHESTRATOR_CODE_TAG],
        metadata={"version": version, "source": "persisted_py"},
        created_at=now,
        updated_at=now,
    )


def is_protected_orchestrator_doc(doc: MemoryDoc) -> bool:
    """检查 MemoryDoc 是否是受保护的编排器或提示覆盖文档"""
    return doc.title.startswith("orchestrator:") or doc.title.startswith("prompt:")


def synth_bare_project(raw_slug: str, user_id: str) -> Optional[Project]:
    """为尚没有 `.project.json` 的 `projects/<raw_slug>/` 目录构建存根 Project。
    通过 `slugify_simple` 规范化 slug，以便裸目录和随后对相同 user+name 的
    `Project::new` 调用折叠为相同的 `ProjectId`。如果规范化后 slug 为空
    （例如名为 `---` 的目录）则返回 `None` — 此类目录无法在不冲突的情况下
    通过工作区路径往返

    `user_id` 是工作区的所有者，因此存根限定为该用户；
    `shared_owner_id` 下的裸项目对其真正所有者不可见，但对其他人全局可见
    """
    slug = slugify_simple(raw_slug)
    if not slug:
        return None

    now = datetime.now(timezone.utc)
    return Project(
        id=ProjectId.from_slug(user_id, slug),
        user_id=user_id,
        name=slug,
        description="",
        goals=[],
        metrics=[],
        metadata={},
        workspace_path=None,
        created_at=now,
        updated_at=now,
    )


def is_globally_shared(doc: MemoryDoc) -> bool:
    """对于物理上全局的文档（无论项目如何只有一个文件）返回 True

    这些文档存放在知名的工作区路径（例如 `.system/engine/orchestrator/v3.py`），
    必须对任何项目的 `list_shared_memory_docs` 查询都可用 —
    参见 `HybridStore` 上的覆盖
    """
    return (doc.title == ORCHESTRATOR_MAIN_TITLE
            or doc.title == ORCHESTRATOR_FAILURES_TITLE
            or doc.title == PREAMBLE_OVERLAY_TITLE)


def validate_orchestrator_content(doc: MemoryDoc) -> None:
    """在持久化之前验证编排器内容

    仅验证 `orchestrator:*` 文档 — 它们包含由 Monty 沙箱执行的 Python 代码。
    `prompt:*` 文档（例如 `prompt:codeact_preamble`）是注入到系统提示中的
    markdown 文本，不是代码 — 将它们作为 Python 验证会拒绝每个提示覆盖。
    如果引擎将来支持基于 Python 的提示覆盖，此函数必须更新以涵盖这些标题

    检查 Python 语法，这样损坏的补丁不会消耗故障预算槽（3 次故障触发自动回滚）。
    语义上危险的模式（`exec(compile(...))`、`__import__('os')`）通过验证，
    因为它们是语法有效的 Python — 所有安全执行在运行时的 Monty 沙箱中进行
    （资源限制、主机函数门控、无文件系统/网络访问）
    """
    if (doc.title.startswith("orchestrator:")
            and doc.title != ORCHESTRATOR_FAILURES_TITLE):
        try:
            validate_python_syntax(doc.content)
        except SyntaxError as e:
            raise EngineError(f"InvalidInput: 编排器补丁 '{doc.title}' 包含无效的 Python: {e}")


def project_slug_for_name(name: str) -> str:
    """用于在磁盘上寻址项目的 slug。纯粹从项目名称派生（无 UUID 后缀），
    以便面向用户的路径 `projects/<slug>/` 是可预测的，并且在项目的 ID 更改时不会变动
    """
    slug = slugify_simple(name)
    return slug if slug else "untitled"


def project_dir(name: str) -> str:
    """面向用户的项目目录。在此路径下写入任何文件是项目存在的声明 —
    引擎存储在 `memory_write` 时自动注册它
    """
    return f"{PROJECTS_ROOT}/{project_slug_for_name(name)}"


def project_path(name: str) -> str:
    """项目的规范元数据文件。通过点前缀隐藏，这样它不会混乱项目的
    `memory_tree` 视图，但仍然位于面向用户的项目目录内，以便模型可以通过
    正常的工作区 API 推理它
    """
    return f"{project_dir(name)}/{PROJECT_METADATA_FILENAME}"


def thread_path(thread_id: ThreadId) -> str:
    """线程文件的路径"""
    tid = str(thread_id.value) if hasattr(thread_id, 'value') else str(thread_id)
    return f"{THREADS_PREFIX}/{tid}.json"


def conversation_path(conversation_id: ConversationId) -> str:
    """对话文件的路径"""
    cid = str(conversation_id.value) if hasattr(conversation_id, 'value') else str(conversation_id)
    return f"{CONVERSATIONS_PREFIX}/{cid}.json"


def step_path(thread_id: ThreadId) -> str:
    """步骤文件的路径"""
    tid = str(thread_id.value) if hasattr(thread_id, 'value') else str(thread_id)
    return f"{STEPS_PREFIX}/{tid}.json"


def event_path(thread_id: ThreadId) -> str:
    """事件文件的路径"""
    tid = str(thread_id.value) if hasattr(thread_id, 'value') else str(thread_id)
    return f"{EVENTS_PREFIX}/{tid}.json"


def lease_path(lease_id: LeaseId) -> str:
    """租约文件的路径"""
    lid = str(lease_id.value) if hasattr(lease_id, 'value') else str(lease_id)
    return f"{LEASES_PREFIX}/{lid}.json"


def mission_dir(project_slug: str, name: str, mission_id: MissionId) -> str:
    """任务目录的路径"""
    mid = str(mission_id.value) if hasattr(mission_id, 'value') else str(mission_id)
    slug = slugify(name, mid)
    return f"{PROJECTS_PREFIX}/{project_slug}/missions/{slug}"


def mission_path(project_slug: str, name: str, mission_id: MissionId) -> str:
    """任务文件的路径"""
    return f"{mission_dir(project_slug, name, mission_id)}/mission.json"


# ── Slugify ─────────────────────────────────────────────────

def slugify(title: str, id: str) -> str:
    """从标题和短 ID 后缀创建人类可读的文件名 slug

    `"Validate tool names before first call"` + `"65c9f5cd-..."` →
    `"validate-tool-names-before-first-call--65c9f5cd"`
    """
    # 将标题转换为小写并替换非字母数字字符
    slug_chars = []
    for c in title.lower():
        if c.isascii() and (c.isalnum() or c == '-'):
            slug_chars.append(c)
        else:
            slug_chars.append('-')

    # 折叠连续的破折号并修剪
    collapsed = []
    prev_dash = False
    for c in slug_chars:
        if c == '-':
            if not prev_dash and collapsed:
                collapsed.append('-')
            prev_dash = True
        else:
            collapsed.append(c)
            prev_dash = False
    collapsed_str = ''.join(collapsed).rstrip('-')

    # 将 slug 截断到 60 个字符，追加 8 字符 ID 后缀。
    # `collapsed_str` 仅包含 ASCII，因为 slugify() 已经替换了非 ASCII 字符，
    # 因此对其进行字节索引切片是安全的
    max_slug = 60
    if len(collapsed_str) > max_slug:
        # 不要在单词中间截断 — 找到限制之前的最后一个破折号
        window = collapsed_str[:max_slug]
        dash_pos = window.rfind('-')
        if dash_pos > 20:
            truncated = collapsed_str[:dash_pos]
        else:
            truncated = collapsed_str[:max_slug]
    else:
        truncated = collapsed_str

    short_id = id[:8] if len(id) >= 8 else id  # UUID 字符串始终是 ASCII
    return f"{truncated}--{short_id}"


# ── Frontmatter 序列化 ──────────────────────────────────────

def yaml_quoted_escape(s: str) -> str:
    """转义字符串以嵌入 YAML 双引号标量中

    YAML 双引号标量要求 `\`、`"` 和控制字符被转义。
    换行符（`\n`、`\r`）、制表符（`\t`）和反斜杠是用户提供的标识符
    （例如 OIDC `sub` 声明）中最常见的违规者
    """
    result = []
    for ch in s:
        if ch == '\\':
            result.append('\\\\')
        elif ch == '"':
            result.append('\\"')
        elif ch == '\n':
            result.append('\\n')
        elif ch == '\r':
            result.append('\\r')
        elif ch == '\t':
            result.append('\\t')
        elif ord(ch) < 0x20:
            result.append(f'\\x{ord(ch):02x}')
        else:
            result.append(ch)
    return ''.join(result)


def serialize_knowledge_doc(doc: MemoryDoc) -> str:
    """将 MemoryDoc 序列化为 YAML frontmatter + markdown 内容"""
    doc_id = str(doc.id.value) if hasattr(doc.id, 'value') else str(doc.id)
    project_id = str(doc.project_id.value) if hasattr(doc.project_id, 'value') else str(doc.project_id)

    lines = ['---']
    lines.append(f'id: "{doc_id}"')
    lines.append(f'project_id: "{project_id}"')
    lines.append(f'user_id: "{yaml_quoted_escape(doc.user_id)}"')
    lines.append(f'doc_type: "{doc.doc_type.value if hasattr(doc.doc_type, "value") else str(doc.doc_type)}"')
    lines.append(f'title: "{yaml_quoted_escape(doc.title)}"')

    if doc.tags:
        tags_str = ', '.join(f'"{yaml_quoted_escape(t)}"' for t in doc.tags)
        lines.append(f'tags: [{tags_str}]')

    if hasattr(doc, 'source_thread_id') and doc.source_thread_id is not None:
        stid = str(doc.source_thread_id.value) if hasattr(doc.source_thread_id, 'value') else str(doc.source_thread_id)
        lines.append(f'source_thread: "{stid}"')

    created_str = doc.created_at.isoformat() if hasattr(doc.created_at, 'isoformat') else str(doc.created_at)
    updated_str = doc.updated_at.isoformat() if hasattr(doc.updated_at, 'isoformat') else str(doc.updated_at)
    lines.append(f'created: "{created_str}"')
    lines.append(f'updated: "{updated_str}"')

    if doc.metadata:
        meta_str = json.dumps(doc.metadata, ensure_ascii=False)
        lines.append(f'metadata: {meta_str}')

    lines.append('---')
    lines.append('')
    lines.append(doc.content)

    return '\n'.join(lines)


def deserialize_knowledge_doc(content: str) -> Optional[MemoryDoc]:
    """将 frontmatter+markdown 字符串反序列化回 MemoryDoc"""
    content = content.lstrip()
    if not content.startswith('---'):
        return None

    # 找到闭合的 ---
    after_first = content[3:]
    nl_pos = after_first.find('\n')
    if nl_pos == -1:
        return None

    after_first_line = after_first[nl_pos + 1:]
    yaml_end = after_first_line.find('\n---')
    if yaml_end == -1:
        return None

    yaml_str = after_first_line[:yaml_end]
    body_start = yaml_end + 4  # 跳过 \n---
    body = after_first_line[body_start:].lstrip('\n')

    # 解析 YAML frontmatter
    try:
        frontmatter = yaml.safe_load(yaml_str)
    except yaml.YAMLError:
        return None

    if not isinstance(frontmatter, dict):
        return None

    id_str = frontmatter.get('id')
    if not id_str:
        return None

    try:
        doc_id = uuid.UUID(id_str)
    except (ValueError, AttributeError):
        return None

    title = frontmatter.get('title', '')

    doc_type_str = frontmatter.get('doc_type', 'Note')
    doc_type_map = {
        'Summary': DocType.Summary,
        'Lesson': DocType.Lesson,
        'Issue': DocType.Issue,
        'Spec': DocType.Spec,
        'Skill': DocType.Skill,
        'Plan': DocType.Plan,
    }
    doc_type = doc_type_map.get(doc_type_str, DocType.Note)

    tags = frontmatter.get('tags', [])

    source_thread_id = None
    source_thread_str = frontmatter.get('source_thread')
    if source_thread_str:
        try:
            source_thread_id = ThreadId(uuid.UUID(source_thread_str))
        except (ValueError, AttributeError):
            pass

    created_str = frontmatter.get('created')
    created_at = _parse_datetime(created_str) if created_str else datetime.now(timezone.utc)

    updated_str = frontmatter.get('updated')
    updated_at = _parse_datetime(updated_str) if updated_str else datetime.now(timezone.utc)

    metadata = frontmatter.get('metadata', {})

    project_id_str = frontmatter.get('project_id')
    if project_id_str:
        try:
            project_id = ProjectId(uuid.UUID(project_id_str))
        except (ValueError, AttributeError):
            logger.debug(
                f"知识文档缺少 project_id frontmatter；加载为 nil — "
                f"这表明文档是在持久化 project_id/user_id 之前序列化的，"
                f"它将对项目作用域的查询不可见"
            )
            project_id = ProjectId(uuid.UUID(int=0))
    else:
        project_id = ProjectId(uuid.UUID(int=0))

    user_id = frontmatter.get('user_id', 'legacy')

    return MemoryDoc(
        id=DocId(doc_id),
        project_id=project_id,
        user_id=user_id,
        doc_type=doc_type,
        title=title,
        content=body,
        source_thread_id=source_thread_id,
        tags=tags,
        metadata=metadata,
        created_at=created_at,
        updated_at=updated_at,
    )


def _parse_datetime(s: str) -> datetime:
    """解析 RFC 3339 日期时间字符串"""
    try:
        # Python 3.7+ 支持 fromisoformat，但 RFC 3339 可能有 'Z' 后缀
        s = s.replace('Z', '+00:00')
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


# ── 线程归档 ─────────────────────────────────────────────────

@dataclass
class ThreadArchiveSummary:
    """已完成线程的紧凑摘要，用于归档"""
    thread_id: str
    goal: str
    state: str
    created_at: str
    completed_at: Optional[str] = None
    step_count: int = 0
    total_tokens: int = 0
    outcome_preview: str = ""
    # `default=0.0` 让在此字段存在之前编写的摘要继续反序列化为零而不是失败
    total_cost_usd: float = 0.0
    # 短侧边栏标签。`default=None` 保持与在此字段存在之前编写的归档文件的向后兼容性
    title: Optional[str] = None


def compact_thread_summary(thread: Thread) -> ThreadArchiveSummary:
    """生成线程的紧凑归档摘要"""
    # 提取最后一条助手消息作为结果预览
    outcome = ""
    for m in reversed(thread.messages):
        if m.role == MessageRole.Assistant:
            outcome = truncate_for_readme(m.content, 200)
            break

    tid = str(thread.id.value) if hasattr(thread.id, 'value') else str(thread.id)
    return ThreadArchiveSummary(
        thread_id=tid,
        goal=truncate_for_readme(thread.goal, 200),
        state=str(thread.state),
        created_at=thread.created_at.isoformat() if hasattr(thread.created_at, 'isoformat') else str(thread.created_at),
        completed_at=thread.completed_at.isoformat() if thread.completed_at and hasattr(thread.completed_at,
                                                                                        'isoformat') else None,
        step_count=thread.step_count,
        total_tokens=thread.total_tokens_used,
        outcome_preview=outcome,
        total_cost_usd=thread.total_cost_usd,
        title=thread.title,
    )


def thread_from_archive(summary: dict) -> Optional[Thread]:
    """从归档摘要重建最小 Thread（用于任务详情页面）"""
    if not isinstance(summary, dict):
        summary = summary.__dict__ if hasattr(summary, '__dict__') else {}

    thread_id_str = summary.get('thread_id', '')
    try:
        tid = uuid.UUID(thread_id_str)
    except (ValueError, AttributeError):
        return None

    created_at = _parse_datetime(summary.get('created_at', ''))
    completed_at_str = summary.get('completed_at')
    completed_at = _parse_datetime(completed_at_str) if completed_at_str else None

    state_str = summary.get('state', 'Done')
    state_map = {
        'Done': ThreadState.Done,
        'Failed': ThreadState.Failed,
        'Completed': ThreadState.Completed,
    }
    state = state_map.get(state_str, ThreadState.Done)

    updated_at = completed_at or created_at

    return Thread(
        id=ThreadId(tid),
        goal=summary.get('goal', ''),
        title=summary.get('title'),
        thread_type=ThreadType.Mission,
        state=state,
        project_id=ProjectId(uuid.UUID(int=0)),  # nil UUID
        user_id='default',
        parent_thread_id=None,
        config=ThreadConfig(),
        messages=[],
        internal_messages=[],
        events=[],
        capability_leases=[],
        metadata={},
        created_at=created_at,
        updated_at=updated_at,
        completed_at=completed_at,
        step_count=summary.get('step_count', 0),
        total_tokens_used=summary.get('total_tokens', 0),
        total_cost_usd=summary.get('total_cost_usd', 0.0),
    )


def truncate_for_readme(s: str, max_chars: int) -> str:
    """为 README 截断字符串"""
    trimmed = ' '.join(s.replace('\n', ' ').split())
    if len(trimmed) <= max_chars:
        return trimmed
    return trimmed[:max_chars] + '...'
