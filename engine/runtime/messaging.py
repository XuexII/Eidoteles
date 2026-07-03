# 通过通道进行线程间消息传递。

from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

from engine.types.capability import CapabilityLease
from engine.types.message import ThreadMessage
from engine.types.thread import ThreadId

from engine.gate import ResumeKind


class ThreadSignal:
    """通过邮箱发送给运行中线程的信号。"""
    pass


@dataclass
class ThreadStopSignal(ThreadSignal):
    """优雅地停止线程。"""
    pass


@dataclass
class ThreadSuspendSignal(ThreadSignal):
    """暂停执行（可以稍后恢复）。"""
    pass


@dataclass
class ThreadResumeSignal(ThreadSignal):
    """恢复挂起的线程。"""
    pass


@dataclass
class ThreadInjectMessageSignal(ThreadSignal):
    """向线程上下文中注入用户消息。"""
    message: ThreadMessage


@dataclass
class ThreadChildCompletedSignal(ThreadSignal):
    """子线程已完成的通知。"""
    child_id: ThreadId
    outcome: ThreadOutcome


class ThreadOutcome:
    """线程执行的最终结果。"""
    pass


@dataclass
class ThreadOutcomeCompleted(ThreadOutcome):
    """已完成，带有可选的文本响应。"""
    response: Optional[str] = None


@dataclass
class ThreadOutcomeStopped(ThreadOutcome):
    """线程被信号停止。"""
    pass


@dataclass
class ThreadOutcomeMaxIterations(ThreadOutcome):
    """达到最大迭代次数但未完成。"""
    pass


@dataclass
class ThreadOutcomeFailed(ThreadOutcome):
    """
    终端故障。

    error: 用户安全的错误消息。渲染到对话回复中。
    debug_detail: 从原始类型化错误保留的低级诊断详情
        （例如 Monty 解释器跟踪、Python 回溯、上游 HTTP 主体）。
        从不面向用户；仅通过网关调试模式显示。
        当原始错误没有携带超出 `error` 的额外详情时为 `None`。
    """
    error: str
    debug_detail: Optional[str] = None


@dataclass
class ThreadOutcomeGatePaused(ThreadOutcome):
    """
    统一执行门控暂停了线程。

    resume_output: 已完成的操作输出，应在恢复时注入而不是重新运行操作。
    paused_lease: 门控暂停操作时捕获的租约快照。
        装箱以将 `ThreadOutcome::GatePaused` 保持在裁剪的
        `large_enum_variant` 阈值以下——`CapabilityLease` 约 360 字节，
        否则将主导整个枚举的大小。
    """
    gate_name: str
    action_name: str
    call_id: str
    parameters: Dict[str, Any]
    resume_kind: ResumeKind
    resume_output: Optional[Dict[str, Any]] = None
    paused_lease: Optional[CapabilityLease] = None

type SignalSender = asyncio.Queue[ThreadSignal]
type SignalReceiver = asyncio.Queue[ThreadSignal]

def signal_channel(buffer: int) -> tuple:
    """
    创建具有给定缓冲区大小的新信号通道。

    每个线程获得一个 (sender, receiver) 对。
    `ThreadManager` 持有 sender；`ExecutionLoop` 持有 receiver。
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=buffer)
    # 在 Python 中，同一个队列对象既可用于发送也可用于接收
    return queue, queue
