from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any
from bootstrap import ironclaw_base_dir
from pathlib import Path
import json
import tomllib
import logging

logger = logging.getLogger(__name__)


@dataclass
class CustomLlmProviderSettings:
    """
    用户通过 Web UI 定义的自定义 LLM 提供者。
    """
    # 唯一标识符（用作 `llm_backend` 值）
    id: str
    # 显示名称
    name: str
    # 适配器协议："open_ai_completions"、"anthropic"、"ollama"
    adapter: str
    # API 端点的基础 URL
    base_url: Optional[str] = None
    # 默认模型标识符
    default_model: Optional[str] = None
    # 可选的内联存储的 API 密钥
    api_key: Optional[str] = None
    # 是否为内置提供者（对于自定义提供者应始终为 False）
    builtin: bool = False

    def __repr__(self) -> str:
        api_key_display = "[已隐藏]" if self.api_key is not None else None
        return (
            f"CustomLlmProviderSettings(id={self.id!r}, name={self.name!r}, "
            f"adapter={self.adapter!r}, base_url={self.base_url!r}, "
            f"default_model={self.default_model!r}, api_key={api_key_display!r}, "
            f"builtin={self.builtin!r})"
        )


@dataclass
class LlmBuiltinOverride:
    """
    内置 LLM 提供者的每提供者覆盖（API 密钥和/或模型）。

    在设置存储中存储为 `llm_builtin_overrides`，以提供者 ID 为键
    （例如 `"openai"`、`"gemini"`）。在启动时由 `crate::config::llm::resolve()` 解析。

    注意：全局 `selected_model`（如果设置）优先于这些每提供者覆盖，
    而每提供者覆盖又优先于环境变量。
    """
    # API 密钥覆盖。优先于环境变量。
    api_key: Optional[str] = None
    # 模型覆盖。优先于环境变量，但不优先于 `selected_model`。
    model: Optional[str] = None
    # 基础 URL 覆盖。优先于环境变量。
    base_url: Optional[str] = None
    # 每提供者设置包，用于需要 api_key / model / base_url 之外额外字段的
    # 非 OpenAI 形状的后端。示例键：
    #
    # - `bedrock`：`region`、`cross_region`、`profile`
    # - `gemini_oauth`：`credentials_path`
    # - `openai_codex`：（目前无；保留）
    #
    # 设置通过向导的通用 `SetupHint` 分发（C 层）流入此包；
    # `crate::config::llm::resolve` 中的二进制端解析器在组装
    # 每提供者配置结构体时读取它们。
    extras: Dict[str, str] = field(default_factory=dict)

    def extra(self, key: str) -> Optional[str]:
        """按键查找 extras-bag 字段；如果不存在或为空则返回 `None`。"""
        value = self.extras.get(key)
        return value if value else None

    def set_extra(self, key: str, value: str) -> None:
        """设置 extras-bag 字段；当 `value` 为空时清除该条目。"""
        if not value:
            self.extras.pop(key, None)
        else:
            self.extras[key] = value

    def __repr__(self) -> str:
        api_key_display = "[已隐藏]" if self.api_key is not None else None
        return (
            f"LlmBuiltinOverride(api_key={api_key_display!r}, "
            f"model={self.model!r}, base_url={self.base_url!r}, "
            f"extras={self.extras!r})"
        )


def builtin_secret_name(provider_id: str) -> str:
    """内置提供者 API 密钥的规范密钥名称。"""
    return f"llm_builtin_{provider_id}_api_key"


def custom_secret_name(provider_id: str) -> str:
    """自定义提供者 API 密钥的规范密钥名称。"""
    return f"llm_custom_{provider_id}_api_key"


class KeySource(str, Enum):
    """密钥主密钥的来源。"""
    # 自动生成的密钥，存储在操作系统密钥链中
    Keychain = "keychain"
    # 用户通过 SECRETS_MASTER_KEY 环境变量提供
    Env = "env"
    # 未配置（密钥功能已禁用）
    None_ = "none"


@dataclass
class EmbeddingsSettings:
    """嵌入配置。"""
    # 是否启用嵌入
    enabled: bool = False
    # 要使用的提供者："openai" 或 "nearai"
    provider: str = "nearai"
    # 用于嵌入的模型
    model: str = "text-embedding-3-small"


