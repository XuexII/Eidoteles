from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict
from pathlib import Path
from llm.session import SessionConfig
import logging

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Tuple
from enum import Enum
import logging

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────

# 当只有 OAuth 令牌存在时用作 `api_key` 的哨兵值。
# 当我们只有 OAuth 令牌时，`llm/mod.rs` 中的提供者工厂检查此值并
# 路由到 `AnthropicOAuthProvider`，因此此占位符永远不会通过网络发送
OAUTH_PLACEHOLDER = "oauth-placeholder"


# ── 缓存保留策略 ─────────────────────────────────────────────

class CacheRetention(Enum):
    """Anthropic 的提示缓存保留策略

    通过 rig-core 的 `additional_params` 注入的顶层 `cache_control` 字段
    控制 Anthropic 的自动提示缓存：
    - `None` — 缓存禁用，不注入 `cache_control`
    - `Short` — 5 分钟 TTL（默认），`{"type": "ephemeral"}`，1.25× 写入附加费
    - `Long` — 1 小时 TTL，`{"type": "ephemeral", "ttl": "1h"}`，2× 写入附加费
    """
    None_ = "none"  # 无提示缓存
    Short = "short"  # 5 分钟 TTL（默认）。写入成本：1.25× 基础输入
    Long = "long"  # 1 小时 TTL。写入成本：2× 基础输入

    @classmethod
    def from_str(cls, s: str) -> "CacheRetention":
        """从字符串解析缓存保留策略

        Args:
            s: 策略字符串（不区分大小写）

        Returns:
            对应的 CacheRetention 枚举值

        Raises:
            ValueError: 当字符串无效时
        """
        lower = s.lower()
        if lower in ("none", "off", "disabled"):
            return cls.None_
        elif lower in ("short", "5m", "ephemeral"):
            return cls.Short
        elif lower in ("long", "1h"):
            return cls.Long
        else:
            raise ValueError(
                f"无效的缓存保留策略 '{s}'，预期为: none, short, long"
            )

    def __str__(self) -> str:
        return self.value


# ── 注册表提供者配置 ─────────────────────────────────────────

@dataclass
class RegistryProviderConfig:
    """基于注册表的提供者的已解析配置

    此单一结构体替代了过去五个独立的配置类型
    （`OpenAiDirectConfig`、`AnthropicDirectConfig`、`OllamaConfig`、
    `OpenAiCompatibleConfig`、`TinfoilConfig`）。`protocol` 字段决定使用哪个
    rig-core 客户端构造函数
    """
    # 使用哪个 API 协议（决定 rig-core 客户端）
    protocol: Any  # ProviderProtocol
    # 提供者标识符（例如 "groq"、"openai"、"tinfoil"）
    provider_id: str
    # API 密钥（对于某些提供者如 Ollama 可选）。
    # 对于 Anthropic OAuth，此值设置为 `OAUTH_PLACEHOLDER`
    api_key: Optional[str] = None
    # API 端点的基础 URL
    base_url: str = ""
    # 模型标识符
    model: str = ""
    # 注入到每个请求的额外 HTTP 头部
    extra_headers: List[Tuple[str, str]] = field(default_factory=list)
    # 支持 Bearer 认证的提供者的 OAuth 令牌（例如通过 `claude login` 的 Anthropic）。
    # 设置时，提供者工厂路由到 OAuth 特定的提供者实现
    oauth_token: Optional[str] = None
    # 为 true 时，将 OpenAI 兼容流量路由到 Codex ChatGPT Responses API 提供者，
    # 而不是 rig-core 的 Chat Completions 路径
    is_codex_chatgpt: bool = False
    # Codex ChatGPT 令牌刷新的 OAuth 刷新令牌
    refresh_token: Optional[str] = None
    # Codex auth.json 的路径，用于持久化刷新的令牌
    auth_path: Optional[Path] = None
    # 提示缓存保留（Anthropic 专用）
    cache_retention: CacheRetention = CacheRetention.None_
    # 此提供者不支持的参数名称（例如 `["temperature"]`）。
    # 支持的键：`"temperature"`、`"max_tokens"`、`"stop_sequences"`。
    # 列出的参数在发送之前从请求中剥离以避免 400 错误
    unsupported_params: List[str] = field(default_factory=list)

    @classmethod
    def generic(
            cls,
            protocol: Any,
            provider_id: str,
            api_key: Optional[str],
            base_url: str,
            model: str,
    ) -> "RegistryProviderConfig":
        """构建一个通用的注册表提供者配置，将提供者特定的可选旋钮保留为其中性默认值"""
        return cls(
            protocol=protocol,
            provider_id=provider_id,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )

    def with_extra_headers(self, extra_headers: List[Tuple[str, str]]) -> "RegistryProviderConfig":
        """设置额外的 HTTP 头部"""
        self.extra_headers = extra_headers
        return self

    def with_unsupported_params(self, unsupported_params: List[str]) -> "RegistryProviderConfig":
        """设置不支持的参数列表"""
        self.unsupported_params = unsupported_params
        return self


