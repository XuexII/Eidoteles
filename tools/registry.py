from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict
import logging
from tools.builtin.message import MessageTool
from tools.tool import Tool

logger = logging.getLogger(__name__)


# 注册可用工具
class ToolRegistry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tools: Dict[str, Tool] = Field(default_factory=dict)
    # 追踪哪些名称已注册为内置名称（受保护，不可被覆盖）。
    builtin_names: set[str] = Field(default_factory=set)
    # 由 WASM 工具填充、供 HTTP 工具使用的共享凭证注册表。
    credential_registry: Optional[SharedCredentialRegistry] = None
    # 用于凭证注入的密钥存储（与 HTTP 工具共享）。
    secrets_store: Optional[SecretsStore] = None
    # 用于内置工具调用的共享速率限制器。
    # rate_limiter: RateLimiter
    # 用于按轮次设置上下文的消息工具的引用。
    message_tool: Optional[MessageTool] = None


    async def set_message_tool_context(self, channel: Optional[str], target: Optional[str]):
        """
        为消息工具设置默认频道和目标。
        在每次智能体轮次之前，使用当前对话的上下文调用此方法。
        """
        pass
