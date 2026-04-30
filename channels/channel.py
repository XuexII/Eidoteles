import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Tuple, Optional, List, Dict, AsyncIterator
from uuid import UUID, uuid4
from abc import ABC, abstractmethod
from enum import Enum

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class IncomingMessage(BaseModel):
    """从外部渠道收到的消息。"""
    # 唯一消息 ID
    id: str = Field(default_factory=uuid4)
    # Channel this message came from.
    channel: str
    # 此交互的存储/持久化范围
    # 对于支持所有者的渠道，当配置的所有者发言时，此为稳定的实例所有者 ID；否则，它可以是访客/发送者范围内的标识符，以保持隔离性。
    user_id: str
    # 此 IronClaw 部署的稳定实例所有者范围
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
    received_at: datetime = Field(default_factory=lambda: datetime.now())
    # Channel-specific metadata.
    metadata: Any = None
    # 可选的 IANA 时区字符串（如 "America/New_York"）
    timezone: Optional[str] = None
    # 收到的消息中的文件或媒体文件
    # attachments: List[IncomingAttachment] = Field(default_factory=list)
    # 内部专用标志：消息由进程内部生成（如任务监控），必须绕过正常的用户输入管道。该字段无法通过元数据设置，因此外部渠道无法伪造。
    is_internal: bool = Field(default=False, frozen=True)

    def conversation_scope(self) -> Optional[str]:
        """
        有效的会话范围，对于旧版调用者则回退至 thread_id。
        :return:
        """

        return self.conversation_scope_id or self.thread_id

    def routing_target(self) -> Optional[str]:
        """
        在当前频道上进行主动回复时的尽力路由目标。
        :return:
        """
        target = routing_target_from_metadata(self.metadata)
        if target:
            return target

        return self.sender_id if self.sender_id else None


def routing_target_from_metadata(metadata: Dict) -> Optional[str]:
    """
    从消息元数据中提取特定频道的主动路由目标。
    """

    if signal_target := metadata.get("signal_target", None):
        if isinstance(signal_target, str):
            return signal_target
        if isinstance(signal_target, (int, float)):
            return str(signal_target)
    elif chat_id := metadata.get("chat_id", None):
        if isinstance(chat_id, str):
            return chat_id
        if isinstance(chat_id, (int, float)):
            return str(chat_id)
    elif target := metadata.get("target", None):
        if isinstance(target, str):
            return target
        if isinstance(target, (int, float)):
            return str(target)

    return None

# 异步迭代器
MessageStream = AsyncIterator[IncomingMessage]

@dataclass
class OutgoingResponse:
    """
    返回给channel的结果
    """
    content: str
    # 回复时使用的线程 ID。
    thread_id: Optional[str]
    # 附加文件路径
    attachments: List[str] = field(default_factory=list)
    # 响应的通道特定元数据。
    metadata: Any = None

class StatusUpdate(Enum):
    # 智能体正在思考/处理信息。
    Thinking = "1"

class Channel(ABC):
    """消息通道特质（trait）

    通道从外部源接收消息并将其转换为统一格式。
    它们还负责将响应发送回去。
    """

    @abstractmethod
    def name(self) -> str:
        """获取通道名称（例如 "cli"、"slack"、"telegram"、"http"）。"""
        pass

    @abstractmethod
    async def start(self) -> MessageStream:
        """开始监听消息。

        返回一个传入消息的流。通道应在内部处理重连和错误恢复。
        """
        pass

    @abstractmethod
    async def respond(
            self,
            msg: IncomingMessage,
            response: OutgoingResponse,
    ) -> None:
        """将响应发送回用户。

        响应在原始消息的上下文中发送（相同的通道，以及适用时相同的线程）。

        抛出:
            ChannelError: 如果发送失败
        """
        pass

    async def send_status(
            self,
            status: StatusUpdate,
            metadata: Dict[str, Any],
    ) -> None:
        """发送状态更新（思考、工具执行等）。

        metadata 包含通道特定的路由信息（例如 Telegram 的 chat_id），
        用于将状态传递到正确的目的地。

        默认实现不执行任何操作（用于不支持状态的通道）。
        """
        pass

    async def broadcast(
            self,
            user_id: str,
            response: OutgoingResponse,
    ) -> None:
        """发送主动消息，无需事先的传入消息。

        用于警报、心跳通知以及其他代理发起的通信。
        user_id 帮助在通道内定位特定用户。

        默认实现不执行任何操作（用于不支持广播的通道）。
        """
        pass

    @abstractmethod
    async def health_check(self) -> None:
        """检查通道是否健康。

        抛出:
            ChannelError: 如果通道不健康
        """
        pass

    def conversation_context(self, metadata: Dict[str, Any]) -> Dict[str, str]:
        """从消息元数据获取对话上下文以用于系统提示。

        返回键值对，例如 {"sender": "姓名", "sender_uuid": "...", "group": "群名"}，
        帮助 LLM 理解它在与谁对话。

        默认实现返回空字典。
        """
        return {}

    async def shutdown(self) -> None:
        """优雅地关闭通道。

        默认实现无操作。
        """
        pass