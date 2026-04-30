import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
from pydantic import BaseModel, SecretStr
from typing_extensions import Self

from db import Database

logger = logging.getLogger(__name__)


class SessionData(BaseModel):
    """
    会话数据已持久化到磁盘
    """
    session_token: str
    created_at: datetime
    auth_provider: Optional[str] = None


class SessionConfig(BaseModel):
    """
    会话管理配置。
    """
    # 认证端点的基础 URL（例如 https://private.near.ai）
    auth_base_url: str = "https://private.near.ai/"
    # 会话文件路径（例如 ~/.ironclaw/session.json）
    session_path: Path = Path("session.json")


@dataclass
class SessionManager:
    """管理 NEAR AI 会话令牌，支持持久化与自动续期。"""

    config: SessionConfig
    client: aiohttp.ClientSession = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    # 当前内存中的令牌(api_key)
    token: Optional[SecretStr] = None
    # 防止在并发 401 错误时出现惊群效应。
    renewal_lock: asyncio.Lock = asyncio.Lock()
    # 用于将会话持久化到设置表的可选数据库存储。
    store: Optional[Database] = None
    # 用于数据库设置的用户 ID（默认值："default"）
    user_id: str = "default"

    # def __post_init__(self):
    #     # 在构造期间同步尝试加载现有会话。
    #     session_path: Path = self.config.session_path
    #     try:
    #         data = session_path.read_text(encoding='utf-8')
    #         session = SessionData.model_validate_json(data)
    #     except:
    #         pass

    def load_existing_session(self) -> Self:
        """
        在构造期间同步尝试加载现有会话。
        :return:
        """
        try:
            with open(self.config.session_path, 'r') as f:
                data = f.read()
            session = SessionData.model_validate_json(data)
            # TODO 可能需要实现锁
            self.token = SecretStr(session.session_token)
            logger.info(f"从 {self.config.session_path} 加载会话令牌")

        except Exception as e:
            pass

        return self

    @classmethod
    async def new_async(cls, config: SessionConfig) -> "SessionManager":
        """
        创建一个会话管理器并异步加载令牌。
        :param config:
        :return:
        """
        pass


async def create_session_manager(config: SessionConfig) -> SessionManager:
    """
    根据配置创建会话管理器，若存在环境变量则加载
    当设置了 NEARAI_SESSION_TOKEN 环境变量时，其优先级高于基于文件的令牌
    这适用于通过环境变量注入令牌的主机服务提供商
    """
    import os

    manager = SessionManager(config)

    if token := os.environ.get("NEARAI_SESSION_TOKEN", None):
        logger.info(f"使用环境变量中NEARAI_SESSION_TOKEN")
        # manager.set_token(SecretStr(token))

    return manager
