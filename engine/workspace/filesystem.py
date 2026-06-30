# [`FilesystemBackend`] —— 基于真实文件系统的透传 [`MountBackend`]，根目录为主机路径。
#
# 当未配置沙盒时，桥接器使用它作为 `/project/` 挂载的默认后端。
# 路径验证会拒绝绝对路径和任何组件级转义（`..`）；在词法验证之后，解析后的路径会尽可能被规范化，并再次与根目录进行校验，以防止基于符号链接的转义。
#
# `read`、`write` 和 `list` 已完全实现。在本版本中，`patch` 和 `shell` 返回 [`MountError::Unsupported`]；
# 当发生这种情况时，桥接器拦截器会回退到主机工具，因此调用方不会丢失功能。
# 这两个方法将在容器化后端上线且需要对称覆盖时实现。

from .mount import DirEntry, EntryKind, MountBackend, MountError, ShellOutput
import os
import asyncio
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import List, Optional, Dict, Union
import logging

logger = logging.getLogger(__name__)


# ── 挂载错误 ─────────────────────────────────────────────────

class MountError(Exception):
    """挂载操作错误"""

    @classmethod
    def invalid_path(cls, path: Union[str, Path], reason: str) -> "MountError":
        """路径无效"""
        return cls(f"无效路径 '{path}': {reason}")

    @classmethod
    def io(cls, path: Union[str, Path], error: Exception) -> "MountError":
        """IO 错误"""
        return cls(f"路径 '{path}' 的 IO 错误: {error}")


class MountErrorUnsupported(MountError):
    """不支持的操作"""

    def __init__(self, operation: str):
        super().__init__(f"不支持的操作: {operation}")


# ── 目录条目 ─────────────────────────────────────────────────

class EntryKind:
    """目录条目类型"""
    File = "File"
    Directory = "Directory"
    Symlink = "Symlink"


@dataclass
class DirEntry:
    """目录条目"""
    path: Path
    kind: str
    size: Optional[int] = None


@dataclass
class ShellOutput:
    """Shell 命令输出"""
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


# ── 挂载后端抽象 ─────────────────────────────────────────────

class MountBackend:
    """挂载后端抽象接口"""

    async def read(self, rel_path: Path) -> bytes:
        """按相对路径读取文件"""
        raise NotImplementedError

    async def write(self, rel_path: Path, content: bytes) -> None:
        """按相对路径写入文件"""
        raise NotImplementedError

    async def list(self, rel_path: Path, depth: int) -> List[DirEntry]:
        """列出目录内容"""
        raise NotImplementedError

    async def patch(
            self,
            rel_path: Path,
            old_string: str,
            new_string: str,
            replace_all: bool,
    ) -> None:
        """在文件中替换字符串"""
        raise MountErrorUnsupported(
            "FilesystemBackend::patch (推迟到后续阶段；桥接回退到主机工具)"
        )

    async def shell(
            self,
            command: str,
            env: Dict[str, str],
            cwd: Optional[Path] = None,
    ) -> ShellOutput:
        """执行 Shell 命令"""
        raise MountErrorUnsupported(
            "FilesystemBackend::shell (推迟到后续阶段；桥接回退到主机工具)"
        )


# ── 文件系统后端 ─────────────────────────────────────────────

