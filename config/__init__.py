from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, List
from dotenv import load_dotenv
from bootstrap import load_ironclaw_env


logger = logging.getLogger(__name__)

# ── 注入环境变量的线程安全覆盖层（从数据库加载的密钥）────────────────

# 被 `inject_llm_keys_from_secrets()` 使用，使 API 密钥对
# `optional_env()` 可用，而无需不安全的 `set_var` 调用。
# `optional_env()` 首先检查真实的环境变量，然后回退到此覆盖层。
#
# 使用 `Mutex<HashMap>` 而非单次锁，以便
# `inject_os_credentials()` 和 `inject_llm_keys_from_secrets()` 都可以
# 合并它们的数据。先运行的那个初始化映射；第二个合并进来。
_INJECTED_VARS: Dict[str, str] = {}
_INJECTED_VARS_LOCK = Lock()

_WARNED_EXPLICIT_DEFAULT_OWNER_ID: bool = False


def _generate_test_master_key() -> str:
    """
    为 `Config.for_testing` 生成一个新的随机 AES-256-GCM 主密钥。

    返回一个十六进制编码的 32 字节密钥（64 个十六进制字符），满足
    `SecretsConfig.resolve` 中的长度检查。每次调用返回不同的值——
    测试不需要跨进程确定性（每个测试在全新的临时数据库之上构建全新的
    密钥存储），并且将常量主密钥提交到源代码树中意味着每个使用
    `--features libsql` 构建的开发者在其进程中都有一个公开已知的密钥。
    """
    import secrets
    return secrets.token_hex(32)


