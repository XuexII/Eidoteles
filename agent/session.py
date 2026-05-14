from pydantic import BaseModel, Field
from llm import ChatMessage, ToolCall
from uuid import uuid4
from typing import Optional, List, Dict, Any, Set
import logging
from datetime import datetime
from enum import Enum, auto

logger = logging.getLogger(__name__)


class ThreadState(str, Enum):
    # 线程空闲，等待输入。
    Idle = auto()
    # 线程正在处理一轮对话。
    Processing = auto()
    # 线程正在等待用户批准。
    AwaitingApproval = auto()
    # 线程已完成（预计不再有新的轮次）。
    Completed = auto()
    # 线程被中断。
    Interrupted = auto()


class PendingAuth(BaseModel):
    # 需要认证的扩展名称。
    extension_name: str
    created_at: datetime = Field(default_factory=datetime.now)


class PendingApproval(BaseModel):
    """
    存储在线程上的待处理工具批准请求。
    """
    request_id: str
    tool_name: str


class TurnState(str, Enum):
    # 正在处理
    Processing = auto()
    # 已完成
    Completed = auto()
    # 失败
    Failed = auto()
    # 该轮对话被中断
    Interrupted = auto()


class TurnToolCall(BaseModel):
    name: str
    parameters: Any
    result: Optional[Any] = None
    error: Optional[str] = None


class Turn(BaseModel):
    """
    线程中的一轮对话（请求/响应对）。
    """
    turn_number: int
    # 最开始的用户输入
    user_input: str
    # Agent的输出
    response: Optional[str] = None
    # 本轮对话中执行的工具调用。
    tool_calls: List[TurnToolCall] = Field(default_factory=list)
    # 本轮状态
    state: TurnState = ThreadState.Processing
    # 开始时间
    started_at: datetime = Field(default_factory=datetime.now)
    # 完成时间
    completed_at: Optional[datetime] = None
    # 报错信息
    error: Optional[str] = None
    # 用于多模态大语言模型输入的临时图像内容部分。
    # 不会被序列化——图像仅用于当前的大语言模型调用。
    # `user_input` 中的文本描述会持久化，用于上下文压缩。


class Thread(BaseModel):
    """
    会话中的对话线程。
    """
    # 唯一的id
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    # 当前状态
    state: ThreadState = ThreadState.Idle
    # 这个线程的轮次
    turns: List[Turn] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    # 更新时间
    updated_at: datetime = Field(default_factory=datetime.now)
    metadata: Optional[Dict] = None
    # 待处理的批准请求（当状态为 AwaitingApproval 时）。
    pending_approval: Optional[PendingApproval] = None
    # 待处理的身份验证令牌请求（线程处于身份验证模式）。
    pending_auth: Optional[PendingAuth] = None

    def start_turn(self, user_input: str) -> Turn:
        """
        以用户输入开始新的一轮。
        """
        turn_number = len(self.turns)
        turn = Turn(turn_number=turn_number, user_input=user_input)
        self.turns.append(turn)
        self.state = ThreadState.Processing
        self.updated_at = datetime.now()
        # turn_number 在入栈前等于长度，因此在入栈后是一个有效索引。
        return self.turns[turn_number]

    def messages(self) -> List[ChatMessage]:
        """
        获取用于上下文构建的所有消息，包括工具调用历史。
        按轮次输出完整的大语言模型兼容消息序列：
        `用户 → [带工具调用的助手 → 工具结果*] → 助手`
        这确保大语言模型能够看到之前的工具执行情况，从而避免在后续轮次中重复尝试已完成的操作。
        """

        messages = []
        for turn in self.turns:
            pass
        return messages



class Session(BaseModel):
    """
    包含一个或多个线程的会话。
    """
    # 唯一的id
    id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    # 激活的thread
    active_thread: Optional[str] = None
    # 全部的thread
    threads: Dict[str, Thread] = Field(default_factory=dict)
    # 创建时间
    created_at: datetime = Field(default_factory=datetime.now)
    # 最后激活时间
    last_active_at: datetime = Field(default_factory=datetime.now)
    metadata: Optional[Dict] = None
    # 已为此会话自动批准的工具（“始终批准”）。
    auto_approved_tools: Set[str] = Field(default_factory=set)


    def create_thread(self) -> Thread:
        """
        创建一个新的thread
        """
        thread = Thread(session_id=self.id)
        thread_id = thread.id
        self.active_thread = thread_id
        self.last_active_at = datetime.now()
        if thread_id in self.threads:
            return self.threads[thread_id]

        self.threads[thread_id] = thread
        return thread