# ── OpenAI Codex 配置 ────────────────────────────────────────

@dataclass
class OpenAiCodexConfig:
    """OpenAI Codex（ChatGPT 订阅 OAuth）的配置"""
    # 要使用的模型（默认："gpt-5.5"）。必须是 ChatGPT 帐户有权使用的模型：
    # codex 专用标识符如 `gpt-5.3-codex` 适用于 API 密钥 Codex 帐户，
    # 但订阅后端以 HTTP 400 拒绝它们，而此提供者仅限订阅
    model: str = "gpt-5.5"
    # OAuth 授权服务器（默认："https://auth.openai.com"）
    auth_endpoint: str = "https://auth.openai.com"
    # Responses API 基础 URL（默认："https://chatgpt.com/backend-api/codex"）
    api_base_url: str = "https://chatgpt.com/backend-api/codex"
    # OAuth 客户端 ID（默认：OpenAI 的公共 Codex 客户端）
    client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    # 会话文件路径（默认：~/.ironclaw/openai_codex_session.json）
    session_path: Path = field(default_factory=lambda: Path.home() / ".ironclaw" / "openai_codex_session.json")
    # 主动刷新前的过期秒数（默认：300）
    token_refresh_margin_secs: int = 300

    @classmethod
    def build(
            cls,
            model: Optional[str] = None,
            auth_endpoint: Optional[str] = None,
            api_base_url: Optional[str] = None,
            client_id: Optional[str] = None,
            session_path: Optional[Path] = None,
            token_refresh_margin_secs: Optional[int] = None,
    ) -> "OpenAiCodexConfig":
        """从已解析的覆盖构建 Codex 配置，对于调用者留空的任何字段回退到 crate 默认值。
        调用者（二进制）拥有环境/设置优先级和 SSRF 验证；
        此辅助函数将默认值集中到 crate 内部
        """
        defaults = cls()
        return cls(
            model=model or defaults.model,
            auth_endpoint=auth_endpoint or defaults.auth_endpoint,
            api_base_url=api_base_url or defaults.api_base_url,
            client_id=client_id or defaults.client_id,
            session_path=session_path or defaults.session_path,
            token_refresh_margin_secs=token_refresh_margin_secs or defaults.token_refresh_margin_secs,
        )


# ── AWS Bedrock 配置 ─────────────────────────────────────────

@dataclass
class BedrockConfig:
    """AWS Bedrock（原生 Converse API）的配置"""
    # AWS 区域（例如 "us-east-1"）
    region: str = "us-east-1"
    # Bedrock 模型 ID（例如 "anthropic.claude-opus-4-6-v1"）
    model: str = ""
    # 跨区域推理前缀："us"、"eu"、"apac"、"global" 或 None
    cross_region: Optional[str] = None
    # AWS 命名配置文件（用于 SSO/assume-role 工作流）
    profile: Optional[str] = None

    # 未配置时使用的默认区域
    DEFAULT_REGION = "us-east-1"

    # Bedrock 接受的有效跨区域推理前缀
    VALID_CROSS_REGION_PREFIXES = {"us", "eu", "apac", "global"}

    @classmethod
    def build(
            cls,
            region: Optional[str] = None,
            model: Optional[str] = None,
            cross_region: Optional[str] = None,
            profile: Optional[str] = None,
    ) -> "BedrockConfig":
        """从已解析的覆盖构建 Bedrock 配置

        - `region` 在 `None` 时回退到 `DEFAULT_REGION`
        - `model` 是必需的（`None` 时引发 `ValueError`）
        - `cross_region` 设置时根据 `VALID_CROSS_REGION_PREFIXES` 验证

        Raises:
            ValueError: 当 model 缺失或 cross_region 无效时
        """
        region = region or cls.DEFAULT_REGION
        if model is None:
            raise ValueError(
                "缺少 BEDROCK_MODEL: 设置 BEDROCK_MODEL 或当 LLM_BACKEND=bedrock 时的 selected_model"
            )
        if cross_region is not None and cross_region not in cls.VALID_CROSS_REGION_PREFIXES:
            raise ValueError(
                f"'{cross_region}' 无效，预期为: {', '.join(sorted(cls.VALID_CROSS_REGION_PREFIXES))}"
            )
        return cls(
            region=region,
            model=model,
            cross_region=cross_region,
            profile=profile,
        )


