# 线程消息——引擎自身的消息类型。
#
# 比主 crate 的 `ChatMessage` 更简单。桥接适配器负责 `ThreadMessage` 和 `ChatMessage` 之间的转换。

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from .provenance import Provenance
from .step import ActionCall


class MessageRole(str, Enum):
    """消息参与者的角色。"""
    System = "system"
    User = "user"
    Assistant = "assistant"
    # 来自能力操作的结果（替代 "Tool" 角色）
    ActionResult = "action_result"


@dataclass
class ThreadMessage:
    """线程对话历史中的消息。"""
    role: MessageRole
    content: str
    provenance: Provenance
    # 对于 ActionResult 消息：此消息响应的调用 ID
    action_call_id: Optional[str] = None
    # 对于 ActionResult 消息：操作名称
    action_name: Optional[str] = None
    # 对于 Assistant 消息：LLM 想要执行的操作
    action_calls: Optional[List[ActionCall]] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def system(cls, content: str) -> "ThreadMessage":
        """创建系统消息。"""
        return cls(
            role=MessageRole.System,
            content=content,
            provenance=ProvenanceSystem(),
            timestamp=datetime.now(timezone.utc),
        )

    @classmethod
    def user(cls, content: str) -> "ThreadMessage":
        """创建用户消息。"""
        return cls(
            role=MessageRole.User,
            content=content,
            provenance=ProvenanceUser(),
            timestamp=datetime.now(timezone.utc),
        )

    @classmethod
    def assistant(cls, content: str) -> "ThreadMessage":
        """创建助手文本消息。"""
        return cls(
            role=MessageRole.Assistant,
            content=content,
            provenance=ProvenanceLlmGenerated(),
            timestamp=datetime.now(timezone.utc),
        )

    @classmethod
    def assistant_with_actions(
            cls,
            content: Optional[str],
            calls: List[ActionCall],
    ) -> "ThreadMessage":
        """创建带有操作调用的助手消息。"""
        return cls(
            role=MessageRole.Assistant,
            content=content or "",
            provenance=ProvenanceLlmGenerated(),
            action_calls=calls,
            timestamp=datetime.now(timezone.utc),
        )

    @classmethod
    def action_result(
            cls,
            call_id: str,
            action_name: str,
            content: str,
    ) -> "ThreadMessage":
        """创建操作结果消息。"""
        return cls(
            role=MessageRole.ActionResult,
            content=content,
            provenance=ProvenanceToolOutput(action_name=action_name),
            action_call_id=call_id,
            action_name=action_name,
            timestamp=datetime.now(timezone.utc),
        )