@dataclass
class TunnelSettings:
    """
    公共 webhook 端点的隧道设置。

    隧道 URL 在所有需要 webhook 的通道之间共享。
    两种模式：
    - **静态 URL**：直接设置 `public_url`（手动隧道管理）。
    - **托管提供者**：设置 `provider`，代理在启动/关闭时自动启动/停止
      隧道进程。
    """
    # 来自隧道提供者的公共 URL（例如 "https://abc123.ngrok.io"）。
    # 在没有提供者的情况下设置时，视为静态（外部管理的）URL。
    public_url: Optional[str] = None
    # 托管隧道提供者："ngrok"、"cloudflare"、"tailscale"、"custom"
    provider: Optional[str] = None
    # Cloudflare 隧道令牌
    cf_token: Optional[str] = None
    # ngrok 认证令牌
    ngrok_token: Optional[str] = None
    # ngrok 自定义域名（付费计划）
    ngrok_domain: Optional[str] = None
    # 使用 Tailscale Funnel（公共）而非 Serve（仅限 tailnet）
    ts_funnel: bool = False
    # Tailscale 主机名覆盖
    ts_hostname: Optional[str] = None
    # 自定义隧道的 shell 命令（带有 `{port}` / `{host}` 占位符）
    custom_command: Optional[str] = None
    # 自定义隧道的健康检查 URL
    custom_health_url: Optional[str] = None
    # 从自定义隧道 stdout 中提取 URL 的子字符串模式
    custom_url_pattern: Optional[str] = None


@dataclass
class ChannelSettings:
    """通道特定设置。"""
    # 是否启用 HTTP webhook 通道
    http_enabled: bool = False
    # HTTP webhook 端口（如果启用）
    http_port: Optional[int] = None
    # HTTP webhook 主机
    http_host: Optional[str] = None
    # 是否启用 Web 网关
    gateway_enabled: bool = True
    # Web 网关监听主机
    gateway_host: Optional[str] = None
    # Web 网关监听端口
    gateway_port: Optional[int] = None
    # Web 网关 bearer 认证令牌。如果未设置，在网关启动时自动生成
    gateway_auth_token: Optional[str] = None
    # 是否启用 CLI 通道
    cli_enabled: bool = True
    # 是否启用 Signal 通道
    signal_enabled: bool = False
    # Signal HTTP URL（signal-cli 守护进程端点）
    signal_http_url: Optional[str] = None
    # Signal 账户（E.164 电话号码）
    signal_account: Optional[str] = None
    # Signal DM 允许来源列表（逗号分隔的 E.164 电话号码）。
    # 逗号分隔的标识符：E.164 电话号码、`*`、裸 UUID 或 `uuid:<id>` 条目。
    # 默认为已配置的账户。
    signal_allow_from: Optional[str] = None
    # Signal 群组允许来源（逗号分隔的群组 ID）
    signal_allow_from_groups: Optional[str] = None
    # Signal DM 策略："open"、"allowlist" 或 "pairing"。默认："pairing"
    signal_dm_policy: Optional[str] = None
    # Signal 群组策略："allowlist"、"open" 或 "disabled"。默认："allowlist"
    signal_group_policy: Optional[str] = None
    # Signal 群组允许来源（逗号分隔的群组成员 ID）。
    # 如果为空，则继承自 signal_allow_from。
    signal_group_allow_from: Optional[str] = None
    # 每通道所有者用户 ID。设置后，该通道仅响应此用户。
    # 键：通道名称（例如 "telegram"），值：所有者用户 ID。
    wasm_channel_owner_ids: Dict[str, int] = field(default_factory=dict)
    # WASM 通道的运行时配置覆盖
    #
    # 键使用 `<channel>:<config_key>` 格式（例如
    # `wecom:allow_from`），值作为 JSON 值传递给通道配置。
    wasm_channel_runtime_overrides: Dict[str, Any] = field(default_factory=dict)
    # 按名称启用的 WASM 通道。
    # 主要由设置向导用于跟踪已配置的通道。
    #
    # 启动时将其视为回退恢复源，仅在运行时持久化
    # `activated_channels` 之前使用。
    wasm_channels: List[str] = field(default_factory=list)
    # 是否启用 WASM 通道
    wasm_channels_enabled: bool = True
    # 包含 WASM 通道模块的目录
    wasm_channels_dir: Optional[Path] = None
    # CLI 模式："tui" 表示富终端 UI，空/缺失表示简单 REPL
    cli_mode: Optional[str] = "tui"


