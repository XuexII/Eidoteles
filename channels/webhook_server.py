import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, APIRouter

logger = logging.getLogger(__name__)


@dataclass
class WebhookServerConfig:
    """
    统一 Webhook 服务器的配置。
    """
    # 服务的ip地址
    host: str = "127.0.0.1"
    # 端口号
    port: int = 8080


@dataclass
class WebhookServer:
    """
    一个托管所有 Webhook 路由的单一 HTTP 服务器。

    各频道通过 `add_routes()` 贡献自己的路由片段，然后通过单次
    `start()` 调用绑定监听器并启动服务器任务。
    """
    config: WebhookServerConfig
    routes: List[APIRouter] = field(default_factory=list)
    # 合并后的路由器在 start() 之后保存，以便通过 `install_listener()` 重启。
    app: Optional[FastAPI] = None
    # 关闭事件，用于停止服务器（对应 oneshot 发送端）
    shutdown_event: Optional[asyncio.Event] = None
    # 服务器任务句柄
    handle: Optional[asyncio.Task] = None

    def add_routes(self, router: APIRouter):
        """
        累加一个路由片段。每个片段应已通过 `.with_state()` 应用其状态。
        """
        self.routes.append(router)

    async def start(self):
        """
        绑定监听器，合并所有路由片段，并启动服务器。
        """
        app = FastAPI()
        while self.routes:
            fragment = self.routes.pop(0)  # pop(0) 模拟 drain: 取出并移除
            app.include_router(fragment)

        self.app = app

        # 绑定并启动服务器
        await self.bind_and_spawn(app)

    async def bind_and_spawn(self, app: FastAPI):
        """
        将监听器绑定到配置的地址并生成服务器任务。
        由 `start()` 使用的私有辅助方法。
        """
        # 创建关闭事件（替代 oneshot channel）
        self.shutdown_event = asyncio.Event()

        # 配置 uvicorn 服务器
        config = uvicorn.Config(
            app,
            host=self.config.host,
            port=self.config.port,
            log_level="info",
        )
        server = uvicorn.Server(config)

        # 启动服务器任务
        self.handle = asyncio.create_task(
            self._run_server(server)
        )

        logger.info(f"Webhook 服务器已启动 {self.config.host}:{self.config.port}")

    async def _run_server(self, server: uvicorn.Server) -> None:
        """
        后台运行的服务器主循环，支持优雅关闭。
        """
        serve_task = asyncio.create_task(server.serve())
        stop_task = asyncio.create_task(self.shutdown_event.wait())

        done, pending = await asyncio.wait(
            [serve_task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_task in done:
            # 收到关闭信号，触发优雅关闭
            logger.debug("Webhook server shutting down")
            server.should_exit = True
            await serve_task
        else:
            # serve 任务意外结束，检查是否有错误
            if serve_task.exception():
                logger.error(f"Webhook server error: {serve_task.exception()}")