# ── NEAR AI 配置 ─────────────────────────────────────────────

@dataclass
class NearAiConfig:
    """NEAR AI 配置"""
    # 要使用的模型（例如 "claude-3-5-sonnet-20241022"、"gpt-4o"）
    model: str = ""
    # 轻量级任务的廉价/快速模型（心跳、路由、评估）
    cheap_model: Optional[str] = None
    # NEAR AI API 的基础 URL
    base_url: str = "https://private.near.ai"
    # NEAR AI Cloud 的 API 密钥
    api_key: Optional[str] = None
    # 可选的故障转移回退模型
    fallback_model: Optional[str] = None
    # 瞬态错误的最大重试次数（默认：3）
    max_retries: int = 3
    # 断路器打开前的连续失败次数。None = 禁用
    circuit_breaker_threshold: Optional[int] = None
    # 断路器在探测前保持打开的秒数（默认：30）
    circuit_breaker_recovery_secs: int = 30
    # 启用内存响应缓存。默认：false
    response_cache_enabled: bool = False
    # 缓存响应的 TTL 秒数（默认：3600）
    response_cache_ttl_secs: int = 3600
    # LRU 驱逐前的最大缓存响应数（默认：1000）
    response_cache_max_entries: int = 1000
    # 故障转移冷却持续时间秒数（默认：300）
    failover_cooldown_secs: int = 300
    # 故障转移冷却前的连续失败次数（默认：3）
    failover_cooldown_threshold: int = 3
    # 启用智能路由级联模式。默认：true
    smart_routing_cascade: bool = True

    @classmethod
    def for_model_discovery(cls) -> "NearAiConfig":
        """创建适合列出可用模型的最小配置

        从环境读取 `NEARAI_API_KEY` 并选择适当的基础 URL
        （存在 API 密钥时为 cloud-api，会话令牌认证时为 private.near.ai）
        """
        api_key = os.environ.get("NEARAI_API_KEY", "")
        api_key = api_key.strip() if api_key else None

        if api_key:
            default_base = "https://cloud-api.near.ai"
        else:
            default_base = "https://private.near.ai"

        base_url = os.environ.get("NEARAI_BASE_URL", default_base)

        return cls(
            model="",
            cheap_model=None,
            base_url=base_url,
            api_key=api_key,
            fallback_model=None,
            max_retries=3,
            circuit_breaker_threshold=None,
            circuit_breaker_recovery_secs=30,
            response_cache_enabled=False,
            response_cache_ttl_secs=3600,
            response_cache_max_entries=1000,
            failover_cooldown_secs=300,
            failover_cooldown_threshold=3,
            smart_routing_cascade=True,
        )


# ── Gemini OAuth 配置 ────────────────────────────────────────

