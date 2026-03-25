import asyncio
from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, Optional, Union

from channels import Channel, IncomingMessage, MessageStream, OutgoingResponse, StatusUpdate
from error import ChannelError
import logging
from futures import StreamExt

# 管理多个输入通道并合并它们的消息流。
# 包含一个注入通道，使得后台任务（例如任务监控器）可以向代理循环推送消息，而无需实现完整的 `Channel` trait。

class ChannelManager:

    def __init__(
            self,
            channels: Dict[str, Channel],
            inject_tx,
            # Taken once in `start_all()` and merged into the stream.
            # tokio::sync::Mutex<Option<mpsc::Receiver<IncomingMessage>>>
            inject_rx
    ):
        self.channels = channels
        self.inject_tx = inject_tx
        self.inject_rx = inject_rx

    @classmethod
    def new(cls):
        # 创建了一个有界通道，缓冲区最多可容纳 64 条未处理的消息。当缓冲区满时，发送操作会等待，起到流量控制作用，防止生产过快导致内存无限增长。
        # 创建容量为 64 的队列
        inject_queue = asyncio.Queue(maxsize=64)
        # 发送端，将消息加入队列
        inject_tx = None
        # 接受端，异步等待下一条消息。当所有发送端被丢弃后，recv() 返回 None，表示通道已关闭
        inject_rx = None
        # TODO 实现一个带异步锁的自字典
        channels = {}
        return cls(channels, inject_tx, inject_rx)

    def inject_sender(self):
        """
        获取注入发送端的克隆。
        python队列的 put 方法天然支持多生产者，无需显式克隆发送端
        :return:
        """
        return self.inject_tx.clone()

    async def add(self, channel: Channel):
        """
        向manger添加channel
        :param channel:
        :return:
        """
        name = channel.name
        await self.channels.insert(name, channel)
        logging.debug("Added channel: {}", name)


    async def _forward_stream_messages(self, name, stream, tx):
        """转发消息"""
        try:
            async for msg in stream:
                try:
                    await tx.send(msg)
                except Exception as e:
                    logging.warning(f"{name}通道的接受通道已经关闭，停止热添加通道")
                    break
        except Exception as e:
            logging.error("流可能异常结束")

        finally:
            logging.debug(f"{name}通道的热添加结束")


    async def hot_add(self, channel: Channel):
        """
        向运行中的代理热添加一个通道。
        启动该通道，在用于 respond() / broadcast() 的通道映射中注册，并生成一个任务，通过 inject_tx 将其流消息转发到代理循环中。
        :param channel:
        :return:
        """
        name = channel.name

        # 关闭任何同名现有通道，以避免出现并行消费者
        # 旧转发任务将在通道关闭后、其流结束时停止
        channels = await self.channels.read()
        if existing := channels.get(name):
            logging.debug(f"在热添加前关闭已经存在的channel: {name}")
            await existing.shutdown()

        stream = await channel.start()

        # 注册用于 respond / broadcast / send_status
        await self.channels.insert(name, channel)

        # 通过 inject_tx 转发流消息
        tx = self.inject_tx.clone()
        asyncio.create_task(self._forward_stream_messages(name, stream, tx))




