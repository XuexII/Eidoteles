"""
ThreadMessage——引擎自身的消息类型

用于构建Thread的对话历史和执行上下文
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from .provenance import (
    Provenance,
    UserProvenance,
    SystemProvenance,
    ToolOutputProvenance,
    LlmGeneratedProvenance

)
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
    # 消息来源（User/System/ToolOutput/LlmGenerated等)
    provenance: Provenance
    # 对于 ActionResult 消息：调用的action id
    action_call_id: Optional[str] = None
    # 对于 ActionResult 消息：调用的action name
    action_name: Optional[str] = None
    # 对于 Assistant 消息：LLM 输出的工具调用
    # TODO: ActionCall 单独实现，作为llm的输出
    action_calls: Optional[List[ActionCall]] = None
    # 时间戳
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def system(cls, content: str) -> "ThreadMessage":
        """创建系统消息。"""
        return cls(
            role=MessageRole.System,
            content=content,
            provenance=SystemProvenance()
        )

    @classmethod
    def user(cls, content: str) -> "ThreadMessage":
        """创建用户消息。"""
        return cls(
            role=MessageRole.User,
            content=content,
            provenance=UserProvenance(),
        )

    @classmethod
    def assistant(cls, content: str) -> "ThreadMessage":
        """创建助手文本消息。"""
        return cls(
            role=MessageRole.Assistant,
            content=content,
            provenance=LlmGeneratedProvenance(),
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
            provenance=LlmGeneratedProvenance(),
            action_calls=calls
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
            provenance=ToolOutputProvenance(action_name=action_name),
            action_call_id=call_id,
            action_name=action_name
        )