@dataclass
class HeartbeatSettings:
    """心跳配置。"""
    # 是否启用心跳
    enabled: bool = False
    # 心跳检查间隔（秒）
    interval_secs: int = 1800
    # 通知心跳发现结果的通道
    notify_channel: Optional[str] = None
    # 通知心跳发现结果的用户 ID
    notify_user: Optional[str] = None
    # 触发的固定时间（HH:MM，24 小时制）。设置后，忽略 interval_secs
    fire_at: Optional[str] = None
    # 静默时间开始的小时（0-23）（跳过心跳）
    quiet_hours_start: Optional[int] = None
    # 静默时间结束的小时（0-23）（恢复心跳）
    quiet_hours_end: Optional[int] = None
    # fire_at 和静默时间的时区（IANA 名称，例如 "Pacific/Auckland"）
    timezone: Optional[str] = None


@dataclass
class AgentSettings:
    """代理行为配置。"""
    # 代理名称
    name: str = "ironclaw"
    # 最大并行作业数
    max_parallel_jobs: int = 5
    # 作业超时（秒）
    job_timeout_secs: int = 3600  # 1 小时
    # 卡住作业的阈值（秒）
    stuck_threshold_secs: int = 300  # 5 分钟
    # 是否在工具执行前使用规划
    use_planning: bool = True
    # 自我修复检查间隔（秒）
    repair_check_interval_secs: int = 60  # 1 分钟
    # 最大修复尝试次数
    max_repair_attempts: int = 3
    # 会话空闲超时（秒）（默认：7 天）。超过此时间不活跃的会话
    # 将从内存中清除
    session_idle_timeout_secs: int = 7 * 24 * 3600  # 7 天
    # 每次代理循环调用的最大工具调用迭代次数（默认：50）
    max_tool_iterations: int = 50
    # 当为 True 时，完全跳过工具批准检查。用于基准测试/CI
    auto_approve_tools: bool = False
    # 新会话的默认时区（IANA 名称，例如 "America/New_York"）
    default_timezone: str = "UTC"
    # 每个作业的最大令牌数（0 = 无限制）
    max_tokens_per_job: int = 0


@dataclass
class WasmSettings:
    """WASM 沙箱配置。"""
    # 是否启用 WASM 工具执行
    enabled: bool = True
    # 包含已安装 WASM 工具的目录
    tools_dir: Optional[Path] = None
    # 默认内存限制（字节）
    default_memory_limit: int = 10 * 1024 * 1024  # 10 MB
    # 默认执行超时（秒）
    default_timeout_secs: int = 60
    # CPU 计量的默认燃料限制
    default_fuel_limit: int = 500_000_000
    # 是否缓存已编译的模块
    cache_compiled: bool = True
    # 已编译模块缓存的目录
    cache_dir: Optional[Path] = None


@dataclass
class SandboxSettings:
    """Docker 沙箱配置。"""
    # 是否启用 Docker 沙箱
    enabled: bool = True
    # 沙箱策略："readonly"、"workspace_write" 或 "full_access"
    policy: str = "readonly"
    # 命令超时（秒）
    timeout_secs: int = 120
    # 内存限制（兆字节）
    memory_limit_mb: int = 2048
    # CPU 份额（相对权重）
    cpu_shares: int = 1024
    # 沙箱的 Docker 镜像
    image: str = "ironclaw-worker:latest"
    # 如果未找到镜像，是否自动拉取
    auto_pull_image: bool = True
    # 通过网络代理允许的额外域名
    extra_allowed_domains: List[str] = field(default_factory=list)
    # 是否启用 Claude Code 沙箱模式
    claude_code_enabled: bool = False
    # 是否启用 ACP（代理客户端协议）代理模式
    acp_enabled: bool = False



