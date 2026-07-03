# `WorkspaceReader` 适配器。
#
# 在宿主的现有 `Workspace` API 之上实现引擎的 `WorkspaceReader` 特质。
# 任务运行时使用此适配器将 `context_paths` 文件加载到已触发任务的元提示中。

from dataclasses import dataclass
from workspace import Workspace


@dataclass
class WorkspaceReaderAdapter:
    """将主机 `Workspace` 适配为引擎的 `WorkspaceReader` 接口

    包装的工作区是属于任务所有者的工作区 — 调用者负责在构造适配器时
    确保租户正确性（通常通过从 `agent.workspace()` 传递每用户工作区句柄）
    """
    workspace: Workspace  # Workspace

    async def read_doc(self, path: str) -> str:
        """按路径读取工作区文档。返回文档正文作为字符串。

        Args:
            path: 工作区内的相对路径

        Returns:
            文档的字符串内容

        Raises:
            EngineError: 当文档不存在或无法读取时
        """
        try:
            doc = await self.workspace.read(path)
            return doc.content
        except Exception as error:
            raise EngineError(f"Store: 工作区读取失败 {path}: {error}")