from dataclasses import dataclass
from typing import List, Dict, Optional
import logging
from workspace.document import paths, MemoryDocument

logger = logging.getLogger(__name__)


# 工作区为智能体提供基于数据库的记忆存储。

# 每个工作区限定于一个用户（以及可选的智能体）。
# 文档持久化到数据库并建立索引以供搜索。
# 同时支持 PostgreSQL（通过 Repository）和 libSQL（通过 Database 特质）。
class Workspace:
    # 用户标识符（来自channel）。
    user_id: str
    # 用于多智能体隔离的可选智能体 ID。
    agent_id: Optional[str] = None
    # 数据库存储后端。
    storage: WorkspaceStorage
    # 用于语义搜索的嵌入提供者。
    embeddings: Optional[EmbeddingProvider]
    # 应用于所有查询的默认搜索配置。
    search_defaults: SearchConfig

    async def read(self, path: str) -> MemoryDocument:
        """
        按路径读取文件。
        如果文档存在则返回，否则返回错误。

        # 示例
        ```ignore
        let doc = workspace.read("context/vision.md").await?;
        println!("{}", doc.content);
        ```
        """

        return await self.storage.get_document_by_path(self.user_id, self.agent_id, path)

    async def system_prompt_for_context_tz(self, is_group_chat: bool, tz):
        """
        构建系统提示词，使用带时区信息的每日日志日期。

        使用给定的时区来确定“今天”和“昨天”，用于注入每日日志。
        """

        return await self.system_prompt_for_context_inner(is_group_chat, tz)

    async def system_prompt_for_context_inner(self, is_group_chat: bool, tz):
        """
        用于构建系统提示词的内部实现。
        """
        parts = []

        # 引导仪式：当 BOOTSTRAP.md 存在时将其注入（仅首次运行时）。
        # 智能体必须完成仪式并随后删除此文件。
        #
        # 注意：BOOTSTRAP.md 特意不设写保护，以便智能体在完成引导后能够删除它。
        # 这意味着提示注入攻击可能会写入该文件，但该文件仅会在下一次会话时被注入
        # （而非当前会话），从而限制了影响范围。
        if doc := await self.read(paths.BOOTSTRAP):
            parts.append(
                "## 首次运行引导\n\n"
                "工作区中存在 BOOTSTRAP.md 文件。请阅读并遵循其中指示，"
                "完成后将其删除。\n\n"
                f"{doc.content}"
            )

        # 按重要程度加载身份文件。
        identity_files = [
            (paths.AGENTS, "## Agent Instructions"),
            (paths.SOUL, "## Core Values"),
            (paths.USER, "## User Context"),
            (paths.IDENTITY, "## Identity"),
        ]
        for (path, header) in identity_files:
            if doc := await self.read(path):
                parts.append(f"{header}\n\n{doc}")

        # 工具说明：代理或用户编写的特定环境指南。
        # TOOLS.md 文件不控制工具的可用性；它仅供参考。
        if doc := await self.read(paths.TOOLS):
            parts.append(f"## Tool Notes\n\n{doc}")

        # 仅在主会话中加载 MEMORY.md（切勿在群聊中加载）
        if (not is_group_chat) and (doc := await self.read(paths.MEMORY)):
            parts.append(f"## Long-Term Memory\n\n{doc}")

        # 添加今天的内存上下文（最近两天的每日日志）

        return "\n\n---\n\n".join(parts)
