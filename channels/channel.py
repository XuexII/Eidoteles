import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Tuple, Optional, List, Dict
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


# 收到的消息中的文件或媒体附件
class IncomingAttachment(BaseModel):
    pass



class IncomingMessage(BaseModel):
    """从外部渠道收到的消息。"""
    # 唯一消息 ID
    id: UUID = Field(default_factory=uuid4)
    # Channel this message came from.
    channel: str
    # 此交互的存储/持久化范围
    # 对于支持所有者的渠道，当配置的所有者发言时，此为稳定的实例所有者 ID；否则，它可以是访客/发送者范围内的标识符，以保持隔离性。
    user_id: str
    #此 IronClaw 部署的稳定实例所有者范围
    owner_id: str
    # 特定渠道的发送者/参与者标识符
    sender_id: str
    # 可选的显示名称
    user_name: Optional[str] = None
    # Message content.
    content: str
    # 用于线程对话的线程/会话 ID
    thread_id: Optional[str] = None
    # 此会话的稳定渠道/聊天/线程范围。
    conversation_scope_id: Optional[str] = None
    # When the message was received.
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Channel-specific metadata.
    metadata: Any = None
    # 可选的 IANA 时区字符串（如 "America/New_York"）
    timezone: Optional[str] = None
    # 收到的消息中的文件或媒体文件
    attachments: List[IncomingAttachment] = Field(default_factory=list)
    # 内部专用标志：消息由进程内部生成（如任务监控），必须绕过正常的用户输入管道。该字段无法通过元数据设置，因此外部渠道无法伪造。
    is_internal: bool = Field(default=False, frozen=True)

    @classmethod
    def new(cls, channel: str, user_id: str, content: str):
        return cls(
            channel=channel,
            owner_id=user_id,
            sender_id=user_id,
            user_id=user_id,
            content=content
        )

    def with_thread(self, thread_id: str):
        """
        设置线程id
        :param thread_id:
        :return:
        """
        self.conversation_scope_id = thread_id
        self.thread_id = thread_id


    def with_owner_id(self, owner_id:str):
        """
        为此消息设置稳定币发行方范围
        :return:
        """
        self.owner_id = owner_id

    def with_sender_id(self, sender_id: str):
        """
        设置特定频道的发送者/行为者标识符。
        """
        self.sender_id = sender_id

    def with_conversation_scope(self, scope_id: str):
        """
        为此消息设置会话范围。
        :param scope_id:
        :return:
        """
        self.conversation_scope_id = scope_id

    def with_metadata(self, metadata: Dict):
        """
        Set metadata.
        """
        self.metadata = metadata

    def with_user_name(self, name: str):
        """
        Set user name.
        :param name:
        :return:
        """
        self.user_name = name

    def with_timezone(self, tz: str):
        """
        设置客户端时区。
        :param tz:
        :return:
        """
        self.timezone = tz

    def with_attachments(self, attachments: List[IncomingAttachment]):
        """
        设置附件。
        :param attachments:
        :return:
        """
        self.attachments = attachments

    def into_internal(self):
        """
        将此消息标记为内部消息（绕过用户输入管道）。
        :return:
        """
        self.is_internal = True

    def conversation_scope(self) -> Optional[str]:
        """
        有效的会话范围，对于旧版调用者则回退至 thread_id。
        :return:
        """

        return self.conversation_scope_id or self.thread_id
    # Best-effort routing target for proactive replies on the current channel.
    def routing_target(self) -> Optional[str]:
        """
        在当前频道上进行主动回复时的尽力路由目标。
        :return:
        """
        routing_target_from_metadata(&self.metadata).or_else(|| {
            if self.sender_id.is_empty() {
                None
            } else {
                Some(self.sender_id.clone())
            }
        })
    }