@dataclass
class SafetySettings:
    """安全配置。"""
    # 最大输出长度（字节）
    max_output_length: int = 100_000
    # 是否启用注入检查
    injection_check_enabled: bool = True


@dataclass
class BuilderSettings:
    """构建器配置。"""
    # 是否启用软件构建器工具
    enabled: bool = True
    # 构建产物的目录
    build_dir: Optional[Path] = None
    # 构建循环的最大迭代次数
    max_iterations: int = 20
    # 构建超时（秒）
    timeout_secs: int = 600
    # 是否自动注册构建的 WASM 工具
    auto_register: bool = True


@dataclass
class RoutineSettings:
    """例行任务调度和执行配置。"""
    # 是否启用例行任务系统
    enabled: bool = True
    # 轮询需要触发的 cron 例行任务的频率（秒）
    cron_check_interval_secs: int = 15
    # 最大并发执行的例行任务数
    max_concurrent_routines: int = 10
    # 触发之间的默认冷却时间（秒）
    default_cooldown_secs: int = 300
    # 轻量级例行任务 LLM 调用的最大输出令牌数
    max_lightweight_tokens: int = 4096
    # 在轻量级例行任务中启用工具执行
    lightweight_tools_enabled: bool = True
    # 轻量级例行任务的最大工具迭代次数
    lightweight_max_iterations: int = 3



@dataclass
class SkillsSettings:
    """技能系统配置。"""
    # 是否启用技能系统
    enabled: bool = True
    # 可以同时处于活动状态的最大技能数量
    max_active_skills: int = 3
    # 分配给技能提示的最大总上下文令牌数
    max_context_tokens: int = 4000
    # 正则表达式激活条件是否可以自动加载技能
    regex_activation_enabled: bool = True


@dataclass
class HygieneSettings:
    """内存清理配置。"""
    # 是否启用清理
    enabled: bool = True
    # 已弃用：保留期现在通过 `.config` 元数据按文件夹设置。
    # 保留此字段以兼容现有数据库设置行。
    daily_retention_days: int = 30
    # 已弃用：保留期现在通过 `.config` 元数据按文件夹设置。
    # 保留此字段以兼容现有数据库设置行。
    conversation_retention_days: int = 7
    # 清理过程中每个文档保留的最大版本数
    version_keep_count: int = 50
    # 清理过程之间的最小小时数
    cadence_hours: int = 12


@dataclass
class SearchSettings:
    """工作区搜索融合配置。"""
    # 融合策略："rrf" 或 "weighted"
    fusion_strategy: str = "rrf"
    # RRF 常量 k
    rrf_k: int = 60
    # 融合的 FTS 权重。`None` = 使用每种策略的默认值
    fts_weight: Optional[float] = None
    # 融合的向量权重。`None` = 使用每种策略的默认值
    vector_weight: Optional[float] = None
    # 是否为内存搜索启用推理增强的召回。
    # `None` = 使用环境变量/默认值（false）
    reasoning_enabled: Optional[bool] = None



@dataclass
class MissionSettings:
    """任务相关设置。"""
    # 对话洞察提取间隔（每完成 N 个线程）。
    # `None` = 使用环境变量/默认值（5）。最小值：1
    insights_interval: Optional[int] = None


@dataclass
class TranscriptionSettings:
    """转录管道设置。"""
    # 是否启用音频转录
    enabled: bool = False