@dataclass
class GeminiOauthConfig:
    """Gemini OAuth 集成的配置

    扩展生成配置参数（topP、topK、seed 等）在请求时从环境变量读取：
    - `GEMINI_TOP_P` — 核采样（0.0–1.0）
    - `GEMINI_TOP_K` — top-k 采样（整数）
    - `GEMINI_SEED` — 确定性生成种子
    - `GEMINI_PRESENCE_PENALTY` — 存在惩罚（-2.0–2.0）
    - `GEMINI_FREQUENCY_PENALTY` — 频率惩罚（-2.0–2.0）
    - `GEMINI_RESPONSE_MIME_TYPE` — 例如 "application/json"
    - `GEMINI_RESPONSE_JSON_SCHEMA` — 结构化输出的 JSON schema 字符串
    - `GEMINI_CACHED_CONTENT` — 缓存内容资源名称
    - `GEMINI_CLI_CUSTOM_HEADERS` — 自定义头部（key:value,key:value）
    - `GOOGLE_GENAI_API_VERSION` — API 版本（默认：v1beta）
    - `GEMINI_API_KEY` — 可选的非 OAuth 认证模式的 API 密钥
    - `GEMINI_API_KEY_AUTH_MECHANISM` — "x-goog-api-key"（默认）或 "bearer"
    """
    model: str = "gemini-2.5-flash"
    credentials_path: Path = field(default_factory=lambda: Path.home() / ".gemini" / "oauth_creds.json")

    # 未配置时使用的默认模型
    DEFAULT_MODEL = "gemini-2.5-flash"

    @staticmethod
    def default_credentials_path() -> Path:
        """默认凭证路径"""
        return Path.home() / ".gemini" / "oauth_creds.json"

    @classmethod
    def build(
            cls,
            model: Optional[str] = None,
            credentials_path: Optional[Path] = None,
    ) -> "GeminiOauthConfig":
        """从已解析的覆盖构建 Gemini OAuth 配置

        当各自的覆盖缺失时，回退到 `DEFAULT_MODEL` 和 `default_credentials_path`
        """
        return cls(
            model=model or cls.DEFAULT_MODEL,
            credentials_path=credentials_path or cls.default_credentials_path(),
        )


# ── LLM 后端类型 ─────────────────────────────────────────────

class LlmBackendKind(Enum):
    """LLM 后端类型"""
    NearAi = "nearai"
    Bedrock = "bedrock"
    GeminiOauth = "gemini_oauth"
    OpenAiCodex = "openai_codex"
    Registry = "registry"

    @classmethod
    def from_backend_id(cls, backend: str) -> "LlmBackendKind":
        """从后端标识符字符串解析后端类型"""
        backend_lower = backend.lower()
        if backend_lower in ("nearai", "near_ai", "near"):
            return cls.NearAi
        elif backend_lower in ("bedrock", "aws_bedrock", "aws"):
            return cls.Bedrock
        elif backend_lower in ("gemini_oauth", "gemini-oauth"):
            return cls.GeminiOauth
        elif backend_lower in ("openai_codex", "openai-codex", "codex"):
            return cls.OpenAiCodex
        else:
            return cls.Registry

    def provider_id(self, registry_provider: Optional[RegistryProviderConfig] = None) -> str:
        """获取提供者标识符"""
        if self == LlmBackendKind.NearAi:
            return "nearai"
        elif self == LlmBackendKind.Bedrock:
            return "bedrock"
        elif self == LlmBackendKind.GeminiOauth:
            return "gemini_oauth"
        elif self == LlmBackendKind.OpenAiCodex:
            return "openai_codex"
        else:
            if registry_provider is not None:
                return registry_provider.provider_id
            return "registry"


# ── LLM 配置 ─────────────────────────────────────────────────

