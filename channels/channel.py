import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Tuple, Optional, List, Dict, AsyncIterator
from uuid import UUID, uuid4
from abc import ABC, abstractmethod
from enum import Enum
import uuid
from ironclaw_common import (
    ExtensionName,
    ExternalThreadId,
    ExternalThreadIdError,
    JobResultStatus
)
from ironclaw_common.attachment import IncomingAttachment
from agent.submission import Submission


logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class IncomingMessage:
    """
    传入消息。
    """
    # 消息的唯一标识符。
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # 频道名称。
    channel: str
    # 用户标识符。
    user_id: str
    # 发送者标识符。
    sender_id: str = ""
    # 可选用户名称。
    user_name: Optional[str] = None
    # 消息内容。
    content: str
    # 可选的结构化提交负载。
    structured_submission: Optional[Submission] = None
    # 可选的线程 ID。
    thread_id: Optional[ExternalThreadId] = None
    # 此对话的稳定频道/聊天/线程范围。
    conversation_scope_id: Optional[str] = None
    # 接收时间。
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # 元数据，默认包含 user_id。
    metadata: dict = field(default_factory=dict)
    # 可选的客户端时区。
    timezone: Optional[str] = None
    # 附件列表。
    attachments: List[IncomingAttachment] = field(default_factory=list)
    # 是否为内部消息。
    is_internal: bool = False
    # 是否为代理广播回显。
    is_agent_broadcast: bool = False
    # 可选的任务触发 ID。
    triggering_mission_id: Optional[str] = None


    def __post_init__(self):
        """
        在 dataclass 初始化后确保 metadata 中包含正确的 user_id。

        对应 Rust 中 new() 的默认 metadata 携带 {"user_id": &user_id} 的逻辑：
        如果 metadata 中 user_id 为空或与 self.user_id 不一致，则同步。
        """
        if isinstance(self.metadata, dict):
            # 字符串类型的 user_id 始终被 self.user_id 覆盖
            existing = self.metadata.get("user_id")
            if existing is None or isinstance(existing, str):
                self.metadata["user_id"] = self.user_id


    def with_agent_broadcast(self) -> "IncomingMessage":
        """
        将此消息标记为代理广播回显。将代理自身出站文本重新作为入站事件发出的
        频道适配器必须调用此方法，以便任务的 OnEvent 触发跳过它。

        对应 Rust:
        pub fn with_agent_broadcast(mut self) -> Self
        """
        self.is_agent_broadcast = True
        return self

    def with_triggering_mission(self, mission_id: str) -> "IncomingMessage":
        """
        将此消息标记为由任务触发产生。用于跨不同任务的链式递归保护。

        对应 Rust:
        pub fn with_triggering_mission(mut self, mission_id: impl Into<String>) -> Self
        """
        self.triggering_mission_id = mission_id
        return self

    def with_thread(self, thread_id: str) -> "IncomingMessage":
        """
        设置线程 ID（可信路径 —— 无需验证）。

        接受原始字符串 —— 该值通过 ExternalThreadId::from_trusted 包装。
        这是一个**可信路径便利方法**：假设调用者从内部/类型化来源
        （数据库行、内部频道适配器、上游频道已接受的平台标识符）获取了字符串。
        conversation_scope_id 影子镜像原始字符串。

        对于不受信任的输入（HTTP webhooks、中继回调、任何原始调用者提供的负载），
        优先使用 try_with_thread，它通过 ExternalThreadId::new 验证，
        并在空/含 NUL/过大字符串时返回错误。

        对应 Rust:
        pub fn with_thread(mut self, thread_id: impl Into<String>) -> Self
        """
        self.conversation_scope_id = thread_id
        self.thread_id = ExternalThreadId.from_trusted(thread_id)
        return self

    def try_with_thread(self, thread_id: str) -> None:
        """
        从不受信任的输入设置线程 ID，验证原始字符串。

        在系统边界使用此变体 —— HTTP webhooks、中继回调负载，
        或任何字符串来自外部调用者的路径。对空、过大或含 NUL 的值
        返回 ExternalThreadIdError；调用者通常记录并丢弃 thread_id
        （或返回 400）。对于内部可信路径（类型化数据库行、已验证的
        频道适配器状态），使用 with_thread。

        接受 &mut self 以便调用者在验证失败时保留消息的所有权。

        对应 Rust:
        pub fn try_with_thread(&mut self, thread_id: impl AsRef<str>) -> Result<(), ExternalThreadIdError>
        """
        typed = ExternalThreadId.new(thread_id)  # 可能抛出 ExternalThreadIdError
        self.conversation_scope_id = typed.as_str()
        self.thread_id = typed

    def with_external_thread(self, thread_id: Any) -> "IncomingMessage":
        """
        从已类型化的 ExternalThreadId 设置线程 ID。

        对应 Rust:
        pub fn with_external_thread(mut self, thread_id: ExternalThreadId) -> Self
        """
        self.conversation_scope_id = thread_id.as_str()
        self.thread_id = thread_id
        return self

    def with_sender_id(self, sender_id: str) -> "IncomingMessage":
        """
        设置频道特定的发送者/参与者标识符。

        对应 Rust:
        pub fn with_sender_id(mut self, sender_id: impl Into<String>) -> Self
        """
        self.sender_id = sender_id
        return self

    def with_conversation_scope(self, scope_id: str) -> "IncomingMessage":
        """
        设置此消息的对话范围。

        对应 Rust:
        pub fn with_conversation_scope(mut self, scope_id: impl Into<String>) -> Self
        """
        self.conversation_scope_id = scope_id
        return self

    def with_metadata(self, metadata: dict) -> "IncomingMessage":
        """
        设置元数据。

        字符串类型的 metadata.user_id 始终被 self.user_id 覆盖 ——
        调用者提供的字符串值被丢弃。这使得 SSE/WS 接收者范围无法从频道元数据伪造：
        WASM 扩展发出的包含 {"user_id":"victim"} 的 JSON 无法将后续的
        ToolStarted/ToolResult 事件路由到另一个租户的流中。

        非字符串的 user_id 值（例如 Telegram 的 i64 聊天用户 ID）保持不变：
        它们无法被利用，因为 SSE 路由层（as_str()）将其视为缺失，
        在多租户模式下故障关闭。覆盖它们会损坏频道私有的元数据。

        非对象输入（Null、数组、标量）被替换为携带 self.user_id 的新对象。
        缺失的 user_id 键被插入为 self.user_id。

        对应 Rust:
        pub fn with_metadata(mut self, metadata: serde_json::Value) -> Self
        """
        if not isinstance(metadata, dict):
            metadata = {}

        # 检查是否需要设置 user_id
        should_set = True
        if "user_id" in metadata:
            existing = metadata["user_id"]
            if isinstance(existing, str):
                should_set = True  # 覆盖字符串值
            else:
                should_set = False  # 保留非字符串值

        if should_set:
            metadata["user_id"] = self.user_id

        self.metadata = metadata
        return self

    def with_user_name(self, name: str) -> "IncomingMessage":
        """
        设置用户名称。

        对应 Rust:
        pub fn with_user_name(mut self, name: impl Into<String>) -> Self
        """
        self.user_name = name
        return self

    def with_structured_submission(self, submission: Any) -> "IncomingMessage":
        """
        附加结构化提交侧带负载。

        对应 Rust:
        pub fn with_structured_submission(mut self, submission: Submission) -> Self
        """
        self.structured_submission = submission
        return self

    def with_timezone(self, tz: str) -> "IncomingMessage":
        """
        设置客户端时区。

        对应 Rust:
        pub fn with_timezone(mut self, tz: impl Into<String>) -> Self
        """
        self.timezone = tz
        return self

    def with_attachments(self, attachments: List[Any]) -> "IncomingMessage":
        """
        设置附件。

        对应 Rust:
        pub fn with_attachments(mut self, attachments: Vec<IncomingAttachment>) -> Self
        """
        self.attachments = attachments
        return self

    def into_internal(self) -> "IncomingMessage":
        """
        将此消息标记为内部消息（绕过用户输入管道）。

        对应 Rust:
        pub(crate) fn into_internal(mut self) -> Self
        """
        self.is_internal = True
        return self


    @property
    def conversation_scope(self) -> Optional[str]:
        """
        有效的对话范围，对遗留调用者回退到 thread_id。
        """
        if self.conversation_scope_id is not None:
            return self.conversation_scope_id
        if self.thread_id is not None:
            return self.thread_id.as_str()
        return None

    @property
    def routing_target(self) -> Optional[str]:
        """
        当前频道上主动回复的最佳路由目标。

        对应 Rust:
        pub fn routing_target(&self) -> Option<String>
        """
        # 首先尝试从元数据中提取路由目标
        target = routing_target_from_metadata(self.metadata)
        if target is not None:
            return target
        # 否则回退到 sender_id
        if self.sender_id:
            return self.sender_id
        return None


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
    content: str
    thread_id: ExternalThreadId | None = None
    attachments: list[str] = field(default_factory=list)
    inline_attachments: list[OutgoingAttachment] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    # --- 工厂方法 ---
    @classmethod
    def text(cls, content: str) -> "OutgoingResponse":
        """创建一个纯文本响应，其他字段用默认值"""
        return cls(content=content)

    # --- Builder 方法 (返回 Self 实现链式调用) ---
    def in_thread(self, thread_id: str) -> "OutgoingResponse":
        """设置线程 ID（信任的输入）"""
        self.thread_id = ExternalThreadId.from_trusted(thread_id)
        return self

    def try_in_thread(self, thread_id: str) -> None:
        """设置线程 ID（不信任的输入，会校验）"""
        self.thread_id = ExternalThreadId.new(thread_id)  # 失败会抛异常
        # 如果你想要 Result 风格，可以不返回 Self 而是 raise 异常

    def in_external_thread(self, thread_id: ExternalThreadId) -> "OutgoingResponse":
        """直接用已构造好的 ExternalThreadId"""
        self.thread_id = thread_id
        return self

    def with_attachments(self, paths: list[str]) -> "OutgoingResponse":
        self.attachments = paths
        return self

    def with_inline_attachments(self, attachments: list[OutgoingAttachment]) -> "OutgoingResponse":
        self.inline_attachments = attachments
        return self


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
