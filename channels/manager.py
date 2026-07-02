import logging
from typing import Dict, List, Optional, AsyncIterator, Any
from channels.channel import IncomingMessage, MessageStream, Channel
import asyncio
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ChannelManager:
    """管理多个输入频道并合并它们的消息流

    包含一个注入频道，以便后台任务（例如作业监视器）可以将消息推送到
    代理循环中，而无需成为完整的 `Channel` 实现
    """
    channels: Dict[str, Channel] = field(default_factory=dict)
    # 注入发送端，供后台任务使用
    inject_tx: Optional[asyncio.Queue] = field(default=None)
    # 在 `start_all()` 中取出一次并合并到流中
    inject_rx: Optional[asyncio.Queue] = field(default=None)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        if self.inject_tx is None:
            self.inject_tx = asyncio.Queue(maxsize=64)
            self.inject_rx = self.inject_tx  # 使用同一个队列进行收发

    def inject_sender(self) -> asyncio.Queue:
        """获取注入发送端的克隆

        后台任务（如作业监视器）使用此将消息推送到代理循环中，
        而无需成为完整的 `Channel` 实现
        """
        return self.inject_tx

    async def add(self, channel: Channel) -> None:
        """向管理器添加频道"""
        name = channel.name()
        async with self._lock:
            self.channels[name] = channel
        logger.debug(f"已添加频道: {name}")

    async def hot_add(self, channel: Channel) -> None:
        """热添加频道到运行中的代理

        启动频道，在频道映射中注册它以用于 `respond()`/`broadcast()`，
        并创建一个任务，将其流消息通过 `inject_tx` 转发到代理循环中
        """
        name = channel.name()

        # 关闭任何同名的现有频道以避免并行消费者。
        # 旧的转发任务将在频道流在关闭后结束时停止
        async with self._lock:
            if name in self.channels:
                existing = self.channels[name]
                logger.debug(f"频道 '{name}': 在热添加替换之前关闭现有频道")
                try:
                    await existing.shutdown()
                except Exception:
                    pass

        stream = await channel.start()

        # 注册以用于 respond/broadcast/send_status
        async with self._lock:
            self.channels[name] = channel

        # 通过 inject_tx 转发流消息
        async def forward_messages():
            try:
                async for msg in stream:
                    try:
                        self.inject_tx.put_nowait(msg)
                    except asyncio.QueueFull:
                        logger.warning(f"频道 '{name}': 注入频道已满，丢弃消息")
            except Exception:
                pass
            logger.debug(f"频道 '{name}': 热添加的频道流已结束")

        asyncio.create_task(forward_messages())

    async def start_all(self) -> List[Any]:
        """启动所有频道并返回合并的消息流

        同时合并注入频道，以便后台任务可以将消息推送到同一流中
        """
        async with self._lock:
            channels_snapshot = dict(self.channels)

        streams = []

        for name, channel in channels_snapshot.items():
            try:
                stream = await channel.start()
                logger.debug(f"已启动频道: {name}")
                streams.append(stream)
            except Exception as e:
                logger.error(f"启动频道 {name} 失败: {e}")
                # 继续处理其他频道，不完全失败

        if not streams:
            raise ChannelError(
                name="all",
                reason="没有频道成功启动",
            )

        # 将注入接收端也加入流合并（注入接收端只能被取出一次）
        if self.inject_rx is not None:
            # 创建从注入队列读取的异步生成器
            async def inject_stream():
                while True:
                    try:
                        msg = await self.inject_rx.get()
                        yield msg
                    except Exception:
                        break

            streams.append(inject_stream())
            logger.debug("注入频道已合并到消息流中")

        # 合并所有流
        return streams

    async def respond(
            self,
            msg: IncomingMessage,
            response: OutgoingResponse,
    ) -> None:
        """向特定频道发送响应"""
        async with self._lock:
            channel = self.channels.get(msg.channel)

        if channel is not None:
            await channel.respond(msg, response)
        else:
            raise ChannelError(
                name=msg.channel,
                reason="未找到频道",
            )

    async def send_status(
            self,
            channel_name: str,
            status: StatusUpdate,
            metadata: dict,
    ) -> None:
        """向特定频道发送状态更新

        元数据包含频道特定的路由信息（例如 Telegram chat_id），
        用于将状态传递到正确的目的地
        """
        async with self._lock:
            channel = self.channels.get(channel_name)

        if channel is not None:
            await channel.send_status(status, metadata)
        else:
            # 静默忽略未找到的频道（状态是尽力而为的）
            pass

    async def broadcast(
            self,
            channel_name: str,
            user_id: str,
            response: OutgoingResponse,
    ) -> None:
        """向特定频道上的特定用户广播消息

        用于主动通知，如心跳警报
        """
        async with self._lock:
            channel = self.channels.get(channel_name)

        if channel is not None:
            await channel.broadcast(user_id, response)
        else:
            raise ChannelError(
                name=channel_name,
                reason="未找到频道",
            )

    async def broadcast_all(
            self,
            user_id: str,
            response: OutgoingResponse,
    ) -> List[Tuple[str, Any]]:
        """向所有频道广播消息

        在每个注册的频道上向指定用户发送
        """
        async with self._lock:
            channels_snapshot = dict(self.channels)

        results = []
        for name, channel in channels_snapshot.items():
            try:
                await channel.broadcast(user_id, response.clone() if hasattr(response, 'clone') else response)
                results.append((name, None))
            except Exception as e:
                results.append((name, e))

        return results

    async def health_check_all(self) -> Dict[str, Any]:
        """检查所有频道的健康状况"""
        async with self._lock:
            channels_snapshot = dict(self.channels)

        results = {}
        for name, channel in channels_snapshot.items():
            try:
                results[name] = await channel.health_check()
            except Exception as e:
                results[name] = e

        return results

    async def shutdown_all(self) -> None:
        """关闭所有频道"""
        async with self._lock:
            channels_snapshot = dict(self.channels)

        for name, channel in channels_snapshot.items():
            try:
                await channel.shutdown()
            except Exception as e:
                logger.error(f"关闭频道 {name} 时出错: {e}")

    async def channel_names(self) -> List[str]:
        """获取频道名称列表"""
        async with self._lock:
            return list(self.channels.keys())

    async def get_channel(self, name: str) -> Optional[Channel]:
        """按名称获取频道"""
        async with self._lock:
            return self.channels.get(name)

    async def remove(self, name: str) -> Optional[Channel]:
        """从管理器中移除频道"""
        async with self._lock:
            return self.channels.pop(name, None)
