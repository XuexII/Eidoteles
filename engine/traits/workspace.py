# 工作区文档读取器。
#
# 由任务运行时用于将 `context_paths` 文件加载到已触发任务的元提示中。
# 宿主（主 `ironclaw` crate）通过现有的 `Workspace` API 实现此功能。
#
# 刻意保持小巧：仅提供足够的接口，按相对路径读取单个文档。
# 引擎不会向工作区写入内容。

from abc import ABC, abstractmethod


class WorkspaceReader(ABC):
    """按路径读取工作区文档。实现必须是租户安全的：
    它们包装的工作区是属于任务所有者的工作区
    """

    @abstractmethod
    async def read_doc(self, path: str) -> str:
        """按相对工作区路径读取文档。将文档正文作为字符串返回。
        当文件不存在或无法解码时，实现应返回错误而不是引发异常
        """
        ...