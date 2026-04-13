import asyncio
from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, Optional, Union

from channels import Channel, IncomingMessage, MessageStream, OutgoingResponse, StatusUpdate
from error import ChannelError
import logging
from futures import StreamExt
from pydantic import BaseModel, ConfigDict, Field
from schems.async_schems import RWLockDict

logger = logging.getLogger(__name__)



class ChannelManager(BaseModel):
    """
    管理多个输入通道并合并它们的消息流。
    包含一个注入通道，使得后台任务（例如任务监控器）可以向代理循环推送消息，而无需实现完整的 `Channel` trait。
    """
    channels: RWLockDict[str, Channel]
    # 创建容量为 64 的异步队列
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    # 在 `start_all()` 中获取一次，并合并到流中。


    def inject_sender(self):
        """
        获取注入发送器的一个克隆。

        后台任务（例如作业监视器）使用此发送器将消息推送到智能体循环中，而无需实现完整的 Channel 接口。
        """
        return self.queue

    async def add(self, channel: Channel):
        """
        向manger添加channel
        :param channel:
        :return:
        """
        name = channel.name
        async with self.channels.write():
            self.channels[name] = channel
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




