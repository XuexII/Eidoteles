#! 运行时平台元数据，注入到系统提示中用于自我认知。
# !
# ! 为代理提供关于自身身份和环境的知识，
# ! 使其能够在不依赖训练数据的情况下回答关于自身、能力和配置的问题。

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class PlatformInfo:
    """
    运行时平台元数据。
    """
    # 软件版本（来自 `CARGO_PKG_VERSION`）。
    version: Optional[str] = None,
    # LLM 后端名称（例如 "nearai"、"openai"、"anthropic"）。
    llm_backend: Optional[str] = None,
    # 活跃的模型名称。
    model_name: Optional[str] = None,
    # 数据库后端（例如 "libsql"、"postgres"）。
    database_backend: Optional[str] = None,
    # 活跃的频道名称（例如 ["telegram", "cli"]）。
    active_channels: Optional[List[str]] = None,
    # 所有者标识符。
    owner_id: Optional[str] = None,
    # 项目仓库 URL。
    repo_url: Optional[str] = None,

    def to_prompt_section(self) -> str:
        """
        格式化为提示部分。如果没有设置其他信息，则仅返回身份行。
        """
        lines = []

        # 身份声明行
        lines.append("You are **IronClaw**, a secure autonomous AI assistant platform.")

        # 版本信息
        if self.version is not None:
            lines.append(f"- Version: {self.version}")

        # 仓库 URL
        if self.repo_url is not None:
            lines.append(f"- Repository: {self.repo_url}")

        # 所有者
        if self.owner_id is not None:
            lines.append(f"- Owner: {self.owner_id}")

        # LLM 后端和模型
        if self.llm_backend is not None:
            model = self.model_name if self.model_name is not None else "default"
            lines.append(f"- LLM: {self.llm_backend} ({model})")

        # 数据库后端
        if self.database_backend is not None:
            lines.append(f"- Database: {self.database_backend}")

        # 活跃频道
        if self.active_channels:
            lines.append(f"- Channels: {', '.join(self.active_channels)}")

        # 如果没有额外信息，返回简洁的身份行
        if len(lines) <= 1:
            return f"\n\n## Platform\n\n{lines[0]}\n"

        # 返回完整的平台部分
        return f"\n\n## Platform\n\n{chr(10).join(lines)}\n"
