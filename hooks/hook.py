from pydantic import BaseModel, Field, ConfigDict
from abc import ABC, abstractmethod
from pydantic import BaseModel, Field, ConfigDict
from typing import Union, Optional, Dict
from enum import Enum, EnumDict


class HookPoint(Enum):
    """
    钩子触发点枚举。
    对应 Rust 中的 HookPoint。
    """
    # 处理入站用户消息之前。
    BeforeInbound = "beforeInbound"
    # 执行工具调用之前。
    BeforeToolCall = "beforeToolCall"
    # 发送出站响应之前。
    BeforeOutbound = "beforeOutbound"
    # 新会话启动时。
    OnSessionStart = "onSessionStart"
    # 会话结束时（被修剪或过期）。
    OnSessionEnd = "onSessionEnd"
    # 在完成一轮对话前，转换最终响应。
    TransformResponse = "transformResponse"

    def as_str(self) -> str:
        """
        返回人类可读的钩子点标识符。
        对应 Rust 中的 as_str 方法。
        """
        return self.value


class Inbound(BaseModel):
    """
    一条即将被处理的入站用户消息
    """
    user_id: str
    channel: str
    content: str
    thread_id: Optional[str]


class ToolCall(BaseModel):
    """
    一个即将被执行的工具调用。
    """
    tool_name: str
    parameters: Dict
    user_id: str
    # “chat” 表示交互式对话，或是用于自治作业的作业 ID 字符串。
    context: str


class Outbound(BaseModel):
    """
    一个即将被发送的出站响应。
    """
    user_id: str
    channel: str
    content: str
    thread_id: Optional[str]


class SessionStart(BaseModel):
    """
    一个新会话已创建。
    """
    user_id: str
    session_id: str


class SessionEnd(BaseModel):
    """
    一个会话已结束（被修剪）。
    """
    user_id: str
    session_id: str


class ResponseTransform(BaseModel):
    """
    最终响应在轮次完成前正在被转换。
    """
    user_id: str
    thread_id: str
    response: str


class HookEvent(Enum):
    Inbound = Inbound
    ToolCall = ToolCall
    Outbound = Outbound
    SessionStart = SessionStart
    SessionEnd = SessionEnd
    ResponseTransform = ResponseTransform

    def hook_point(self) -> HookPoint:
        """
        返回此事件对应的 [`HookPoint`]。
        """



# pub trait Hook: Send + Sync
class Hook(ABC):
    """
    用于实现生命周期钩子的特质

    钩子可以在定义明确的节点拦截并修改智能体的操作
    Send + Sync 意味着一个类型既可以安全地在线程间转移所有权，又可以安全地在多个线程间共享引用
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        该hook的唯一name
        """
        pass

    @abstractmethod
    def hook_points(self) -> HookPoint:
        """
        此钩子应被调用的生命周期节点。
        """
        pass

    def failure_mode(self) -> HookFailureMode:
        """
        如何处理此钩子中发生的失败
        默认值：FailOpen（出错时继续执行）
        """
        return HookFailureMode.FailOpen

    def timeout(self) -> Duration:
        """
        此钩子允许运行的最长时间。
        默认值: 5s
        """
        return Duration.from_secs(5)

    @abstractmethod
    async def execute(self, event: HookEvent, ctx: HookContext) -> Union[HookOutcome, HookError]:
        """
        执行hook
        """
        pass
