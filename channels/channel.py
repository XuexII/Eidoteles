import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Tuple, Optional, List
from uuid import UUID

from pydantic import BaseModel


# 收到的消息中的文件或媒体附件
class IncomingAttachment(BaseModel):
    pass



class IncomingMessage(BaseModel):
    """从外部渠道收到的消息。"""
    # 唯一消息 ID
    id: UUID
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
    user_name: Optional[str]
    # Message content.
    content: str
    # 用于线程对话的线程/会话 ID
    thread_id: Optional[str]
    # 此会话的稳定渠道/聊天/线程范围。
    conversation_scope_id: Optional[str]
    # When the message was received.
    received_at: datetime
    # Channel-specific metadata.
    metadata: Any
    # 可选的 IANA 时区字符串（如 "America/New_York"）
    timezone: Optional[str]
    # 收到的消息中的文件或媒体文件
    attachments: List[IncomingAttachment]
    # 内部专用标志：消息由进程内部生成（如任务监控），必须绕过正常的用户输入管道。该字段无法通过元数据设置，因此外部渠道无法伪造。
    _is_internal: bool