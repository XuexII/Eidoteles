from pydantic import BaseModel, Field, ConfigDict
from abc import ABC, abstractmethod
from pydantic import BaseModel, Field, ConfigDict
from typing import Union, Optional, Dict
from enum import Enum, auto
from schems.enum_schems import ClassEnum
from dataclasses import dataclass


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


class HookEvent(ClassEnum):
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
        pass

    def apply_modification(self, modified: str):
        """
        将一个修改字符串应用到事件的主要内容字段。
        :param modified:
        :return:
        """
        pass

class Continue(BaseModel):
    """
    继续处理，可选择使用修改后的内容。
    """
    # 如果不为为 `None`，则将此值替换事件的主要内容。
    modified: Optional[str] = None

    __match_args__ = ("modified",)

class Reject(BaseModel):
    """
    完全拒绝此事件。
    """
    # 拒绝原因的人类可读说明。
    reason: str

    __match_args__ = ("reason",)

class HookOutcome(ClassEnum):
    """
    hook的执行结果
    """
    Continue: Continue
    Reject: Reject


    @classmethod
    def ok(cls):
        return cls.Continue(modified=None)

    @classmethod
    def modify(cls, value: str):
        return cls.Continue(modified=value)

    @classmethod
    def reject(cls, reason: str):
        return cls.Reject(reason=reason)


class HookFailureMode(Enum):
    """
    如何处理钩子执行失败的情况。
    """
    # 发生错误或超时时，继续处理，如同钩子返回了 `ok()`。
    FailOpen = auto()
    # 发生错误或超时时，拒绝此事件。
    FailClosed = auto()

class ExecutionFailed(BaseModel):
    """
    [error("钩子执行失败：{reason}")]
    """
    reason: str

class Timeout(BaseModel):
    """
    [error("hook执行超时，执行时间：{timeout}")]
    """
    timeout: float

class Rejected(BaseModel):
    """
    [error("hook被拒绝：{reason}")]
    """

    reason: str

class HookError(ClassEnum):
    """
    钩子执行错误。
    """
    ExecutionFailed = ExecutionFailed
    Timeout = Timeout
    Rejected = Rejected




class HookContext(BaseModel):
    """
    与事件一起传递给钩子的上下文。
    """
    # 钩子可以使用的任意元数据。
    metadata: Optional[Dict] = None


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

    def timeout(self) -> float:
        """
        此钩子允许运行的最长时间。
        默认值: 5s
        """
        return 5.0

    @abstractmethod
    async def execute(self, event: HookEvent, ctx: HookContext) -> Union[HookOutcome, HookError]:
        """
        执行hook
        """
        pass
