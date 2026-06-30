# 工作区挂载表抽象。
#
# 定义 [`MountBackend`] 特质——针对存储后端执行文件系统和 Shell 操作的统一接口——以及一个小的 [`WorkspaceMounts`] 注册表，用于将智能体面向的路径（如 `/project/foo.txt`）解析到拥有该路径的后端。
#
# 这是 nearai/ironclaw#1894 中提议的统一工作区 VFS 的一个刻意精简的子集。引擎 v2 的每个项目沙盒需要此抽象，以便无论 `/project/` 挂载由主机文件系统（默认）还是由通过 JSON-RPC 分发到每个项目沙盒容器的容器化后端提供服务，相同的智能体面向路径方案都能正常工作。
#
# 此 crate 内置两个后端：
#
# - [`FilesystemBackend`] —— 透传至根目录为主机路径的真实文件系统。在未配置沙盒时由桥接器使用。
# - 桥接器的 `ContainerizedFilesystemBackend`（独立模块，参见 `src/bridge/sandbox/`）—— 通过 JSON-RPC 进入每个项目的容器。位于宿主 crate 中，因为它需要 Docker。
#
# 特质本身保留在引擎 crate 中，以便两个后端——以及 nearai/ironclaw#1894 可能添加的任何未来后端——可以在不依赖宿主基础设施的情况下实现相同的接口。

from .filesystem import FilesystemBackend
from .mount import DirEntry, EntryKind, MountBackend, MountError, ShellOutput
from .registry import ProjectMountFactory, ProjectMounts, WorkspaceMounts