@dataclass
class FilesystemBackend(MountBackend):
    """以真实主机路径为根的直通挂载后端"""
    root: Path

    def safe_join(self, rel_path: Path) -> Path:
        """词法验证 `rel_path` 并将其与根路径连接

        拒绝绝对路径和任何包含 `..` 或根组件的路径 —
        既作为给调用者的清晰错误，也作为抵御目录遍历攻击的第一层防御
        """
        if rel_path.is_absolute():
            raise MountError.invalid_path(
                rel_path,
                "不允许绝对路径；请传递相对于挂载点的路径",
            )

        for part in rel_path.parts:
            if part == "..":
                raise MountError.invalid_path(
                    rel_path,
                    "不允许 `..` 组件",
                )
            # 检查根目录标记（在 Windows 上可能是 "C:\\" 等形式）
            if os.path.isabs(str(part)):
                raise MountError.invalid_path(
                    rel_path,
                    "不允许根或前缀组件",
                )

        return self.root / rel_path

    def canonicalize_under_root(self, joined: Path) -> Path:
        """在解析路径后，通过规范化任何现有祖先并验证其
        保持在 `self.root` 下来防御符号链接逃逸

        当规范化成功时返回规范形式，否则返回词法连接。
        尚不存在的文件（写入路径）无法被规范化 —
        对于这些，我们向上走到最近的现有祖先，规范化*那个*，
        然后重新附加缺失的尾部
        """
        # 当根目录尚不存在时（项目目录未创建），完全跳过规范化检查。
        # 词法安全性已由 `safe_join` 保证（无 `..`，无绝对路径）。
        # 没有此保护，现有前缀遍历将向上爬到一个真实祖先
        # （例如 `/tmp`），而针对不存在根的 `starts_with` 检查将始终失败，
        # 阻止会创建目录的写入
        try:
            canonical_root = self.root.resolve()
        except OSError:
            return Path(joined)

        # 找到 `joined` 的最长现有前缀
        existing_prefix = Path(joined)
        tail = []
        while not existing_prefix.exists() and existing_prefix != existing_prefix.parent:
            tail.append(existing_prefix.name)
            existing_prefix = existing_prefix.parent

        try:
            canonical_prefix = existing_prefix.resolve()
        except OSError:
            canonical_prefix = Path(existing_prefix)

        if not str(canonical_prefix).startswith(str(canonical_root)):
            raise MountError.invalid_path(
                joined,
                "解析路径通过符号链接逃逸了挂载根目录",
            )

        # 重新组装完整路径
        result = canonical_prefix
        for component in reversed(tail):
            result = result / component

        # TOCTOU 缓解：如果重新组装的路径现在存在于磁盘上
        # （例如在遍历和此处之间另一个线程创建了它，或者符号链接被交换到尾部），
        # 重新规范化并再次验证包含以关闭竞态窗口
        if result.exists():
            try:
                final_canonical = result.resolve()
                if not str(final_canonical).startswith(str(canonical_root)):
                    raise MountError.invalid_path(
                        joined,
                        "解析路径通过符号链接逃逸了挂载根目录（组装后检查）",
                    )
                return final_canonical
            except OSError:
                pass

        return result

    def resolve(self, rel_path: Path) -> Path:
        """结合 [`safe_join`] 和 [`canonicalize_under_root`] 进行完整解析，
        防御词法和符号链接逃逸
        """
        joined = self.safe_join(rel_path)
        return self.canonicalize_under_root(joined)

    async def read(self, rel_path: Path) -> bytes:
        """按相对路径读取文件"""
        full = self.resolve(rel_path)
        try:
            # 在线程池中运行以避免阻塞事件循环
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: full.read_bytes())
        except OSError as e:
            raise MountError.io(full, e)

    async def write(self, rel_path: Path, content: bytes) -> None:
        """按相对路径写入文件"""
        full = self.resolve(rel_path)
        parent = full.parent
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: parent.mkdir(parents=True, exist_ok=True))
            await loop.run_in_executor(None, lambda: full.write_bytes(content))
        except OSError as e:
            raise MountError.io(full, e)

    async def list(self, rel_path: Path, depth: int) -> List[DirEntry]:
        """列出目录内容"""
        full = self.resolve(rel_path)
        out = []
        await _list_dir_recursive(full, full, depth, out)
        return out

    async def patch(
            self,
            rel_path: Path,
            old_string: str,
            new_string: str,
            replace_all: bool,
    ) -> None:
        """在文件中替换字符串（不支持）"""
        raise MountErrorUnsupported(
            "FilesystemBackend::patch (推迟到后续阶段；桥接回退到主机工具)"
        )

    async def shell(
            self,
            command: str,
            env: Dict[str, str],
            cwd: Optional[Path] = None,
    ) -> ShellOutput:
        """执行 Shell 命令（不支持）"""
        raise MountErrorUnsupported(
            "FilesystemBackend::shell (推迟到后续阶段；桥接回退到主机工具)"
        )


async def _list_dir_recursive(
        dir: Path,
        root: Path,
        depth: int,
        out: List[DirEntry],
) -> None:
    """遍历目录并发出 [`DirEntry`] 值，深度最多 `depth` 层

    `depth = 0` 仅列出 `dir` 的直接子项
    """
    stack = [(dir, 0)]

    while stack:
        current, current_depth = stack.pop()

        try:
            loop = asyncio.get_running_loop()
            entries = await loop.run_in_executor(None, lambda: list(current.iterdir()))
        except OSError as e:
            raise MountError.io(current, e)

        for path in entries:
            # 使用 lstat（symlink_metadata）以便检测符号链接而不是静默跟踪到其目标
            try:
                metadata = await loop.run_in_executor(None, lambda: path.lstat())
            except OSError as e:
                raise MountError.io(path, e)

            # 确定条目类型
            if os.path.islink(str(path)):
                kind = EntryKind.Symlink
            elif path.is_dir():
                kind = EntryKind.Directory
            else:
                kind = EntryKind.File

            # 计算相对路径
            try:
                rel = path.relative_to(root)
            except ValueError:
                rel = path

            size = metadata.st_size if kind == EntryKind.File else None

            out.append(DirEntry(path=rel, kind=kind, size=size))

            # 仅递归进入真实目录（非符号链接）。对于真实目录，
            # 在遍历之前验证它们解析在根目录内 — 绑定挂载或硬链接可能逃逸
            if (kind == EntryKind.Directory
                    and current_depth < depth
                    and path.is_dir()
                    and not os.path.islink(str(path))):
                try:
                    canonical = path.resolve()
                    if str(canonical).startswith(str(root)):
                        stack.append((path, current_depth + 1))
                except OSError:
                    pass