@dataclass
class Config:
    """代理的主配置。"""
    owner_id: str
    database: DatabaseConfig
    llm: LlmConfig
    embeddings: EmbeddingsConfig
    tunnel: TunnelConfig
    channels: ChannelsConfig
    agent: AgentConfig
    safety: SafetyConfig
    wasm: WasmConfig
    secrets: SecretsConfig
    builder: BuilderModeConfig
    heartbeat: HeartbeatConfig
    hygiene: HygieneConfig
    routines: RoutineConfig
    # 已解析的运行时配置文件/部署模式策略。PR 5+ 规划器集成的真实来源
    # （要暴露哪些后端，要应用什么批准姿态）。目前此字段已连接但尚未
    # 被现有的后端选择站点使用。
    runtime: RuntimeConfig
    sandbox: SandboxModeConfig
    claude_code: ClaudeCodeConfig
    acp: AcpModeConfig
    skills: SkillsConfig
    transcription: TranscriptionConfig
    search: WorkspaceSearchConfig
    missions: MissionsConfig
    workspace: WorkspaceConfig
    observability: ObservabilityConfig
    # OAuth/社交登录配置（Google、GitHub 等）。
    oauth: OAuthConfig
    # 通道中继集成（通过外部中继服务的 Slack）。
    # 仅当同时设置了 `CHANNEL_RELAY_URL` 和 `CHANNEL_RELAY_API_KEY` 时才存在。
    relay: Optional[RelayConfig] = None

    def is_multi_tenant_deployment(self) -> bool:
        """
        返回此部署是否配置为以多租户模式运行。

        保持此决策由配置驱动，而不是从运行时数据库内容推断。
        在创建任何非所有者用户之前，部署可以显式地是多租户的。
        """
        return self.agent.multi_tenant

    @classmethod
    def for_testing(
        cls,
        libsql_path: Path,
        skills_dir: Path,
        installed_skills_dir: Path,
    ) -> "Config":
        """
        为集成测试创建一个完整的 Config，不读取环境变量。

        设置如下：
        - 在给定路径使用 libSQL 数据库
        - WASM 和嵌入禁用
        - 使用给定的目录启用技能
        - 心跳、例行任务、沙箱、构建器全部禁用
        - 安全设置中注入检查关闭，输出限制为 100k
        """
        return cls(
            owner_id="default",
            database=DatabaseConfig(
                backend=DatabaseBackend.LibSql,
                url="unused://test",
                pool_size=1,
                ssl_mode=SslMode.Disable,
                libsql_path=libsql_path,
                libsql_url=None,
                libsql_auth_token=None,
            ),
            llm=llm.for_testing(),
            embeddings=EmbeddingsConfig.default(),
            tunnel=TunnelConfig.default(),
            channels=ChannelsConfig(
                cli=CliConfig(enabled=False),
                http=None,
                gateway=None,
                signal=None,
                tui=None,
                wasm_channels_dir=Path(tempfile.gettempdir()) / "ironclaw-test-channels",
                wasm_channels_enabled=False,
                configured_wasm_channels=[],
                wasm_channel_owner_ids={},
                reborn_telegram_v2_enabled=False,
                wasm_channel_runtime_overrides={},
            ),
            agent=AgentConfig.for_testing(),
            safety=SafetyConfig(
                max_output_length=100_000,
                injection_check_enabled=False,
            ),
            wasm=WasmConfig(
                enabled=False,
                **WasmConfig.default().__dict__,
            ),
            # 测试配置获得一个全新生成的随机主密钥，以便密钥存储开箱即用。
            # 没有这个，每个涉及凭证的重放模式测试都必须构建自己的
            # SecretsStore 或完全跳过密钥路径。密钥每次调用生成（不是
            # 硬编码常量）——`Config.for_testing` 是 `pub` 的，所以 crate
            # 内或下游测试中的任何内容都可以调用它，并且将已知主密钥提交到
            # 源代码树中意味着每个使用 `--features libsql` 构建的开发者
            # 在其进程中都有一个公开已知的 AES-256-GCM 密钥。
            # 测试在这里不需要跨进程确定性：每个测试创建自己的临时数据库，
            # 所以密钥存储每次调用都是全新的。
            secrets=SecretsConfig(
                master_key=_generate_test_master_key(),
                enabled=True,
                source=KeySource.Env,
                generated=False,
            ),
            builder=BuilderModeConfig(
                enabled=False,
                **BuilderModeConfig.default().__dict__,
            ),
            heartbeat=HeartbeatConfig.default(),
            hygiene=HygieneConfig.default(),
            routines=RoutineConfig(
                enabled=False,
                **RoutineConfig.default().__dict__,
            ),
            runtime=RuntimeConfig.safe_default(),
            sandbox=SandboxModeConfig(
                enabled=False,
                **SandboxModeConfig.default().__dict__,
            ),
            claude_code=ClaudeCodeConfig.default(),
            acp=AcpModeConfig.default(),
            skills=SkillsConfig(
                enabled=True,
                local_dir=skills_dir,
                installed_dir=installed_skills_dir,
                **SkillsConfig.default().__dict__,
            ),
            transcription=TranscriptionConfig.default(),
            search=WorkspaceSearchConfig.default(),
            missions=MissionsConfig.default(),
            workspace=WorkspaceConfig.default(),
            observability=ObservabilityConfig.default(),
            oauth=OAuthConfig.default(),
            relay=None,
        )

    @classmethod
    async def from_db(
        cls,
        store: "SettingsStore",
        user_id: str,
    ) -> "Config":
        """
        从环境变量和数据库加载配置。

        优先级：DB/TOML > env > default。首先加载 TOML 作为基础，
        然后将数据库值合并到顶部。子系统解析器在环境变量之前检查
        合并后的设置（引导/安全字段除外）。
        """
        # 现有的调用点传递工作区 owner_id，即操作员/管理员范围。
        return await cls.from_db_with_toml(store, user_id, None, True)

    @classmethod
    async def from_db_with_toml(
        cls,
        store: "SettingsStore",
        user_id: str,
        toml_path: Optional[Path],
        is_operator: bool,
    ) -> "Config":
        """
        从数据库加载，可选地覆盖 TOML 配置文件。

        优先级：DB/TOML > env > default。TOML 作为基础加载，
        然后将数据库值合并到顶部。例外情况参见模块文档。

        `is_operator` 控制仅限管理员的 LLM 设置键的纵深防御过滤
        （`llm_builtin_overrides`、`llm_custom_providers`、
        `ollama_base_url`、`openai_compatible_base_url`）。当为 `False` 时，
        这些键会从数据库覆盖层中剥离，以便非管理员用户（或预先存在的
        遗留数据库行）无法通过每用户设置重新激活私有/回环提供者端点。
        """
        dotenvy.load_dotenv()
        bootstrap.load_ironclaw_env()

        settings = await cls._load_db_backed_settings(
            store, user_id, toml_path, is_operator, False
        )
        return await cls._build(settings)

    @classmethod
    async def from_env(cls) -> "Config":
        """
        仅从环境变量加载配置（无数据库）。

        在数据库连接之前的早期启动期间使用，以及由没有数据库访问权限的
        CLI 命令使用。如果磁盘上存在，则回退到遗留的 `settings.json`。

        通过 dotenvy 加载 `./.env`（标准，更高优先级）和
        `~/.ironclaw/.env`（较低优先级），dotenvy 永远不会覆盖现有变量。
        """
        return await cls.from_env_with_toml(None)

    def with_runtime_overrides(
        self,
        overrides: RuntimeConfigOverrides,
    ) -> "Config":
        """
        使用叠加在 `Self.build` 已执行的基于 env 的解析之上的 CLI 覆盖
        重新解析 [`RuntimeConfig`]。根据 #3045 的 PR 3，CLI 标志优先于
        环境变量。

        如果新的 `(deployment, profile)` 对被拒绝，则返回解析器的类型化错误——
        例如 `--deployment-mode hosted_multi_tenant --runtime-profile local_dev`
        将安全关闭。
        """
        if (
            overrides.deployment is None
            and overrides.profile is None
            and overrides.yolo_disclosure_acknowledged is None
        ):
            return self
        # 对于 CLI 未设置的任何字段，解析器重新读取环境变量，
        # 因此即使仅覆盖三个字段中的一个，
        # `RuntimeConfig.resolve_from` 也能正确返回
        #   CLI > env > default
        self.runtime = RuntimeConfig.resolve_from(overrides)
        return self

    @classmethod
    async def from_env_with_toml(
        cls,
        toml_path: Optional[Path],
    ) -> "Config":
        """从环境变量加载，可选地覆盖 TOML 配置文件。"""
        settings = load_bootstrap_settings(toml_path)
        return await cls._build(settings)

    @staticmethod
    def _apply_toml_overlay(
        settings: "Settings",
        explicit_path: Optional[Path],
    ) -> None:
        """
        加载 TOML 配置文件并将其合并到设置中。

        如果 `explicit_path` 为某个路径，则从该路径加载（错误是致命的）。
        如果为 `None`，则尝试默认路径 `~/.ironclaw/config.toml`
        （文件缺失时静默忽略）。
        """
        path = explicit_path if explicit_path is not None else Settings.default_toml_path()

        try:
            toml_settings = Settings.load_toml(path)
            if toml_settings is not None:
                settings.merge_from(toml_settings)
                logger.debug("已从 %s 加载 TOML 配置", path)
            elif explicit_path is not None:
                raise ConfigError(f"配置文件未找到: {path}")
        except Exception as e:
            if explicit_path is not None:
                raise ConfigError(f"加载配置文件失败 {path}: {e}")
            logger.warning("加载默认配置文件失败: %s", e)

    async def re_resolve_llm(
        self,
        store: Optional["SettingsStore"],
        user_id: str,
        toml_path: Optional[Path],
    ) -> None:
        """
        在凭证注入后仅重新解析 LLM 配置。

        由 `AppBuilder.init_secrets()` 在将 API 密钥注入环境覆盖层之后调用。
        仅重建 `self.llm`——所有其他配置字段不受影响，
        保留初始配置加载（或测试模式下的 `Config.for_testing()`）的值。
        """
        is_operator = user_id == self.owner_id
        await self._re_resolve_llm_with_secrets(
            store, user_id, toml_path, None, is_operator
        )

    async def _re_resolve_llm_with_secrets(
        self,
        store: Optional["SettingsStore"],
        user_id: str,
        toml_path: Optional[Path],
        secrets_store: Optional["SecretsStore"],
        is_operator: bool,
    ) -> None:
        """
        重新解析 LLM 配置，从密钥存储中水合 API 密钥。

        `is_operator` 控制仅限管理员的 LLM 设置键的纵深防御过滤；
        详情参见 [`Config.from_db_with_toml`]。
        """
        self.llm = await Config._resolve_llm_with_secrets(
            store, user_id, toml_path, secrets_store, is_operator
        )

    @classmethod
    async def _load_db_backed_settings(
        cls,
        store: "SettingsStore",
        user_id: str,
        toml_path: Optional[Path],
        is_operator: bool,
        strict_db_reads: bool,
    ) -> "Settings":
        """
        构建用于数据库支持配置读取的设置覆盖层。

        解析顺序是 profile -> TOML -> admin DB -> per-user DB。
        这在完整配置加载和仅 LLM 热重载之间共享，以便它们读取相同的
        所有者/管理员范围，而无需重复合并逻辑。
        """
        settings = Settings.default()
        profile.apply_profile(settings)
        cls._apply_toml_overlay(settings, toml_path)

        admin_scope = ADMIN_SETTINGS_USER_ID
        if user_id != admin_scope:
            try:
                admin_map = await store.get_all_settings(admin_scope)
                if admin_map:
                    if not is_operator:
                        helpers.strip_admin_only_llm_keys(admin_map)
                    admin_settings = Settings.from_db_map(admin_map)
                    settings.merge_from(admin_settings)
            except Exception as e:
                if strict_db_reads:
                    raise ConfigError(f"从数据库加载管理员范围设置失败: {e}")
                logger.warning("从数据库加载管理员范围设置失败，使用默认值: %s", e)

        try:
            user_map = await store.get_all_settings(user_id)
            if not is_operator:
                helpers.strip_admin_only_llm_keys(user_map)
            db_settings = Settings.from_db_map(user_map)
            settings.merge_from(db_settings)
        except Exception as e:
            if strict_db_reads:
                raise ConfigError(f"从数据库加载设置失败: {e}")
            logger.warning("从数据库加载设置失败，使用默认值: %s", e)

        return settings

    @classmethod
    async def _resolve_llm_with_secrets_inner(
        cls,
        store: Optional["SettingsStore"],
        user_id: str,
        toml_path: Optional[Path],
        secrets_store: Optional["SecretsStore"],
        is_operator: bool,
        strict_db_reads: bool,
    ) -> LlmConfig:
        """解析 LLM 配置的内部实现。"""
        if store is not None:
            settings = await cls._load_db_backed_settings(
                store, user_id, toml_path, is_operator, strict_db_reads
            )
        else:
            settings = Settings.default()
            profile.apply_profile(settings)
            cls._apply_toml_overlay(settings, toml_path)

        if secrets_store is not None:
            await hydrate_llm_keys_from_secrets(settings, secrets_store, user_id)

        # 启动路径（非严格）：如果用户配置的后端不可用，则回退到 NearAI。
        # 这可以防止 #2514 崩溃循环，并在用户修复其提供者配置时
        # 保持实例可运行。
        #
        # 回退仅在内存中——用户的数据库持久化的
        # `llm_backend` 和 `selected_model` 被有意保留不变，
        # 因此瞬时的水合失败（数据库读取竞争、密钥解密问题）
        # 不会在下一次重启时破坏其配置的提供者（#3229）。
        # 之前将回退同步到数据库的行为将一次性回退变成了永久降级。
        #
        # 热重载路径（严格）：使用纯 `resolve`，以便错误的保存使整个调用失败，
        # 并让调用者回滚触发的设置写入。在这里静默回退将是更糟糕的用户体验——
        # 用户保存了 "openrouter"，运行时将切换到 NearAI，UI 将显示 NearAI，
        # 用户会想知道他们的选择去哪了。
        if strict_db_reads:
            return llm.resolve(settings)

        return llm.resolve_with_fallback(settings)

    @classmethod
    async def _resolve_llm_with_secrets(
        cls,
        store: Optional["SettingsStore"],
        user_id: str,
        toml_path: Optional[Path],
        secrets_store: Optional["SecretsStore"],
        is_operator: bool,
    ) -> LlmConfig:
        """
        从当前源栈仅解析 LLM 配置。

        这被需要与启动时完全相同的所有者/管理员合并语义的热重载路径使用，
        而无需重建不相关的配置部分。
        非严格模式：应用 `resolve_with_fallback`，因此不可用的用户后端
        在启动时降级到 NearAI，而不是崩溃循环（#2514）。
        对于热重载路径，使用 [`resolve_llm_with_secrets_strict`]。
        """
        return await cls._resolve_llm_with_secrets_inner(
            store, user_id, toml_path, secrets_store, is_operator, False
        )

    @classmethod
    async def _resolve_llm_with_secrets_strict(
        cls,
        store: Optional["SettingsStore"],
        user_id: str,
        toml_path: Optional[Path],
        secrets_store: Optional["SecretsStore"],
        is_operator: bool,
    ) -> LlmConfig:
        """
        为必须在数据库读取错误时安全关闭的热重载路径解析 LLM 配置，
        以便调用者可以回滚触发的设置写入。
        严格模式还禁用 NearAI 回退：损坏的保存产生 `Err`
        而不是静默降级，这是调用者触发回滚并保留用户显式选择所需的信号。
        """
        return await cls._resolve_llm_with_secrets_inner(
            store, user_id, toml_path, secrets_store, is_operator, True
        )

    @classmethod
    async def _build(cls, settings: "Settings") -> "Config":
        """从设置构建配置（由 from_env 和 from_db 共享）。"""
        owner_id = resolve_owner_id(settings)

        tunnel = TunnelConfig.resolve(settings)
        channels = ChannelsConfig.resolve(settings, owner_id)

        # 针对持久的所有者范围解析启动工作区。
        # 网关可能暴露不同的发送者身份，但基础运行时工作区保持所有者范围，
        # 每用户网关工作区由 WorkspacePool 单独处理。
        workspace = WorkspaceConfig.resolve(owner_id)

        llm_config = llm.resolve(settings)
        embeddings_config = embeddings.resolve_embeddings_config(
            settings, llm_config.nearai.base_url
        )

        return cls(
            owner_id=owner_id,
            database=DatabaseConfig.resolve(),
            llm=llm_config,
            embeddings=embeddings_config,
            tunnel=tunnel,
            channels=channels,
            agent=AgentConfig.resolve(settings),
            safety=resolve_safety_config(settings),
            wasm=WasmConfig.resolve(settings),
            secrets=await SecretsConfig.resolve(),
            builder=BuilderModeConfig.resolve(settings),
            heartbeat=HeartbeatConfig.resolve(settings),
            hygiene=HygieneConfig.resolve(settings),
            routines=RoutineConfig.resolve(settings),
            # #3045 的 PR 3：目前仅从环境变量读取运行时配置文件/部署模式。
            # CLI 覆盖在 `from_env*` 返回后通过
            # `Config.with_runtime_overrides` 到达，因此二进制入口点在
            # 一个位置应用它们，而不是将它们线程化到每个内部的
            # `build` 调用者。
            runtime=RuntimeConfig.resolve_from(RuntimeConfigOverrides()),
            sandbox=SandboxModeConfig.resolve(settings),
            claude_code=ClaudeCodeConfig.resolve(settings),
            acp=AcpModeConfig.resolve(settings),
            skills=SkillsConfig.resolve(settings),
            transcription=TranscriptionConfig.resolve(settings),
            search=WorkspaceSearchConfig.resolve(settings),
            missions=MissionsConfig.resolve(settings),
            workspace=workspace,
            observability=ObservabilityConfig(
                backend=os.environ.get("OBSERVABILITY_BACKEND", "none"),
            ),
            oauth=OAuthConfig.resolve(),
            relay=RelayConfig.from_env(),
        )

def load_bootstrap_settings(toml_path: Optional[Path]) -> Settings:
    """
    加载引导设置。

    加载 `.env` 文件和环境变量，应用默认设置，
    然后应用配置文件覆盖层和可选的 TOML 文件覆盖层。
    """
    # 加载 .env 文件（不会覆盖已存在的环境变量）
    load_dotenv()
    # 加载 IronClaw 特定的环境变量
    load_ironclaw_env()

    # 从默认设置开始
    settings = Settings.default()
    # 应用运行时配置文件
    profile.apply_profile(settings)
    # 应用 TOML 覆盖层（如果提供）
    Config._apply_toml_overlay(settings, toml_path)
    return settings