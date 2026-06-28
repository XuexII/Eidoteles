from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from host import (
    NoopKeyPersistor,
    NoopSessionRenewer,
    SharedSessionDb,
    SharedSessionKeyPersistor,
    SharedSessionRenewer,
    SharedSessionSecrets
)
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class SessionData(BaseModel):
    """
    会话数据已持久化到磁盘
    """
    session_token: str
    created_at: datetime
    auth_provider: Optional[str] = None


@dataclass
class NearWalletSignedMessage:
    """
    NEP-413 钱包签名及其覆盖的载荷，准备好通过 `/v1/auth/near` 交换为
    NEAR AI 会话令牌。浏览器通过已连接的 NEAR 钱包生成此对象；
    字段映射到 NEAR AI 的认证请求（注意嵌套的线字段使用 camelCase，
    而顶层键使用 snake_case）。
    """
    account_id: str
    public_key: str
    # 64 个原始 ed25519 签名字节的 base64 标准编码
    signature: str
    # 被签名的确切消息字符串（NEAR AI 期望一个固定值）
    message: str
    # 被签名的 NEP-413 接收者（NEAR AI 期望 `cloud.near.ai`）
    recipient: str
    # 被签名的 32 字节 nonce。NEAR AI 要求前 8 个字节
    # 为大端序的纪元毫秒时间戳（在 5 分钟窗口内验证）
    nonce: bytes
    callback_url: Optional[str] = None


