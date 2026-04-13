from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Optional
from uuid import UUID
from workspace.document import MemoryChunk, MemoryDocument, WorkspaceEntry, paths
from datetime import datetime
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

class Workspace(BaseModel):
    """
    工作区为智能体提供基于数据库的记忆存储。

    每个工作区限定于一个用户（以及可选的智能体）。
    文档持久化到数据库并建立索引以供搜索。
    同时支持 PostgreSQL（通过 Repository）和 libSQL（通过 Database 特质）。
    """
    user_id: str
    agent_id: Optional[UUID] = None
    # 数据库存储后端。
    storage: WorkspaceStorage
    # 用于语义搜索的embedding
    embeddings: Optional[EmbeddingProvider] = None
    # 应用于所有查询的默认搜索配置。
    search_defaults: SearchConfig

    async def system_prompt_for_context_tz(self, is_group_chat: bool, tz):
        """
        构建系统提示词，可选择性地排除个人记忆。

        当 `is_group_chat` 为 true 时，将排除 MEMORY.md，以防止
        将个人上下文泄露到群聊对话中。
        """
        prompt = await self.system_prompt_for_context_inner(is_group_chat, tz)
        return prompt

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
        doc = await self.read(paths.BOOTSTRAP)

        if doc.content:
            parts.append(f"## 首次运行引导\n\n\
             工作区中存在 BOOTSTRAP.md 文件。请阅读并遵循其中指示，\
             完成后将其删除。\n\n{doc.content}")

        # 按重要程度加载身份文件
        identity_files = [
            (paths.AGENTS, "## Agent Instructions"),
            (paths.SOUL, "## Core Values"),
            (paths.USER, "## User Context"),
            (paths.IDENTITY, "## Identity")
        ]

        for (path, header) in identity_files:
            doc = await self.read(path)

            if doc.content:
                parts.append(f"{header}\n\n{doc.content}")

        # 工具说明：智能体或用户编写的特定环境指导。
        # TOOLS.md 不控制工具的可用性；它仅作为指导。
        doc = await self.read(paths.TOOLS)

        if doc.content:
            parts.append(f"## Tool Notes\n\n{doc.content}")

        # 仅在直接/主会话中加载 MEMORY.md（群聊中绝不加载）
        if not is_group_chat:
            doc = await self.read(paths.MEMORY)

            if doc.content:
                parts.append(f"## Long-Term Memory\n\n{doc.content}")

        # 添加今日记忆上下文（最近两天的每日日志）
        now = datetime.now()
        today = ""
        yesterday = ""
        for date in [today, yesterday]:
            doc = await self.daily_log(date)
            header = "## Today's Notes" if date == today else "## Yesterday's Notes"
            if doc.content:
                parts.append(f"{header}\n\n{doc.content}")

        return "\n\n---\n\n".join(parts)

    async def write(self, path: Path, content: str):
        """
        写入（创建或更新）文件。

        隐式创建父目录（这些目录在数据库中是虚拟的）。
        写入后会重新索引文档以供搜索。

        # 示例
        ```ignore
         workspace.write("projects/alpha/README.md", "# Alpha 项目\n\n在此处填写描述。").await?;
         ```
        """
        path = normalize_path(path)
        doc = await self.storage.get_or_create_document_by_path(self.user_id, self.agent_id, path)
        await self.storage.update_document(doc.id, content)
        await self.reindex_document(doc.id)
        memory_doc = await self.storage.get_document_by_id(doc.id)
        return memory_doc

