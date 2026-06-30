# [`MountBackend`] 特质及关联的值类型。

import os
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Optional, List, Dict, Union
from abc import ABC, abstractmethod
import logging

logger = logging.getLogger(__name__)


# ── 挂载错误 ─────────────────────────────────────────────────

class MountError(Exception):
    """挂载后端可能返回的错误

    区分"工具失败"（`Tool`）和"沙箱/传输失败"（`Backend`），
    以便引擎可以向编排器以不同方式显示它们。
    `NotFound`、`PermissionDenied` 和 `InvalidPath` 是 LLM 应看到的正常可恢复结果；
    `Backend` 是应在不同层面重试的基础设施故障
    """

    def __init__(self, message: str, error_type: str = "Io",
                 path: Optional[str] = None, reason: Optional[str] = None,
                 operation: Optional[str] = None):
        self.error_type = error_type
        self.path = path
        self.reason = reason
        self.operation = operation
        super().__init__(message)

    @classmethod
    def NotFound(cls, path: Union[str, Path]) -> "MountError":
        """路径在此挂载中不存在"""
        path_str = str(path)
        return cls(
            message=f"未找到: {path_str}",
            error_type="NotFound",
            path=path_str,
        )

    @classmethod
    def InvalidPath(cls, path: Union[str, Path], reason: str) -> "MountError":
        """路径在挂载根目录之外、包含 `..`、是绝对路径，
        或被后端的安全检查拒绝
        """
        path_str = str(path)
        return cls(
            message=f"无效路径: {path_str}: {reason}",
            error_type="InvalidPath",
            path=path_str,
            reason=reason,
        )

    @classmethod
    def PermissionDenied(cls, path: Union[str, Path]) -> "MountError":
        """操作系统级别的权限错误"""
        path_str = str(path)
        return cls(
            message=f"权限被拒绝: {path_str}",
            error_type="PermissionDenied",
            path=path_str,
        )

    @classmethod
    def Io(cls, path: Union[str, Path], reason: str = "") -> "MountError":
        """底层存储的 I/O 错误"""
        path_str = str(path)
        return cls(
            message=f"路径 {path_str} 的 IO 错误: {reason}",
            error_type="Io",
            path=path_str,
            reason=reason,
        )

    @classmethod
    def io(cls, path: Union[str, Path], err: Exception) -> "MountError":
        """从 OS 错误和路径构建 MountError

        根据错误类型自动选择合适的错误变体
        """
        path_str = str(path)
        if isinstance(err, FileNotFoundError):
            return cls.NotFound(path_str)
        elif isinstance(err, PermissionError):
            return cls.PermissionDenied(path_str)
        else:
            return cls.Io(path_str, str(err))

    @classmethod
    def Tool(cls, reason: str) -> "MountError":
        """工具执行返回非零状态或其他工具级别的错误。
        LLM 应看到此错误并可以自我纠正
        """
        return cls(
            message=f"工具错误: {reason}",
            error_type="Tool",
            reason=reason,
        )

    @classmethod
    def Backend(cls, reason: str) -> "MountError":
        """后端传输/沙箱基础设施故障（容器宕机、守护进程崩溃、IPC 中断）。
        编排器不应将此作为工具错误直接显示给 LLM — 它是需要在不同层面处理的基础设施问题
        """
        return cls(
            message=f"后端错误: {reason}",
            error_type="Backend",
            reason=reason,
        )

    @classmethod
    def Unsupported(cls, operation: str) -> "MountError":
        """此后端在当前版本中不支持的操作"""
        return cls(
            message=f"不支持的操作: {operation}",
            error_type="Unsupported",
            operation=operation,
        )


# ── 目录条目类型 ─────────────────────────────────────────────

class EntryKind:
    """目录条目的类型"""
    File = "File"
    Directory = "Directory"
    Symlink = "Symlink"


@dataclass
class DirEntry:
    """从 [`MountBackend::list`] 返回的一个条目"""
    # 条目路径，相对于挂载根目录
    path: Path
    # 这是文件、目录还是符号链接
    kind: str
    # 大小（字节）（仅对文件有意义）
    size: Optional[int] = None


@dataclass
class ShellOutput:
    """通过 [`MountBackend::shell`] 执行 Shell 的输出"""
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


# ── 挂载后端接口 ─────────────────────────────────────────────

class MountBackend(ABC):
    """工作区挂载表中一个挂载的存储后端

    传递给后端方法的所有路径都是**相对于挂载根目录的**。
    后端必须拒绝通过 `..`、绝对路径或符号链接解析到根目录之外
    来逃逸根目录的路径。此接口不指定拒绝机制；
    具体实现（如 [`FilesystemBackend`]）实现纵深防御的路径验证
    """

    @abstractmethod
    async def read(self, rel_path: Path) -> bytes:
        """读取 `rel_path` 的内容并返回其字节"""
        ...

    @abstractmethod
    async def write(self, rel_path: Path, content: bytes) -> None:
        """将 `content` 写入 `rel_path`，根据需要创建父目录。
        如果文件已存在则覆盖
        """
        ...

    @abstractmethod
    async def list(self, rel_path: Path, depth: int) -> List[DirEntry]:
        """列出 `rel_path` 下最多 `depth` 层的条目。`depth = 0` 仅列出直接条目。
        `rel_path` 必须是目录
        """
        ...

    @abstractmethod
    async def patch(
            self,
            rel_path: Path,
            old_string: str,
            new_string: str,
            replace_all: bool,
    ) -> None:
        """对 `rel_path` 应用搜索/替换编辑

        在 `rel_path` 处的文件中查找 `old_string` 并用 `new_string` 替换它。
        当 `replace_all` 为 true 时，每个出现都被替换；
        否则仅替换第一个匹配项。镜像 `ApplyPatchTool` 的契约

        如果补丁应用尚未接线，实现可能返回 [`MountError::Unsupported`] —
        在这种情况下，桥接拦截器将回退到主机工具
        """
        ...

    @abstractmethod
    async def shell(
            self,
            command: str,
            env: Dict[str, str],
            cwd: Optional[Path] = None,
    ) -> ShellOutput:
        """在 Shell 中执行 `command`。`cwd`（如果存在）相对于挂载根目录

        如果 Shell 执行尚未接线，实现可能返回 [`MountError::Unsupported`]
        """
        ...