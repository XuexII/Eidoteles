# [`WorkspaceMounts`] —— 每个项目的挂载表注册表。
#
# 将 [`ProjectId`] 映射到 [`ProjectMounts`]（一组 `(prefix, backend)` 对）。
# 新项目条目通过构造时提供的 [`ProjectMountFactory`] 延迟构建，因此桥接器可以接入默认的 `FilesystemBackend` 或 `ContainerizedFilesystemBackend`，而 `WorkspaceMounts` 无需了解任一实现细节。
#
# 解析采用最长前缀匹配。智能体看到的是一个统一的文件系统（`/project/foo.txt`、`/memory/notes.md`、`/home/...`）——注册表会返回拥有每个路径的后端及其对应的相对路径。

from ..types.project import ProjectId
from .mount import MountBackend, MountError

import asyncio
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Optional, Dict, Tuple, List
from abc import ABC, abstractmethod
import logging

logger = logging.getLogger(__name__)


# ── 项目挂载表 ───────────────────────────────────────────────

@dataclass
class ProjectMounts:
    """一个项目的挂载表"""
    # `(prefix, backend)` 对，按前缀长度降序排序，以便最长前缀匹配正确解析。
    # 使用 [`add`] 方法维护排序
    mounts: List[Tuple[str, MountBackend]] = field(default_factory=list)

    def add(self, prefix: str, backend: MountBackend) -> None:
        """在 `prefix` 处添加挂载。前缀必须以 `/` 结尾且为绝对路径
        （以 `/` 开头）；注册表以规范化形式存储它。
        重新注册相同的前缀会替换之前的后端
        """
        p = prefix
        if not p.startswith('/'):
            p = f"/{p}"
        if not p.endswith('/'):
            p = f"{p}/"

        # 移除相同前缀的现有条目
        self.mounts = [(existing, be) for existing, be in self.mounts if existing != p]
        self.mounts.append((p, backend))
        # 最长前缀优先
        self.mounts.sort(key=lambda entry: len(entry[0]), reverse=True)

    def resolve(self, path: str) -> Optional[Tuple[MountBackend, Path]]:
        """根据表解析路径。返回 `(backend, relative_path)`，
        其中 `relative_path` 是 `path` 在匹配前缀之后的部分

        `path` 可以以 `/` 开头或不以 `/` 开头。尾随斜杠对于相对部分保留。
        精确前缀匹配（例如 `/project/`）返回空的相对路径
        """
        normalized = path if path.startswith('/') else f"/{path}"

        for prefix, backend in self.mounts:
            if normalized.startswith(prefix):
                rest = normalized[len(prefix):]
                return (backend, Path(rest) if rest else Path())
            # 也接受没有尾随斜杠的精确匹配：
            # resolve("/project") 对于前缀 "/project/" → 空 rel
            if prefix.endswith('/'):
                without_slash = prefix[:-1]
                if normalized == without_slash:
                    return (backend, Path())

        return None

    def __len__(self) -> int:
        """此表中的挂载数量（用于诊断）"""
        return len(self.mounts)

    def is_empty(self) -> bool:
        """此挂载表是否为空"""
        return len(self.mounts) == 0


# ── 项目挂载工厂 ─────────────────────────────────────────────

class ProjectMountFactory(ABC):
    """在首次访问时为项目构建新的 [`ProjectMounts`]

    由桥接实现：在默认模式下，返回在 `/project/` 处注册的
    `FilesystemBackend(~/.ironclaw/projects/<id>/)`。
    当配置了沙箱容器时，改为返回 `ContainerizedFilesystemBackend`
    """

    @abstractmethod
    async def build(self, project_id: ProjectId) -> ProjectMounts:
        """为 `project_id` 构建挂载表。每个项目最多调用一次 —
        结果由 [`WorkspaceMounts`] 缓存
        """
        ...


# ── 工作区挂载注册表 ─────────────────────────────────────────

@dataclass
class WorkspaceMounts:
    """按项目划分的挂载表注册表

    持有缓存的 `Dict[ProjectId, ProjectMounts]` 和一个按需构建新条目的工厂。
    可通过引用廉价克隆
    """
    factory: ProjectMountFactory
    _by_project: Dict[ProjectId, ProjectMounts] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def resolve(
            self,
            project_id: ProjectId,
            path: str,
    ) -> Optional[Tuple[MountBackend, Path]]:
        """为给定项目解析 `path`，在首次访问时惰性构建项目的挂载表。
        如果表中没有挂载拥有该路径，返回 `None`
        （这是桥接拦截器回退到直接主机执行的信号）
        """
        # 快速路径：检查缓存
        async with self._lock:
            if project_id in self._by_project:
                return self._by_project[project_id].resolve(path)

            # 慢速路径：构建挂载表
            mounts = await self.factory.build(project_id)
            self._by_project[project_id] = mounts
            return mounts.resolve(path)

    async def invalidate(self, project_id: ProjectId) -> None:
        """丢弃项目的缓存挂载表。在项目被删除或其容器被重置时调用，
        以便下次访问时重建
        """
        async with self._lock:
            self._by_project.pop(project_id, None)