@dataclass
class LlmConfig:
    """LLM 提供者配置

    NearAI 仍然是默认后端，有自己的配置结构体（会话认证）。
    所有其他提供者通过提供者注册表解析，生成通用的 `RegistryProviderConfig`
    """
    # 后端标识符（例如 "nearai"、"openai"、"groq"、"tinfoil"）
    backend: str = "nearai"
    # 会话管理器配置（认证 URL、令牌持久化路径）。
    # 由 NearAI 提供者用于 OAuth/会话令牌认证
    session: SessionConfig = None  # SessionConfig
    # NEAR AI 配置（始终填充，也用于嵌入）
    nearai: NearAiConfig = field(default_factory=NearAiConfig)
    # 基于注册表的提供者的已解析提供者配置。
    # 当后端为 "nearai" 或 "bedrock" 时为 None
    provider: Optional[RegistryProviderConfig] = None
    # AWS Bedrock 配置（当 backend=bedrock 时填充）
    bedrock: Optional[BedrockConfig] = None
    # Gemini OAuth 配置（当 backend=gemini_oauth 时填充）
    gemini_oauth: Optional[GeminiOauthConfig] = None
    # OpenAI Codex 配置（当 backend=openai_codex 时填充）
    openai_codex: Optional[OpenAiCodexConfig] = None
    # LLM API 调用的 HTTP 请求超时秒数。
    # 默认：120。对于需要更多时间在消费硬件上进行提示评估的
    # 本地 LLM（Ollama、vLLM、LM Studio）增加此值
    request_timeout_secs: int = 120
    # 轻量级任务的通用廉价/快速模型（心跳、路由、评估）。
    # 适用于任何后端。通过 `LLM_CHEAP_MODEL` 环境变量设置。
    # 设置时，优先于 NearAI 特定的 `NEARAI_CHEAP_MODEL`
    cheap_model: Optional[str] = None
    # 启用智能路由级联模式（如果廉价模型响应看起来不确定，用主要模型重试）。
    # 默认：true。通过 `SMART_ROUTING_CASCADE` 设置
    smart_routing_cascade: bool = True
    # 瞬态 LLM 错误的最大重试次数。
    # 通过 `LLM_MAX_RETRIES` 设置（回退到 `NEARAI_MAX_RETRIES`）。默认：3
    max_retries: int = 3
    # 断路器打开前的连续失败次数。None = 禁用。
    # 通过 `LLM_CIRCUIT_BREAKER_THRESHOLD` 设置（回退到 `CIRCUIT_BREAKER_THRESHOLD`）
    circuit_breaker_threshold: Optional[int] = None
    # 断路器在探测前保持打开的秒数。默认：30。
    # 通过 `LLM_CIRCUIT_BREAKER_RECOVERY_SECS` 设置（回退到 `CIRCUIT_BREAKER_RECOVERY_SECS`）
    circuit_breaker_recovery_secs: int = 30
    # 启用内存响应缓存。默认：false。
    # 通过 `LLM_RESPONSE_CACHE_ENABLED` 设置（回退到 `RESPONSE_CACHE_ENABLED`）
    response_cache_enabled: bool = False
    # 缓存响应的 TTL 秒数。默认：3600。
    # 通过 `LLM_RESPONSE_CACHE_TTL_SECS` 设置（回退到 `RESPONSE_CACHE_TTL_SECS`）
    response_cache_ttl_secs: int = 3600
    # LRU 驱逐前的最大缓存响应数。默认：1000。
    # 通过 `LLM_RESPONSE_CACHE_MAX_ENTRIES` 设置（回退到 `RESPONSE_CACHE_MAX_ENTRIES`）
    response_cache_max_entries: int = 1000

    def backend_kind(self) -> LlmBackendKind:
        """获取后端类型"""
        return LlmBackendKind.from_backend_id(self.backend)

    def active_provider_id(self) -> str:
        """获取活跃的提供者标识符"""
        return self.backend_kind().provider_id(self.provider)

    def cheap_model_name(self) -> Optional[str]:
        """解析有效的廉价模型名称

        解析顺序：
        1. `LLM_CHEAP_MODEL`（通用，适用于任何后端）
        2. `NEARAI_CHEAP_MODEL`（仅 NearAI，向后兼容）
        """
        if self.cheap_model is not None:
            return self.cheap_model
        if self.backend == "nearai":
            return self.nearai.cheap_model
        return None

    def active_model_name(self) -> str:
        """解析热重载后在状态/UI 中显示的模型名称

        由网关状态处理程序使用，在提供者链被交换时刷新
        `ActiveConfigSnapshot.llm_model`，而不触及活跃的提供者实例
        （例如在第一个请求到达新链之前）
        """
        backend_lower = self.backend.lower()

        if backend_lower in ("nearai", "near_ai", "near"):
            return self.nearai.model
        elif backend_lower in ("bedrock", "aws_bedrock", "aws"):
            if self.bedrock is not None:
                return self.bedrock.model
            return self.nearai.model
        elif backend_lower in ("gemini_oauth", "gemini-oauth"):
            if self.gemini_oauth is not None:
                return self.gemini_oauth.model
            return self.nearai.model
        elif backend_lower in ("openai_codex", "openai-codex", "codex"):
            if self.openai_codex is not None:
                return self.openai_codex.model
            return "gpt-5.5"
        else:
            if self.provider is not None:
                return self.provider.model
            return self.nearai.model