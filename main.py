import logging
import asyncio
from config import Config
from channels import HttpChannel, WebhookServerConfig, WebhookServer, ChannelManager
from copy import deepcopy
from typing import List, Dict, Optional
from fastapi import APIRouter
from agent import AgentDeps, Agent
from app import AppBuilder, AppBuilderFlags
from llm import create_session_manager
from channels.web.log_layer import LogBroadcaster
from pathlib import Path

logger = logging.getLogger(__name__)

def main():
    """同步入口点：加载.env文件，然后启动异步主函数"""
    # 加载.env文件（与Rust的dotenvy对应）
    from dotenv import load_dotenv
    load_dotenv()

    # 加载IronClaw特定环境变量（假设bootstrap模块有该函数）
    # bootstrap.load_ironclaw_env()

    # 使用asyncio运行异步主函数
    asyncio.run(async_main())


async def async_main():
    # --------------------加载配置--------------------
    toml_path = Path(".")
    try:
        config = await Config.from_env_with_toml(toml_path)
    except Exception as e:
        raise RuntimeError(f"加载配置文件失败")

    # --------------------初始化会话管理器--------------------
    # 在设置通道之前初始化会话管理器
    session = await create_session_manager(config.llm.session)
    # 在追踪系统初始化之前创建日志广播器，以便 WebLogLayer 能够捕获所有事件。
    log_broadcaster = LogBroadcaster()

    # --------------------通过 AppBuilder 构建所有核心组件--------------------
    flags = AppBuilderFlags(no_db=True)
    app_builder = AppBuilder(
        config=config,
        flags=flags,
        toml_path=toml_path,
        session=session,
        log_broadcaster=log_broadcaster
    )

    components = await app_builder.build_all()

    # --------------------通道设置--------------------
    channels = ChannelManager()
    channel_names: List[str] = []

    webhook_routes: List[APIRouter] = []

    # 添加http通道
    http_config = config.channels.http
    http_channel = HttpChannel(http_config)
    webhook_routes.append(http_channel.routes())
    host, port = http_channel.addr()
    channel_names.append(http_channel.name())
    await channels.add(http_channel)
    logger.info(f"http 通道已启动 {http_config.host}:{http_config.port}")

    # 如果已注册任何路由，则启动统一 Webhook 服务器。
    webhook_server = None
    if webhook_routes:
        server = WebhookServer(WebhookServerConfig(host=host, port=port))
        # 添加路由
        for routes in webhook_routes:
            server.add_routes(routes)

        await server.start()
        webhook_server = server

    # --------------------运行Agent--------------------
    # Agent依赖项设置
    deps = AgentDeps(
        owner_id=config.owner_id,
        store=components.db,
        llm=components.llm,
        tools=components.tools
    )

    agent = Agent(
        config=config,
        deps=deps,
        channels=channels
    )

    await agent.run()

    # --------------------退出--------------------