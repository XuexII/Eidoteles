import logging
from typing import Dict, List, Optional, AsyncIterator
from pydantic import BaseModel, Field
from channels.channel import IncomingMessage, MessageStream, Channel
import asyncio
from dataclasses import dataclass, field
from utils.async_schems import RWLockDict
from aiorwlock import RWLock

logger = logging.getLogger(__name__)

# 管理多个输入通道并合并它们的消息流。
# 包含一个注入通道，使得后台任务（例如任务监控器）可以向代理循环推送消息，而无需实现完整的 `Channel` trait。
@dataclass
class ChannelManager:
    channels: Dict[str, Channel] = field(default_factory=dict)
    inject_tx: asyncio.Queue = asyncio.Queue(64)
    lock: RWLock = RWLock()
    inject_rx = None
    _inject_rx = None


    async def add(self, channel: Channel):
        """
        添加channel
        """
        name = channel.name()
        async with self.lock.writer_lock:
            self.channels[name] = channel
        logger.info(f"新增一个通道: {name}")

    async def start_all(self):
        """
        启动所有频道并返回合并后的消息流。
        还会合并注入通道，以便后台任务可以将消息推入同一消息流。
        """

        streams: List[MessageStream] = []

        for name, channel in self.channels.items():
            try:
                stream = await channel.start()
                logger.info(f"启动通道: {name}")
                streams.append(stream)
            except Exception as e:
                logger.error(f"启动通道失败: {name}")

        if not streams:
            return "ERRO: 所有通道启动失败"

        # 提取注入接收端（只做一次），如果存在则加入流列表
        # async with self._inject_lock:
        #     inject_rx = self._inject_rx
        #     self._inject_rx = None  # 取出后置空，确保只消费一次
        #
        # if inject_rx is not None:
        #     # 注入接收端本身就是一个 asyncio.Queue，直接用作流
        #     streams.append(inject_rx)
        #     logging.debug("Injection channel merged into message stream")

        # 创建统一的消息队列，所有子流和注入流都会汇聚到此
        merged: asyncio.Queue = asyncio.Queue()

        async def _merge(msg_stream: MessageStream):
            async for item in msg_stream:
                await merged.put(item)
            # 可选：放一个哨兵表示该流结束？但简单起见，流结束后任务直接退出

        merge_tasks = [asyncio.create_task(_merge(s)) for s in streams]

        return merged

