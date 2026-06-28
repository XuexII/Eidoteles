import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, List


@dataclass
class LogEntry:
    """广播到已连接客户端的单条日志条目。"""
    level: str
    target: str
    message: str
    timestamp: str


class LogBroadcaster:
    """
    将日志条目广播给 SSE 订阅者。

    在 main.rs 早期创建（在 tracing 初始化之前），
    与 tracing 层和网关的 SSE 端点共享。

    保留最近条目的环形缓冲区，以便在启动后连接的浏览器
    仍能看到启动日志。
    """

    def __init__(self, max_recent: int = 200):
        # 用于广播的异步队列（替代 broadcast::Sender）
        self._queues: List[asyncio.Queue] = []
        self._queues_lock = asyncio.Lock()
        # 环形缓冲区存储最近条目
        self.recent: Deque[LogEntry] = deque(maxlen=max_recent)
        # 在广播到 SSE 客户端之前，从日志消息中清除密钥
        self.leak_detector: LeakDetector = LeakDetector()

    async def subscribe(self) -> asyncio.Queue:
        """
        创建新的订阅队列。

        返回一个异步队列，调用者可以从中异步迭代以接收日志条目。
        """
        queue: asyncio.Queue = asyncio.Queue()
        async with self._queues_lock:
            self._queues.append(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        """移除订阅队列。"""
        async with self._queues_lock:
            if queue in self._queues:
                self._queues.remove(queue)

    async def broadcast(self, entry: LogEntry) -> None:
        """向所有订阅者广播日志条目，并存入环形缓冲区。"""
        # 存入环形缓冲区
        self.recent.append(entry)

        # 广播到所有订阅者
        async with self._queues_lock:
            queues = list(self._queues)

        for queue in queues:
            try:
                queue.put_nowait(entry)
            except asyncio.QueueFull:
                pass  # 丢弃慢速消费者的条目

    def get_recent(self) -> List[LogEntry]:
        """获取最近的日志条目，供稍后连接的客户端使用。"""
        return list(self.recent)


def init_tracing(log_broadcaster: LogBroadcaster, suppress_stderr: bool):
    pass