@dataclass
class Settings:
    """持久化到磁盘的用户设置。"""
    # 引导向导是否已完成
    onboard_completed: bool = False

    # 此 IronClaw 实例的稳定所有者范围
    #
    # 这是从 env / disk / TOML 加载的引导配置。我们不将其持久化到
    # 每用户数据库设置表中，因为数据库查找本身就需要已知所有者范围。
    owner_id: Optional[str] = None

    # === 步骤 1：数据库 ===
    # 数据库后端："postgres" 或 "libsql"
    database_backend: Optional[str] = None
    # 数据库连接 URL（postgres://...）
    database_url: Optional[str] = None
    # 数据库连接池大小
    database_pool_size: Optional[int] = None
    # 本地 libSQL 数据库文件的路径
    libsql_path: Optional[str] = None
    # 用于远程副本同步的 Turso 云 URL
    libsql_url: Optional[str] = None

    # === 步骤 2：安全性 ===
    # 密钥主密钥的来源
    secrets_master_key_source: KeySource = KeySource.None_
    # 生成的主密钥十六进制（仅限 env var 模式，由向导写入 .env）
    secrets_master_key_hex: Optional[str] = None

    # === 步骤 3：推理提供者 ===
    # LLM 后端："nearai"、"anthropic"、"openai"、"github_copilot"、"ollama"、
    # "openai_compatible"、"tinfoil"、"bedrock"
    llm_backend: Optional[str] = None
    # 用户通过 Web UI 定义的自定义 LLM 提供者
    llm_custom_providers: List[CustomLlmProviderSettings] = field(default_factory=list)
    # 内置提供者的每提供者覆盖（API 密钥和/或模型）
    llm_builtin_overrides: Dict[str, LlmBuiltinOverride] = field(default_factory=dict)
    # Ollama 基础 URL（当 llm_backend = "ollama" 时）
    ollama_base_url: Optional[str] = None
    # OpenAI 兼容端点基础 URL（当 llm_backend = "openai_compatible" 时）
    openai_compatible_base_url: Optional[str] = None

    # **已弃用。** Bedrock 区域——已移至 D 层中的
    # `llm_builtin_overrides["bedrock"].extras["region"]`。
    # 现有值在加载时通过 [`Settings.migrate_legacy_provider_fields`] 迁移；
    # 新代码必须改为从 extras 包读取/写入。保留一个版本，
    # 以便从旧 settings.json 文件升级的用户不会丢失数据。
    bedrock_region: Optional[str] = None
    # **已弃用。** Bedrock 跨区域推理前缀——已移至
    # `llm_builtin_overrides["bedrock"].extras["cross_region"]`。
    bedrock_cross_region: Optional[str] = None
    # **已弃用。** Bedrock 的 AWS 配置文件名称——已移至
    # `llm_builtin_overrides["bedrock"].extras["profile"]`。
    bedrock_profile: Optional[str] = None

    # === 步骤 4：模型选择 ===
    # 当前选定的模型
    selected_model: Optional[str] = None
    # LLM 请求的默认采样温度（0.0–2.0）。
    # 设置后，用作对话轮次的默认值。
    # 每次请求的温度（例如来自 API 的）优先。
    temperature: Optional[float] = None

    # === 步骤 5：嵌入 ===
    # 嵌入配置
    embeddings: EmbeddingsSettings = field(default_factory=EmbeddingsSettings)

    # === 步骤 6：通道 ===
    # 公共 webhook 端点的隧道配置
    tunnel: TunnelSettings = field(default_factory=TunnelSettings)
    # 通道配置
    channels: ChannelSettings = field(default_factory=ChannelSettings)

    # === 步骤 7：心跳 ===
    # 心跳配置
    heartbeat: HeartbeatSettings = field(default_factory=HeartbeatSettings)

    # === 对话配置文件引导 ===
    # 对话配置文件引导是否已完成
    #
    # 在用户与运行中的助手首次交互期间设置（而非在设置向导期间），
    # 在代理通过 `memory_write` 构建心理画像配置文件之后。
    # 由代理循环（通过工作区系统提示连接）使用，以在引导完成后
    # 抑制 BOOTSTRAP.md 注入。
    profile_onboarding_completed: bool = False

    # === 高级设置（设置期间不询问，可通过 CLI 编辑） ===
    # 代理行为配置
    agent: AgentSettings = field(default_factory=AgentSettings)
    # WASM 沙箱配置
    wasm: WasmSettings = field(default_factory=WasmSettings)
    # Docker 沙箱配置
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)
    # 安全配置
    safety: SafetySettings = field(default_factory=SafetySettings)
    # 构建器配置
    builder: BuilderSettings = field(default_factory=BuilderSettings)
    # 例行任务调度和执行配置
    routines: RoutineSettings = field(default_factory=RoutineSettings)
    # 技能系统配置
    skills: SkillsSettings = field(default_factory=SkillsSettings)
    # 内存清理配置
    hygiene: HygieneSettings = field(default_factory=HygieneSettings)
    # 工作区搜索融合配置
    search: SearchSettings = field(default_factory=SearchSettings)
    # 任务配置
    missions: MissionSettings = field(default_factory=MissionSettings)
    # 转录配置
    transcription: Optional[TranscriptionSettings] = None
    # 每工具权限覆盖
    #
    # 键是工具名称；持久化的值是权威的。缺失的工具
    # 回退到知名工具的种子默认值，然后回退到 `AskEachTime`。
    tool_permissions: Dict[str, "PermissionState"] = field(default_factory=dict)

    @classmethod
    def from_db_map(cls, map_data: Dict[str, Any]) -> "Settings":
        """
        从扁平键值映射（存储在数据库中）重建 Settings。

        每个键是一个点分隔路径（例如 "agent.name"），值是一个 JSONB 值。
        缺失的键使用其默认值。
        """
        # 从默认值开始，然后覆盖每个数据库设置。
        #
        # 设置表同时存储 Settings 结构体字段和应用特定数据
        # （例如 nearai.session_token）。跳过不对应于已知 Settings 路径的键。
        settings = cls()

        for key, value in map_data.items():
            if key == "owner_id":
                continue

            # 将 JSONB 值转换为字符串用于现有的 set() 方法
            if value is None:
                continue  # null 表示默认值，跳过
            elif isinstance(value, bool):
                value_str = str(value).lower()
            elif isinstance(value, (int, float)):
                value_str = str(value)
            elif isinstance(value, str):
                value_str = value
            else:
                value_str = json.dumps(value)

            try:
                settings.set(key, value_str)
            except Exception as e:
                error_msg = str(e)
                # 设置表同时存储 Settings 字段和应用特定数据
                # （例如 nearai.session_token）。静默跳过未知路径。
                if "路径未找到" in error_msg:
                    pass
                else:
                    logger.warning(
                        "无法应用数据库设置 '%s' = '%s': %s",
                        key,
                        value_str,
                        e,
                    )

        # 与 JSON 磁盘加载时运行的迁移相同——在 D 层之前写入的数据库行
        # 仍然携带命名的 `bedrock_*` 列，而解析器现在仅从
        # `llm_builtin_overrides["bedrock"].extras` 读取。
        # 如果没有此调用，现有的基于数据库的操作员在升级后
        # 将静默丢失其 bedrock region/profile/cross-region。
        settings.migrate_legacy_provider_fields()
        return settings

    def to_db_map(self) -> Dict[str, Any]:
        """
        将 Settings 展平为适合数据库存储的键值映射。

        每个条目是一个 (点分隔路径, JSONB 值) 对。
        """
        try:
            json_data = json.loads(json.dumps(self, default=lambda o: o.__dict__))
        except Exception:
            return {}

        result = {}
        _collect_settings_json(json_data, "", result)
        result.pop("owner_id", None)
        return result

    @staticmethod
    def default_path() -> Path:
        """获取默认设置文件路径 (~/.ironclaw/settings.json)。"""
        return ironclaw_base_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        """从磁盘加载设置，如果未找到则返回默认值。"""
        return cls.load_from(cls.default_path())

    @classmethod
    def load_from(cls, path: Path) -> "Settings":
        """从特定路径加载设置（用于引导遗留迁移）。"""
        try:
            data = path.read_text()
            settings = cls(**json.loads(data))
        except Exception:
            settings = cls()
        settings.migrate_legacy_provider_fields()
        return settings

    def migrate_legacy_provider_fields(self) -> None:
        """
        将遗留的命名每提供者字段移入通用的
        `llm_builtin_overrides[<id>].extras` 包。

        D 层将 bedrock 特定配置从命名列
        (`bedrock_region`、`bedrock_cross_region`、`bedrock_profile`) 中移出，
        放入每提供者设置包中，以便添加新的非 OpenAI 形状的后端
        不需要新的 `Settings` 列。旧的持久化
        settings.json / config.toml 文件仍然携带命名字段；
        此辅助函数在加载时将它们合并到 `extras` 中一次，并清除
        原始字段，以便后续保存只写入新的形状。

        现有的 extras 值优先于遗留字段。以某种方式同时携带
        两种形状的文件（手动编辑，或未来同时发出两者的写入器）
        保留新形状的值，而不是被遗留值静默覆盖。

        幂等：重新运行是无操作的，因为命名字段已被清空。
        """
        region = self.bedrock_region
        cross_region = self.bedrock_cross_region
        profile = self.bedrock_profile

        if region is not None or cross_region is not None or profile is not None:
            if "bedrock" not in self.llm_builtin_overrides:
                self.llm_builtin_overrides["bedrock"] = LlmBuiltinOverride()

            entry = self.llm_builtin_overrides["bedrock"]

            if region is not None and entry.extra("region") is None:
                entry.set_extra("region", region)
                self.bedrock_region = None

            if cross_region is not None and entry.extra("cross_region") is None:
                entry.set_extra("cross_region", cross_region)
                self.bedrock_cross_region = None

            if profile is not None and entry.extra("profile") is None:
                entry.set_extra("profile", profile)
                self.bedrock_profile = None

    @staticmethod
    def default_toml_path() -> Path:
        """默认 TOML 配置文件路径 (~/.ironclaw/config.toml)。"""
        return ironclaw_base_dir() / "config.toml"

    @classmethod
    def load_toml(cls, path: Path) -> Optional["Settings"]:
        """
        从 TOML 文件加载设置。

        如果文件不存在，返回 `None`。仅当文件存在但无法解析时才返回错误。
        """
        try:
            data = path.read_text()
        except FileNotFoundError:
            return None
        except Exception as e:
            raise ValueError(f"无法读取 {path}: {e}")

        try:
            raw_settings = toml.loads(data)
        except Exception as e:
            raise ValueError(f"{path} 中的 TOML 无效: {e}")

        settings = cls()
        for key, value in raw_settings.items():
            if hasattr(settings, key):
                setattr(settings, key, value)

        # 与 JSON 磁盘加载和数据库重建时运行的迁移相同——
        # 在 D 层之前写入的 TOML 文件仍然在顶层携带命名的
        # `bedrock_*` 键，而解析器现在仅从
        # `llm_builtin_overrides["bedrock"].extras` 读取。
        settings.migrate_legacy_provider_fields()
        return settings

    def save_toml(self, path: Path) -> None:
        """将当前设置写入带有良好注释的 TOML 配置文件。"""
        try:
            raw = toml.dumps(self.__dict__)
        except Exception as e:
            raise ValueError(f"序列化设置失败: {e}")

        content = (
            "# IronClaw 配置文件。\n"
            "#\n"
            "# 优先级：DB settings > env vars > this file > defaults。\n"
            "# 等于内置默认值的数据库值被视为未设置。\n"
            "# 例外：引导和安全敏感字段仅限环境变量。\n"
            "# 取消注释并编辑值以覆盖默认值。\n"
            "# 运行 `ironclaw config init` 重新生成此文件。\n"
            "#\n"
            "# 文档：https://github.com/nearai/ironclaw\n"
            "\n"
            f"{raw}"
        )

        parent = path.parent
        if parent:
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                raise ValueError(f"无法创建 {parent}: {e}")

        try:
            path.write_text(content)
        except Exception as e:
            raise ValueError(f"无法写入 {path}: {e}")

    def merge_from(self, other: "Settings") -> None:
        """
        将 `other` 中的值合并到 `self` 中，对于与默认值不同的字段，
        优先使用 `other`。

        这启用了分层：加载数据库/JSON 设置作为基础，然后将
        TOML 值覆盖在上面。仅应用 TOML 文件中显式更改的字段
        （即与默认值不同的字段）。
        """
        import copy

        default = Settings()
        default_dict = default.__dict__
        other_dict = {k: v for k, v in other.__dict__.items() if not k.startswith('_')}
        self_dict = {k: v for k, v in self.__dict__.items() if not k.startswith('_')}

        def _merge_non_default(target_val: Any, other_val: Any, default_val: Any) -> Any:
            """仅当 other 与默认值不同时才合并。"""
            if isinstance(target_val, dict) and isinstance(other_val, dict) and isinstance(default_val, dict):
                result = copy.deepcopy(target_val)
                for key, other_v in other_val.items():
                    default_v = default_val.get(key)
                    if key in result:
                        result[key] = _merge_non_default(result[key], other_v, default_v)
                    elif other_v != default_v:
                        result[key] = copy.deepcopy(other_v)
                return result
            else:
                return copy.deepcopy(other_val) if other_val != default_val else target_val

        merged = _merge_non_default(self_dict, other_dict, default_dict)
        for key, value in merged.items():
            setattr(self, key, value)

    def get(self, path: str) -> Optional[str]:
        """通过点分隔路径获取设置值（例如 "agent.max_parallel_jobs"）。"""
        parts = path.split('.')
        current = self.__dict__

        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                return None

        if current is None:
            return "null"
        elif isinstance(current, bool):
            return str(current).lower()
        elif isinstance(current, (int, float)):
            return str(current)
        elif isinstance(current, str):
            return current
        elif isinstance(current, (list, dict)):
            return json.dumps(current)
        else:
            return str(current)

    def set(self, path: str, value: str) -> None:
        """
        通过点分隔路径设置设置值。

        如果路径无效或值无法解析，则抛出错误。
        """
        parts = path.split('.')
        if not parts:
            raise ValueError("路径为空")

        # 导航到父对象
        current = self
        for part in parts[:-1]:
            if isinstance(current, dict):
                current = current.get(part)
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                raise ValueError(f"路径未找到: {path}")

            if current is None:
                raise ValueError(f"路径未找到: {path}")

        final_key = parts[-1]

        # 获取现有值以推断类型
        if isinstance(current, dict):
            existing = current.get(final_key)
        elif hasattr(current, final_key):
            existing = getattr(current, final_key)
        else:
            existing = None

        # 尝试从现有值推断类型
        if existing is not None:
            if isinstance(existing, bool):
                new_value = value.lower() in ('true', '1', 'yes')
            elif isinstance(existing, int):
                try:
                    new_value = int(value)
                except ValueError:
                    raise ValueError(f"期望 {path} 为整数，得到 '{value}'")
            elif isinstance(existing, float):
                try:
                    new_value = float(value)
                except ValueError:
                    raise ValueError(f"期望 {path} 为数字，得到 '{value}'")
            elif isinstance(existing, list):
                try:
                    new_value = json.loads(value)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{path} 的 JSON 数组无效: {e}")
            elif isinstance(existing, dict):
                try:
                    new_value = json.loads(value)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{path} 的 JSON 对象无效: {e}")
            else:
                new_value = value
        else:
            # 键不存在，尝试解析为 JSON 或使用字符串
            try:
                new_value = json.loads(value)
            except json.JSONDecodeError:
                new_value = value

        # 设置值
        if isinstance(current, dict):
            current[final_key] = new_value
        else:
            setattr(current, final_key, new_value)

    def reset(self, path: str) -> None:
        """将设置重置为其默认值。"""
        default = Settings()
        default_value = default.get(path)
        if default_value is None:
            raise ValueError(f"未知设置: {path}")

        self.set(path, default_value)

    def list(self) -> List[Tuple[str, str]]:
        """将所有设置列出为 (路径, 值) 对。"""
        results: List[Tuple[str, str]] = []
        _collect_settings(self.__dict__, "", results)
        results.sort(key=lambda x: x[0])
        return results


# ── 辅助函数 ──

def _collect_settings_json(
    value: Any,
    prefix: str,
    results: Dict[str, Any],
) -> None:
    """递归收集设置路径及其 JSON 值（用于数据库存储）。"""
    if isinstance(value, dict):
        for key, val in value.items():
            path = f"{prefix}.{key}" if prefix else key
            _collect_settings_json(val, path, results)
    else:
        results[prefix] = value


def _collect_settings(
    value: Any,
    prefix: str,
    results: List[Tuple[str, str]],
) -> None:
    """递归收集设置路径和值。"""
    if isinstance(value, dict):
        for key, val in value.items():
            path = f"{prefix}.{key}" if prefix else key
            _collect_settings(val, path, results)
    elif isinstance(value, list):
        display = json.dumps(value)
        results.append((prefix, display))
    elif isinstance(value, str):
        results.append((prefix, value))
    elif isinstance(value, bool):
        results.append((prefix, str(value).lower()))
    elif isinstance(value, (int, float)):
        results.append((prefix, str(value)))
    elif value is None:
        results.append((prefix, "null"))