def near_auth_request_body(signed: NearWalletSignedMessage) -> Dict[str, Any]:
    """
    构建 `/v1/auth/near` 请求体。保持独立以便线形状
    （snake_case 顶层 / camelCase 嵌套拆分，以及 nonce 作为字节数组）
    可以在没有 HTTP 往返的情况下进行单元测试。
    """
    payload: Dict[str, Any] = {
        "message": signed.message,
        "nonce": list(signed.nonce),
        "recipient": signed.recipient,
        "callbackUrl": signed.callback_url,
    }

    return {
        "signed_message": {
            "accountId": signed.account_id,
            "publicKey": signed.public_key,
            "signature": signed.signature,
            "state": None,
        },
        "payload": payload,
    }


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
    """
    管理 NEAR AI 会话令牌，支持持久化和自动续期。

    数据库 / 加密密钥 / 交互式续期 / env 持久化钩子通过 traits 抽象，
    因此此 crate 不需要依赖嵌入应用程序。无头部署可以不设置它们；
    CLI 构建会接入真实的实现。
    """
    config: SessionConfig
    client: Any = None  # httpx.AsyncClient，在 __post_init__ 中初始化
    # 内存中的当前令牌
    token: Optional[str] = None
    # 防止并发 401 时的惊群效应
    renewal_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 用于将会话持久化到设置表的可选数据库存储
    store: Optional[SharedSessionDb] = None
    # 数据库设置的用户 ID（默认："<unset>"）。占位符；在启动时由 attach_store() 用真实的 owner_id 覆盖
    user_id: str = "<unset>"
    # 可选加密密钥存储——如果存在，优先于明文设置
    secrets: Optional[SharedSessionSecrets] = None
    # 交互式续期钩子。默认为返回 `SessionRenewalFailed` 的空操作
    renewer: Optional[SharedSessionRenewer] = None
    # 一次性 API 密钥输入（运行时 env + .env 文件）的持久化器。默认为空操作
    key_persistor: Optional[SharedSessionKeyPersistor] = None

    def __post_init__(self):
        """初始化后设置 httpx 客户端。"""
        if self.client is None:
            import httpx
            self.client = httpx.AsyncClient(timeout=30.0)

    @classmethod
    def new(cls, config: SessionConfig) -> "SessionManager":
        """
        同步初始化
        """
        manager = cls(config)
        # 在构造期间同步尝试加载现有会话
        try:
            data = manager.config.session_path.read_text()
            session = SessionData(**json.loads(data))
            manager.token = session.session_token
            logger.info("已从 %s 加载会话令牌", manager.config.session_path)
        except Exception as e:
            logger.debug(f"SessionManager初始化时，未找到现有会话: {e}")
            pass

        return manager

    @classmethod
    async def new_async(cls, config: SessionConfig) -> "SessionManager":
        """
        创建会话管理器并异步加载令牌。
        """
        manager = cls(config)

        try:
            await manager.load_session()
        except Exception as e:
            logger.debug("SessionManager初始化时，未找到现有会话: %s", e)

        return manager

    async def attach_store(self, store: SharedSessionDb, user_id: str) -> None:
        """
        附加用于持久化会话令牌的数据库存储。

        附加存储后，会话令牌除了保存到磁盘文件外，还会保存到 `settings`
        表（键：`nearai.session_token`）。加载时，数据库优先于磁盘。
        """
        self.store = store
        self.user_id = user_id

        # 尝试从数据库加载（可能由之前的运行保存）
        try:
            await self.load_session_from_db()
        except Exception as e:
            logger.debug("数据库中无会话: %s", e)

    async def attach_secrets(self, secrets: SharedSessionSecrets) -> None:
        """
        附加用于安全会话令牌持久化的加密密钥存储。

        附加后，`save_session` 除了写入磁盘文件外，还会写入密钥存储，
        并且 `load_session_from_db` 优先使用密钥存储而非明文设置表。
        """
        self.secrets = secrets

        # 尝试从加密密钥加载（优先于设置表）
        try:
            await self.load_session_from_secrets()
        except Exception as e:
            logger.debug("密钥存储中无会话: %s", e)

    async def attach_renewer(self, renewer: SharedSessionRenewer) -> None:
        """
        附加在会话过期时使用的交互式续期器。

        无头部署可以跳过此步骤；默认的 `NoopSessionRenewer`
        返回 `SessionRenewalFailed`，调用者应提前设置
        `NEARAI_SESSION_TOKEN` 或 `NEARAI_API_KEY`。
        """
        self.renewer = renewer

    async def attach_key_persistor(self, persistor: SharedSessionKeyPersistor) -> None:
        """附加由续期器内 API 密钥输入路径使用的持久化器。"""
        self.key_persistor = persistor

    @property
    def auth_base_url(self) -> str:
        """对已配置的认证基础 URL 的只读访问（供续期器实现使用）。"""
        return self.config.auth_base_url

    async def near_wallet_login(
            self,
            signed: NearWalletSignedMessage,
    ) -> str:
        """
        将 NEP-413 钱包签名交换为 NEAR AI 会话令牌。

        将签名消息 POST 到 `{auth_base}/v1/auth/near`，成功时返回
        `access_token`。NEAR AI 拒绝没有 `User-Agent` 的请求，
        因此始终会发送一个。此操作不会持久化令牌——
        调用者应用它（例如通过 [`save_session_for_renewer`]），
        以便它通过与 OAuth 登录相同的磁盘/数据库/密钥管道。
        """
        url = f"{self.config.auth_base_url}/v1/auth/near"
        response = await self.client.post(
            url,
            headers={"User-Agent": "ironclaw"},
            json=near_auth_request_body(signed),
        )

        if not response.is_success:
            status = response.status_code
            body = response.text
            preview = body[:200] if len(body) > 200 else body
            raise RuntimeError(
                f"NEAR 钱包登录被拒绝: HTTP {status}: {preview}"
            )

        parsed = response.json()
        return parsed["access_token"]

    async def save_session_for_renewer(
            self,
            token: str,
            auth_provider: Optional[str] = None,
    ) -> None:
        """
        `SessionRenewer` 实现用于将新收到的会话令牌通过与内部流程
        相同的磁盘 + 数据库 + 密钥管道写回的公共钩子。
        """
        await self.save_session(token, auth_provider)
        self.token = token

    async def get_token(self) -> str:
        """获取当前会话令牌，如果未认证则抛出错误。"""
        if self.token is None:
            raise RuntimeError("nearai: 认证失败")
        return self.token

    async def has_token(self) -> bool:
        """检查是否有令牌（不向服务器验证）。"""
        return self.token is not None

    async def ensure_authenticated(self) -> None:
        """
        确保我们有有效的会话，如果需要则触发续期器。

        如果没有令牌，则向已注册的 `SessionRenewer` 请求一个。
        如果有令牌，则通过访问 `/v1/users/me` 验证它。
        如果验证失败，则向续期器请求新令牌。
        """
        if not await self.has_token():
            await self.run_renewer()
            return

        logger.debug("正在验证会话...")
        try:
            await self.validate_token()
            logger.debug("会话有效")
        except Exception as e:
            logger.info("会话已过期或无效: %s", e)
            await self.run_renewer()

    async def run_renewer(self) -> None:
        """运行续期器。"""
        if self.renewer is not None:
            await self.renewer.renew(self)
        else:
            raise RuntimeError("nearai: 会话续期失败")

    async def validate_token(self) -> None:
        """通过调用 /v1/users/me 端点验证当前令牌。"""
        token = await self.get_token()
        url = f"{self.config.auth_base_url}/v1/users/me"

        response = await self.client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )

        if response.is_success:
            return

        if response.status_code == 401:
            raise RuntimeError("nearai: 会话已过期")

        status = response.status_code
        body = response.text
        preview = body[:200] if len(body) > 200 else body
        raise RuntimeError(
            f"验证失败: HTTP {status}: {preview}"
        )

    async def handle_auth_failure(self) -> None:
        """
        处理认证失败（401 响应）。

        获取续期锁以防止惊群效应，然后向已注册的
        `SessionRenewer` 请求新令牌。
        """
        async with self.renewal_lock:
            logger.info("会话已过期或无效，正在重新认证...")
            await self.run_renewer()

    async def save_session(
            self,
            token: str,
            auth_provider: Optional[str] = None,
    ) -> None:
        """将会话数据保存到磁盘和（如果可用）数据库。"""
        session = SessionData(
            session_token=token,
            created_at=datetime.now(timezone.utc),
            auth_provider=auth_provider,
        )

        # 保存到磁盘（始终执行，作为引导回退）
        parent = self.config.session_path.parent
        if parent:
            parent.mkdir(parents=True, exist_ok=True)

        session_json = json.dumps(
            {
                "session_token": session.session_token,
                "created_at": session.created_at.isoformat(),
                "auth_provider": session.auth_provider,
            },
            indent=2,
        )

        self.config.session_path.write_text(session_json)

        # 限制性权限：会话文件包含密钥令牌
        if os.name != "nt":
            self.config.session_path.chmod(0o600)

        logger.debug("会话已保存到 %s", self.config.session_path)

        # 如果已附加，则持久化到加密密钥存储（首选）
        if self.secrets is not None:
            session_json_str = json.dumps(
                {
                    "session_token": session.session_token,
                    "created_at": session.created_at.isoformat(),
                    "auth_provider": session.auth_provider,
                }
            )
            try:
                await self.secrets.create(
                    self.user_id,
                    "nearai_session_token",
                    session_json_str,
                    "nearai",
                )
                logger.debug("会话已保存到加密密钥存储")
            except Exception as e:
                logger.warning("无法将会话保存到加密密钥: %s", e)
        # 仅在没有附加密钥存储时，才保存到数据库设置表作为回退
        elif self.store is not None:
            session_value = json.dumps(
                {
                    "session_token": session.session_token,
                    "created_at": session.created_at.isoformat(),
                    "auth_provider": session.auth_provider,
                }
            )
            try:
                await self.store.set_setting(
                    self.user_id,
                    "nearai.session_token",
                    session_value,
                )
                logger.debug("会话也已保存到数据库设置")
            except Exception as e:
                logger.warning("无法将会话保存到数据库: %s", e)

    async def load_session_from_db(self) -> None:
        """尝试从数据库加载会话。"""
        if self.store is None:
            raise RuntimeError("nearai: 未附加数据库存储")

        # 尝试加载会话令牌
        value = None
        try:
            value = await self.store.get_setting(
                self.user_id, "nearai.session_token"
            )
        except Exception as e:
            raise RuntimeError(f"数据库查询失败: {e}")

        if value is None:
            # 尝试遗留键。仅在实际存在时警告（真正的向后兼容迁移）。
            # 当两个键都不存在时（全新安装），直接返回"数据库中无会话"错误。
            try:
                legacy = await self.store.get_setting(
                    self.user_id, "nearai.session"
                )
            except Exception as e:
                raise RuntimeError(f"数据库查询失败: {e}")

            if legacy is not None:
                logger.warning(
                    "nearai.session_token 缺失；回退到遗留的 nearai.session 以保持向后兼容"
                )
                value = legacy
            else:
                raise RuntimeError("nearai: 数据库中无会话")

        # value 可能是 JSON 字符串或字典
        if isinstance(value, str):
            session_data = json.loads(value)
        else:
            session_data = value

        self.token = session_data["session_token"]
        logger.info("已从数据库设置加载会话")

    async def load_session_from_secrets(self) -> None:
        """
        尝试从加密密钥存储加载会话。

        会话以 JSON 序列化的 `SessionData` 字符串形式存储在
        密钥名称 `nearai_session_token` 下。当密钥存储可用时，
        此方式优先于明文设置表。
        """
        if self.secrets is None:
            raise RuntimeError("nearai: 未附加密钥存储")

        try:
            decrypted = await self.secrets.get_decrypted(
                self.user_id, "nearai_session_token"
            )
        except Exception as e:
            raise RuntimeError(f"密钥查找失败: {e}")

        session_data = json.loads(decrypted)
        self.token = session_data["session_token"]
        logger.info("已从加密密钥存储加载会话")

    async def load_session(self) -> None:
        """从磁盘加载会话数据。"""
        try:
            data = self.config.session_path.read_text()
        except Exception as e:
            raise RuntimeError(
                f"无法读取会话文件 {self.config.session_path}: {e}"
            )

        session_data = json.loads(data)
        self.token = session_data["session_token"]

        logger.info(
            "已从 %s 加载会话 (创建于: %s)",
            self.config.session_path,
            session_data.get("created_at", "未知"),
        )

    async def set_token(self, token: str) -> None:
        """直接设置令牌（用于测试或从环境变量迁移）。"""
        self.token = token


async def create_session_manager(config: SessionConfig) -> SessionManager:
    """
    从配置创建会话管理器，如果存在环境变量则加载。

    当设置 `NEARAI_SESSION_TOKEN` 时，它优先于基于文件的令牌。
    这支持托管提供者通过环境变量注入令牌。
    """
    manager = await SessionManager.new_async(config)

    # NEARAI_SESSION_TOKEN 环境变量始终优先于基于文件的令牌。
    # 托管提供者设置此环境变量，并期望它被直接使用——
    # 无需文件持久化。
    token = os.environ.get("NEARAI_SESSION_TOKEN")
    if token:
        logger.info("使用来自 NEARAI_SESSION_TOKEN 环境变量的会话令牌")
        await manager.set_token(token)

    return manager
