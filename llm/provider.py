from abc import ABC, abstractmethod
from enum import Enum
from typing import List, Dict, Optional
from dataclasses import dataclass
import logging
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool-call"
    TOOL_RESPONSE = "tool-response"

    @classmethod
    def roles(cls):
        return [r.value for r in cls]

class ContentPart(str, Enum):
    TEXT = "text"
    ImageUrl = "image_url"

class ToolCall(BaseModel):
    id: str
    name: str
    arguments: Dict

class ChatMessage(BaseModel):
    role: Role
    content: str
    # 多模态内容部分（图像等）。
    # 当非空时，提供商会将内容序列化为一个部分数组（其中 `content` 作为文本部分包含在内），而不是纯字符串。
    content_parts: List[ContentPart] = Field(default_factory=list, exclude_if=lambda v: not v)
    # 如果这是工具结果消息，则为工具调用 ID。
    tool_call_id: Optional[str] = Field(default=None, exclude_if=lambda v: v is None)
    # 工具结果的工具名称。
    name: Optional[str] = Field(default=None, exclude_if=lambda v: v is None)
    # 助手工具调用记录（根据 OpenAI 协议，这些调用必须出现在工具结果消息之前的助手消息上）。
    tool_calls: Optional[List[ToolCall]] = Field(default=None, exclude_if=lambda v: v is None)



class LlmProvider(ABC):

    def model_name(self):
        pass

