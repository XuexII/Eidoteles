from __future__ import annotations
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Union, Optional, Tuple
from channels.web.log_layer import LogBroadcaster
from config import Config
from llm import LlmProvider, RecordingLlm, SessionManager, build_provider_chain
from dataclasses import dataclass
from pathlib import Path
import logging
import asyncio
from db import Database
from tools import ToolRegistry
import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class AppComponents:
    """
    完全初始化的应用程序组件，准备好进行通道连接和代理构建。
    """
    # 数据库重载和密钥注入后的（可能已变异的）配置
    config: Config
    db: Optional[Database] = None
    secrets_store: Optional[SecretsStore] = None
    llm: LlmProvider = None  # 在 __post_init__ 或调用方设置
    cheap_llm: Optional[LlmProvider] = None
    # LLM 提供者链的热重载控制器。当 LLM 通过 `AppBuilder.with_llm`
    # 注入时为 `None`（测试夹具），因此链最初不是从配置构建的。
    llm_reload: Optional[LlmReloadHandle] = None
    safety: SafetyLayer = None
    tools: ToolRegistry = None
    embeddings: Optional[EmbeddingProvider] = None
    workspace: Optional[Workspace] = None
    # 工作区支持的 `SettingsStore` 适配器，将设置双重写入
    # 遗留的 `settings` 表和 `.system/settings/**` 工作区文档。
    # 当 `db` 和 `workspace` 都可用时填充。
    # 只需要 `SettingsStore` 的使用者（权限工具、SIGHUP 重载处理器）
    # 应优先使用此接口而非原始的 `db`，以便运行时设置写入
    # 通过工作区流动并获得模式验证。
    settings_store: Optional[SettingsStore] = None
    # 用于 `flush()` / `invalidate_user()` 的具体缓存句柄。
    # 当缓存处于活动状态时，与 `settings_store` 是同一实例。
    settings_cache: Optional[CachedSettingsStore] = None
    extension_manager: Optional[ExtensionManager] = None
    mcp_session_manager: McpSessionManager = None
    mcp_process_manager: McpProcessManager = None
    wasm_tool_runtime: Optional[WasmToolRuntime] = None
    log_broadcaster: LogBroadcaster = None
    context_manager: ContextManager = None
    hooks: HookRegistry = None
    # 标准代理运行时使用的共享线程/会话管理器
    agent_session_manager: AgentSessionManager = None
    skill_registry: Optional[SkillRegistry] = None
    skill_catalog: Optional[SkillCatalog] = None
    cost_guard: CostGuard = None
    recording_handle: Optional[RecordingLlm] = None
    http_interceptor: Optional[HttpInterceptor] = None
    session: SessionManager = None
    catalog_entries: List[RegistryEntry] = field(default_factory=list)
    dev_loaded_tool_names: List[str] = field(default_factory=list)
    builder: Optional[SoftwareBuilder] = None
    # 进程内直写缓存：`(channel, external_id)` → `Identity`。
    # 由配对流程填充（任务 8）。在此预分配，以便所有子系统
    # 可以持有指向同一缓存实例的 `Arc`。
    ownership_cache: OwnershipCache = None


@dataclass
class AppBuilderFlags:
    """
    控制可选初始化阶段的选项。
    """
    no_db: bool = False  # 默认为 False，表示启用数据库



@dataclass
class AppBuilderFlags:
    """AppBuilder 的标志。"""
    no_db: bool = False
    # 其他标志根据需要添加


@dataclass
class AppBuilder:
    """
    协调 5 个机械化初始化阶段的构建器。
    """
    config: Config
    flags: AppBuilderFlags
    toml_path: Optional[Path]
    session: SessionManager
    log_broadcaster: LogBroadcaster

    # 累积的状态
    db: Optional[Database] = None
    secrets_store: Optional[SecretsStore] = None

    # 测试覆盖
    llm_override: Optional[LlmProvider] = None

    # 密钥存储所需的后端特定句柄
    handles: Optional[DatabaseHandles] = None

    def with_database(self, db: Database) -> None:
        """
        注入预创建的数据库，跳过 `init_database()`。

        **警告：** 这会使 `self.handles` 保持为 `None`，这意味着
        `init_secrets()` 无法构造真实的 `SecretsStore`（该存储需要
        后端特定的句柄，而不是通用的 `Database`）。
        需要凭证/OAuth/加密密钥的测试必须改用
        [`AppBuilder.with_database_and_handles`]，以便密钥路径保持连接。
        """
        self.db = db

    def with_database_and_handles(
        self,
        db: Database,
        handles: DatabaseHandles,
    ) -> None:
        """
        注入预创建的数据库**和**匹配的后端特定句柄，
        跳过 `init_database()`。

        当测试将执行涉及 `SecretsStore` 的代码路径时使用此方法
        （OAuth、加密凭证、密钥支持的 WASM 工具）。
        对于 libSQL 后端，句柄通过 `LibSqlBackend.shared_db()` 构造；
        对于 PostgreSQL 通过 `PgBackend.pool()` 构造。
        """
        self.db = db
        self.handles = handles

    def with_llm(self, llm: LlmProvider) -> None:
        """注入预创建的 LLM 提供者，跳过 `init_llm()`。"""
        self.llm_override = llm

    async def init_database(self) -> None:
        """
        阶段 1：初始化数据库后端。

        创建数据库连接，运行迁移，从数据库重载配置，
        将会话管理器附加到数据库，并清理过时的作业。
        """
        if self.db is not None:
            logger.debug("数据库已提供，跳过 init_database()")
            return

        if self.flags.no_db:
            logger.warning("在没有数据库连接的情况下运行")
            return

        db, handles = await db.connect_with_handles(self.config.database)
        self.handles = handles

        # 初始化后：确保所有者用户行存在并重写 'default' user_id 行
        await bootstrap_ownership(db, self.config)

        # 初始化后：迁移磁盘配置，从数据库重载配置，附加会话，清理
        try:
            await migrate_disk_to_db(db, self.config.owner_id)
        except Exception as e:
            logger.warning("磁盘到数据库设置迁移失败: %s", e)

        toml_path = self.toml_path
        # is_operator=true：owner_id 是操作员/管理员范围
        try:
            db_config = await Config.from_db_with_toml(
                db, self.config.owner_id, toml_path, True
            )
            self.config = db_config
            logger.debug("配置已从数据库重载")
        except Exception as e:
            logger.warning(
                "从数据库重载配置失败，保留基于环境变量的配置: %s", e
            )

        session_db: SharedSessionDb = DatabaseSessionDb(db)
        await self.session.attach_store(session_db, self.config.owner_id)

        # 发射后不管的清理工作——无需阻塞启动
        db_cleanup = db
        asyncio.create_task(self._cleanup_stale_jobs(db_cleanup))

        self.db = db

    @staticmethod
    async def _cleanup_stale_jobs(db: Database) -> None:
        """清理过时的沙箱作业。"""
        try:
            await db.cleanup_stale_sandbox_jobs()
        except Exception as e:
            logger.warning("清理过时的沙箱作业失败: %s", e)

    def install_ephemeral_secrets_store(self, reason: str) -> None:
        """
        安装一个临时的内存密钥存储，以便下游 WASM 工具/通道连接
        始终可以依赖 `self.secrets_store` 为已设置状态。

        在持久密钥构造失败时使用（无主密钥、无数据库句柄、加密初始化失败）。
        没有此回退，WASM 工具凭证注入在托管 TEE 部署上会静默地不执行任何操作，
        因为加载器仅在 `self.secrets_store` 已设置时才连接存储——
        参见 #1537（"WASM 凭证注入在托管 TEE 上失败"）。

        声明必需凭证的工具随后将通过 `resolve_host_credentials` 中的
        安全关闭分支拒绝运行，从而显示清晰的错误，而不是发出未认证的 HTTP 请求。

        `reason` 指定触发回退的具体路径——以警告级别记录，
        以便诊断 TEE 部署的操作员可以区分"主密钥从未解析"、
        "主密钥已解析但无数据库句柄"和"加密初始化失败"，
        而无需开启调试日志。

        返回 `build_ephemeral_secrets_store` 的错误，以便真正损坏的加密设置
        在此处中止启动——否则下游阶段（例如 `init_extensions`）稍后会以
        更难以操作的"密钥存储未初始化"错误失败。
        """
        store = build_ephemeral_secrets_store()
        logger.warning(
            "reason=%s, 持久密钥存储不可用；安装临时内存回退。"
            "通过 `ironclaw tool auth` 保存的凭证在重启后将不会持久化。"
            "运行 `ironclaw doctor` 进行诊断（有关托管 TEE 的具体信息，参见 #1537）。",
            reason,
        )
        self.secrets_store = store

    async def init_secrets(self) -> None:
        """
        阶段 2：创建密钥存储。

        需要主密钥和后端特定的数据库句柄。创建存储后，
        将任何加密的 LLM API 密钥注入配置覆盖层并重新解析配置。
        """
        master_key = self.config.secrets.master_key()
        if master_key is None:
            # 没有可用的密钥数据库，但我们仍然可以从操作系统凭证存储
            # 加载令牌（例如，通过 Claude Code 的 macOS Keychain /
            # Linux ~/.claude/.credentials.json 的 Anthropic OAuth）。
            inject_os_credentials()

            # 消费未使用的句柄
            self.handles = None

            # 仅使用操作系统凭证重新解析 LLM 配置
            store: Optional[SettingsStore] = self.db
            toml_path = self.toml_path
            owner_id = self.config.owner_id
            try:
                await self.config.re_resolve_llm(store, owner_id, toml_path)
            except Exception as e:
                logger.warning(
                    "操作系统凭证注入后重新解析 LLM 配置失败: %s", e
                )

            self.install_ephemeral_secrets_store("主密钥解析未产生密钥")
            return

        try:
            crypto = SecretsCrypto(master_key)
        except Exception as e:
            logger.warning("初始化密钥加密失败: %s", e)
            self.handles = None
            self.install_ephemeral_secrets_store("密钥加密初始化失败")
            return

        # 回退覆盖 init_database 在填充 self.handles 之前提前返回的无数据库路径
        handles = self.handles if self.handles is not None else DatabaseHandles.default()
        store = create_secrets_store(crypto, handles)

        # 安全门：如果我们在本次运行中自动生成了新的主密钥，
        # 但密钥表已经包含来自先前密钥的行，则这些行无法解密，
        # 静默继续会掩盖不可恢复的数据。大声失败（并在探测错误时安全关闭），
        # 以便用户可以在任何新写入堆积之前恢复原始密钥。
        #
        # 回滚 auto_generate_and_persist 已经提交的持久化：
        # 否则后续重启会将新写入的密钥读取为 `source = Env/Keychain, generated = false`，
        # 跳过此门控，并静默接受错误的密钥。
        # 回滚保持门控在每次启动时重新触发，直到用户恢复真实密钥或清除过时行。
        if store is not None and self.config.secrets.generated:
            try:
                await verify_generated_key_safe(self.config.secrets.generated, store)
            except Exception as gate_err:
                if self.config.secrets.generated:
                    await rollback_generated_key_persistence(
                        self.config.secrets.source,
                        ironclaw_env_path(),
                    )
                raise gate_err

        if store is not None:
            # 将任何明文 API 密钥从设置表迁移到加密密钥存储。
            # 幂等——在每次启动时运行是安全的。
            if self.db is not None:
                await migrate_plaintext_llm_keys(
                    self.db, store, self.config.owner_id
                )

                # 将 NEAR AI 会话令牌从明文设置迁移到加密密钥。
                # 幂等——在每次启动时运行是安全的。
                await migrate_session_credential(
                    self.db, store, self.config.owner_id
                )

            # 从加密存储注入 LLM API 密钥
            await inject_llm_keys_from_secrets(store, self.config.owner_id)

            # 使用新可用的密钥重新解析 LLM 配置，
            # 包括从密钥存储水合的密钥。
            settings_store: Optional[SettingsStore] = self.db
            toml_path = self.toml_path
            owner_id = self.config.owner_id
            # is_operator=true：owner_id 是操作员/管理员范围
            try:
                await self.config.re_resolve_llm_with_secrets(
                    settings_store, owner_id, toml_path, store, True
                )
            except Exception as e:
                logger.warning("密钥注入后重新解析 LLM 配置失败: %s", e)

            # 将密钥存储连接到会话管理器，以便将来的
            # 令牌保存进入加密存储。
            session_secrets: SharedSessionSecrets = SecretsStoreSessionSecrets(store)
            await self.session.attach_secrets(session_secrets)

        self.secrets_store = store

        # 如果没有创建持久存储（例如，主密钥已解析但没有可用的数据库句柄），
        # 回退到临时内存存储，以便下游 WASM 工具/通道连接仍然通过
        # 凭证注入代码路径。有关原理，请参见 `install_ephemeral_secrets_store`（#1537）。
        if self.secrets_store is None:
            if self.handles is None:
                reason = (
                    "主密钥已解析但没有可用的数据库句柄"
                    "（no_db 模式或 init_database 未运行）"
                )
            elif self.handles.libsql_db is None and self.handles.pg_pool is None:
                reason = (
                    "主密钥已解析但 libsql 和 postgres 句柄均不存在"
                    "（可能是 feature-flag / 后端不匹配）"
                )
            else:
                reason = (
                    "主密钥已解析且数据库句柄存在但 create_secrets_store 返回了 None"
                    "（意外情况）"
                )
            self.install_ephemeral_secrets_store(reason)

    async def init_llm(
        self,
    ) -> Tuple[LlmProvider, Optional[LlmProvider], Optional[RecordingLlm], LlmReloadHandle]:
        """
        阶段 3：初始化 LLM 提供者链。

        委托给 `build_provider_chain`，后者应用所有装饰器
        （重试、智能路由、故障转移、断路器、响应缓存）。
        """
        llm, cheap_llm, recording_handle, reload_handle = await build_provider_chain(
            self.config.llm, self.session
        )
        return llm, cheap_llm, recording_handle, reload_handle

    async def init_tools(
        self,
        llm: LlmProvider,
        cheap_llm: Optional[LlmProvider],
    ) -> Tuple[
        SafetyLayer,
        ToolRegistry,
        Optional[EmbeddingProvider],
        Optional[Workspace],
        Optional[SoftwareBuilder],
        SharedCredentialRegistry,
        Optional[HttpInterceptor],
        Optional[WorkspaceResolver],
    ]:
        """
        阶段 4：初始化安全层、工具、嵌入和工作区。
        """
        safety = SafetyLayer(self.config.safety)
        logger.debug("安全层已初始化")

        # 使用凭证注入支持初始化工具注册表
        credential_registry = SharedCredentialRegistry()
        engine_version = EngineVersion.V2 if is_engine_v2_enabled() else EngineVersion.V1
        registry = ToolRegistry().with_engine_version(engine_version)
        if self.db is not None:
            registry = registry.with_database(self.db)
        if self.secrets_store is not None:
            registry = registry.with_credentials(credential_registry, self.secrets_store)

        # 仅限测试的 HTTP 主机重映射。限制在 debug/test 构建中，因此
        # 发布部署上的意外 `IRONCLAW_TEST_HTTP_REMAP` 环境变量不会
        # 静默地将出站 HTTP 从生产重定向到测试端点。
        http_interceptor = remap_from_env() if __debug__ else None
        if http_interceptor is not None:
            registry = registry.with_http_interceptor(http_interceptor)

        tools = registry
        tools.register_builtin_tools()
        tools.register_tool_info()
        tools.register_system_tools()

        if self.secrets_store is not None:
            tools.register_secrets_tools(self.secrets_store)

        # 使用统一方法创建嵌入提供者。
        # 在边界处将 LLM 端的 `BedrockConfig` 转换为嵌入端的
        # `BedrockEmbeddingSetup`，以便嵌入层不依赖于 `ironclaw_llm` 配置类型。
        bedrock_setup = None
        if self.config.llm.bedrock is not None:
            bedrock_setup = BedrockEmbeddingSetup(
                region=self.config.llm.bedrock.region,
                profile=self.config.llm.bedrock.profile,
            )
        embeddings = await create_provider(
            self.config.embeddings,
            ProviderDeps(
                session=self.session,
                bedrock_setup=bedrock_setup,
            ),
        )

        # 如果数据库可用，注册内存工具
        workspace_user_id = self.config.owner_id
        workspace: Optional[Workspace] = None
        workspace_resolver: Optional[WorkspaceResolver] = None
        if self.db is not None:
            emb_cache_config = EmbeddingCacheConfig(
                max_entries=self.config.embeddings.cache_size,
            )
            ws = Workspace.new_with_db(workspace_user_id, self.db).with_search_config(
                self.config.search
            )

            if embeddings is not None:
                ws = ws.with_embeddings_cached(embeddings, emb_cache_config)

            # 连接工作区级别的设置（读取范围、内存层）
            if self.config.workspace.read_scopes:
                ws = ws.with_additional_read_scopes(self.config.workspace.read_scopes)
                logger.info(
                    "工作区配置了多范围读取，user_id=%s, read_scopes=%s",
                    workspace_user_id,
                    ws.read_user_ids(),
                )
            ws = ws.with_memory_layers(self.config.workspace.memory_layers)

            # 内存工具必须通过 `ctx.user_id` 解析，而不是固定的启动工作区。
            # 即使在非认证多租户模式之外，某些通道和测试夹具也会
            # 通过按需播种的每用户租户工作区路由非所有者用户。
            #
            # 部署是否为多租户是配置，而不是我们应该从当前数据库内容推断的属性。
            # 管理员可以在创建任何租户用户之前以多租户模式启动。
            is_multi_tenant = self.config.is_multi_tenant_deployment()

            # 在多租户模式下，在所有者工作区上启用管理员系统提示，
            # 以便调度器从 __admin__ 范围读取 SYSTEM.md。
            if is_multi_tenant:
                ws = ws.with_admin_prompt()

            workspace = ws
            pool: WorkspaceResolver = WorkspacePool(
                self.db,
                embeddings,
                emb_cache_config,
                self.config.search,
                self.config.workspace,
            )
            workspace_resolver = pool
            reasoning_llm = cheap_llm if cheap_llm is not None else llm
            tools.register_memory_tools_with_resolver(
                pool, reasoning_llm, self.config.search.reasoning_enabled
            )
            logger.debug(
                "内存工具已配置每用户工作区解析器，multi_tenant=%s",
                is_multi_tenant,
            )

        # 如果我们有工作区和 LLM API 凭证，则注册图像/视觉工具
        if workspace is not None:
            provider = self.config.llm.provider
            if provider is not None:
                api_base = provider.base_url
                api_key_opt = provider.api_key
            else:
                api_base = self.config.llm.nearai.base_url
                api_key_opt = self.config.llm.nearai.api_key

            if api_key_opt is not None:
                # 检查图像生成模型
                model_name = (
                    provider.model
                    if provider is not None
                    else self.config.llm.nearai.model
                )
                models = [model_name]
                gen_model = suggest_image_model(models) or "black-forest-labs/FLUX.2-klein-4B"
                tools.register_image_tools(api_base, api_key_opt, gen_model, None)

                # 检查视觉模型
                vision_model = suggest_vision_model(models) or model_name
                tools.register_vision_tools(api_base, api_key_opt, vision_model, None)

        # 如果启用，注册构建器工具
        builder = None
        if self.config.builder.enabled and (
            self.config.agent.allow_local_tools or not self.config.sandbox.enabled
        ):
            builder = await tools.register_builder_tool(
                llm, self.config.builder.to_builder_config()
            )
            logger.debug("构建器模式已启用")

        return (
            safety,
            tools,
            embeddings,
            workspace,
            builder,
            credential_registry,
            http_interceptor,
            workspace_resolver,
        )

    async def init_extensions(
        self,
        tools: ToolRegistry,
        hooks: HookRegistry,
        settings_store_override: Optional[SettingsStore],
        ownership_cache: OwnershipCache,
    ) -> Tuple[
        McpSessionManager,
        McpProcessManager,
        Optional[WasmToolRuntime],
        Optional[ExtensionManager],
        List[RegistryEntry],
        List[str],
    ]:
        """
        阶段 5：加载 WASM 工具、MCP 服务器，并创建扩展管理器。
        """
        # `McpSessionManager()` 硬编码 1800 秒空闲超时
        # （参见 `src/tools/mcp/session.rs`）。目前没有会话计数上限——
        # 如果大型部署需要，向管理器添加 `max_sessions` 字段并在此处添加真正的旋钮；
        # 先前的 `MCP_MAX_SESSIONS` 环境变量已连接但从未到达结构体，已被移除。
        mcp_session_manager = McpSessionManager()
        mcp_process_manager = McpProcessManager()

        # 急切创建 WASM 工具运行时，以便启动后安装的扩展
        # （例如通过 Web UI）仍然可以激活。工具目录仅在加载模块时需要，
        # 而不是用于引擎初始化。
        wasm_tool_runtime: Optional[WasmToolRuntime] = None
        if self.config.wasm.enabled:
            try:
                wasm_tool_runtime = WasmToolRuntime(self.config.wasm.to_runtime_config())
            except Exception as e:
                logger.warning("初始化 WASM 运行时失败: %s", e)

        # 并发加载 WASM 工具和 MCP 服务器
        dev_loaded_tool_names = await self._load_wasm_tools(tools, wasm_tool_runtime)
        startup_mcp_clients = await self._load_mcp_servers(
            mcp_session_manager, mcp_process_manager
        )

        # 加载注册表目录条目用于扩展发现
        catalog_entries = []
        try:
            catalog = RegistryCatalog.load_or_embedded()
            catalog_entries = catalog.discovery_entries()
            logger.debug(
                "已加载 %d 个注册表目录条目用于扩展发现",
                len(catalog_entries),
            )
        except Exception as e:
            logger.warning("加载注册表目录失败: %s", e)

        # 追加内置条目（例如通道中继集成），使其出现在 Web UI 的可用扩展列表中
        builtin = builtin_entries()
        for entry in builtin:
            if not any(e.name == entry.name for e in catalog_entries):
                catalog_entries.append(entry)

        # 创建扩展管理器。`init_secrets` 保证
        # `self.secrets_store` 已设置——要么是持久存储，要么是
        # 临时内存回退——因此扩展管理器、WASM 工具加载器和 WASM 通道设置
        # 都共享相同的存储实例。有关推动无条件连接的托管 TEE 回归，
        # 请参见 #1537。
        if self.secrets_store is None:
            raise RuntimeError(
                "密钥存储未初始化；在 init_extensions() 之前调用 init_secrets()"
            )
        ext_secrets: SecretsStore = self.secrets_store

        em = ExtensionManager(
            mcp_session_manager,
            mcp_process_manager,
            ext_secrets,
            tools,
            hooks,
            wasm_tool_runtime,
            self.config.wasm.tools_dir,
            self.config.channels.wasm_channels_dir,
            self.config.tunnel.public_url,
            self.config.owner_id,
            self.db,
            catalog_entries,
        )
        if settings_store_override is not None:
            em = em.with_settings_store(settings_store_override)

        pairing_store = None
        if self.db is not None:
            ps = PairingStore(self.db, ownership_cache)
            em = em.with_pairing_store(ps)
            pairing_store = ps

        # 连接 Reborn Telegram v2 功能标志，以便管理器
        # 可以在 v2 拥有 webhook 安装时拒绝遗留 `telegram` WASM
        # 通道的热激活（Henry 对 PR #3356 的审查——单独的启动守卫是不够的）。
        em.set_reborn_telegram_v2_enabled(self.config.channels.reborn_telegram_v2_enabled)

        extension_manager = em
        tools.register_extension_tools(extension_manager)
        if pairing_store is not None:
            tools.register_sync(PairingApproveTool(pairing_store))

        # 注册权限管理工具并使用内置注册表支持升级 tool_list。
        # 当调用者提供时，优先使用工作区支持的适配器（生产连接），
        # 以便设置写入通过模式验证；对于没有工作区的测试夹具，
        # 回退到原始数据库。
        settings_store_for_perms: Optional[SettingsStore] = (
            settings_store_override if settings_store_override is not None else self.db
        )
        tools.register_permission_tools(settings_store_for_perms)
        tools.upgrade_tool_list(extension_manager, settings_store_for_perms)

        logger.debug("扩展管理器已使用聊天内发现工具初始化")

        if startup_mcp_clients:
            logger.info(
                "正在将 %d 个启动 MCP 客户端注入扩展管理器",
                len(startup_mcp_clients),
            )
            for name, client in startup_mcp_clients:
                # 此处的 `name` 是原始配置行的 `server.name`，
                # 在 `create_client_from_config()` 将连字符规范化为下划线之前捕获。
                # 客户端本身、生成的包装器以及会话/进程管理器都使用规范化后的名称。
                # 在此处使用原始 `name` 会将客户端以 `"my-mcp-server"` 插入
                # `McpClientStore`，而包装器在调度时查找 `"my_mcp_server"`，
                # 导致每次调用都静默失败，并显示
                # "MCP server '…' is not active for this user"，
                # 直到手动重新激活。从客户端的规范字段获取名称，
                # 以保证插入键与调度时的查找键匹配。
                normalized_name = client.server_name()
                registered = await extension_manager.inject_mcp_client(
                    normalized_name, self.config.owner_id, client
                )
                if name != normalized_name:
                    logger.debug(
                        "启动 MCP 服务器名称已规范化（连字符 -> 下划线）用于客户端存储注入，"
                        "raw_name=%s, normalized=%s",
                        name,
                        normalized_name,
                    )
                logger.debug(
                    "已为启动 MCP 服务器注册 %d 个工具，server=%s",
                    len(registered),
                    normalized_name,
                )

        # 在启动时验证 ACP 代理配置（轻量级——无连接，仅配置检查）
        try:
            acp_agents = (
                await load_acp_agents_from_db(self.db, self.config.owner_id)
                if self.db is not None
                else await load_acp_agents()
            )
            enabled = [a for a in acp_agents.enabled_agents()]
            if enabled:
                names = [a.name for a in enabled]
                logger.info(
                    "ACP 代理已配置：%s（%d 个已启用）",
                    ", ".join(names),
                    len(enabled),
                )
        except Exception as e:
            logger.debug("未配置 ACP 代理 (%s)", e)

        # register_builder_tool() 已在内部调用 register_dev_tools()，
        # 因此仅当构建器尚未执行时才在此处注册。
        builder_registered_dev_tools = self.config.builder.enabled and (
            self.config.agent.allow_local_tools or not self.config.sandbox.enabled
        )
        if self.config.agent.allow_local_tools and not builder_registered_dev_tools:
            tools.register_dev_tools()

        return (
            mcp_session_manager,
            mcp_process_manager,
            wasm_tool_runtime,
            extension_manager,
            catalog_entries,
            dev_loaded_tool_names,
        )

    async def _load_wasm_tools(
        self,
        tools: ToolRegistry,
        wasm_tool_runtime: Optional[WasmToolRuntime],
    ) -> List[str]:
        """加载 WASM 工具。"""
        dev_loaded_tool_names: List[str] = []

        if wasm_tool_runtime is None:
            return dev_loaded_tool_names

        loader = WasmToolLoader(wasm_tool_runtime, tools)
        if self.secrets_store is not None:
            loader = loader.with_secrets_store(self.secrets_store)
        if self.db is not None:
            loader = loader.with_role_lookup(self.db)

        try:
            results = await loader.load_from_dir(self.config.wasm.tools_dir)
            if results.loaded:
                logger.debug(
                    "已从 %s 加载 %d 个 WASM 工具",
                    self.config.wasm.tools_dir,
                    len(results.loaded),
                )
            for path, err in results.errors.items():
                logger.warning("加载 WASM 工具失败 %s: %s", path, err)
        except Exception as e:
            logger.warning("扫描 WASM 工具目录失败: %s", e)

        try:
            results = await load_dev_tools(loader, self.config.wasm.tools_dir)
            dev_loaded_tool_names.extend(results.loaded)
            if dev_loaded_tool_names:
                logger.debug(
                    "已从构建产物加载 %d 个开发 WASM 工具",
                    len(dev_loaded_tool_names),
                )
        except Exception as e:
            logger.debug("未找到开发 WASM 工具: %s", e)

        return dev_loaded_tool_names

    async def _load_mcp_servers(
        self,
        mcp_session_manager: McpSessionManager,
        mcp_process_manager: McpProcessManager,
    ) -> List[Tuple[str, Any]]:
        """加载 MCP 服务器。"""
        try:
            servers = await load_mcp_servers_ready(self.db, self.config.owner_id)
        except Exception as e:
            if hasattr(e, 'is_config_error'):
                logger.warning("MCP 服务器配置无效: %s。修复或删除损坏的配置。", e)
            else:
                logger.debug("未配置 MCP 服务器 (%s)", e)
            return []

        enabled = list(servers.enabled_servers())
        if not enabled:
            return []

        logger.debug("正在加载 %d 个已配置的 MCP 服务器...", len(enabled))

        startup_clients = []
        for server in enabled:
            server_name = server.name
            has_custom_auth_header = server.has_custom_auth_header()

            try:
                client = await create_client_from_config(
                    server,
                    mcp_session_manager,
                    mcp_process_manager,
                    self.secrets_store,
                    self.config.owner_id,
                )
            except Exception as e:
                logger.warning("为 '%s' 创建 MCP 客户端失败: %s", server_name, e)
                continue

            try:
                mcp_tools = await client.list_tools()
                tool_count = len(mcp_tools)
                logger.debug(
                    "已连接到 MCP 服务器 '%s'（%d 个工具）；"
                    "将包装器注册推迟到管理器初始化",
                    server_name,
                    tool_count,
                )
                startup_clients.append((server_name, client))
            except Exception as e:
                err_str = str(e)
                if is_auth_error_message(err_str):
                    if has_custom_auth_header:
                        logger.warning(
                            "MCP 服务器 '%s' 拒绝了其配置的 Authorization 头。"
                            "更新已配置的凭证并重试。",
                            server_name,
                        )
                    else:
                        logger.warning(
                            "MCP 服务器 '%s' 需要认证。"
                            "运行：ironclaw mcp auth %s",
                            server_name,
                            server_name,
                        )
                else:
                    logger.warning(
                        "连接到 MCP 服务器 '%s' 失败: %s",
                        server_name,
                        e,
                    )

        return startup_clients

    async def build_all(self) -> AppComponents:
        """按顺序运行所有初始化阶段并返回组装的组件。"""
        await self.init_database()
        await self.init_secrets()

        # 初始化后验证：具有专用配置槽的后端
        # (nearai/gemini_oauth/bedrock/openai_codex) 从其自己的子结构体读取，
        # 不填充 `LlmConfig.provider`。对于 OpenAI 形状的注册表后端，
        # 如果没有解析到提供者配置，则提前失败。
        registry = ProviderRegistry.load()
        backend = self.config.llm.backend
        entry = registry.find(backend)
        has_dedicated_config = entry is not None and entry.protocol.has_dedicated_config()
        if not has_dedicated_config and self.config.llm.provider is None:
            raise RuntimeError(
                f"LLM_BACKEND={backend} 已配置但未找到凭证。"
                "设置适当的 API 密钥环境变量或运行设置向导。"
            )

        if self.llm_override is not None:
            llm = self.llm_override
            cheap_llm = None
            recording_handle = None
            llm_reload = None
        else:
            llm, cheap_llm, recording_handle, llm_reload = await self.init_llm()

        (
            safety,
            tools,
            embeddings,
            workspace,
            builder,
            credential_registry,
            http_interceptor,
            workspace_resolver,
        ) = await self.init_tools(llm, cheap_llm)

        # 早期创建钩子注册表，以便运行时扩展激活可以注册钩子
        hooks = HookRegistry()

        # 注册会话摘要钩子（在会话结束时写入对话摘要）
        if self.db is not None and workspace_resolver is not None:
            summary_llm = cheap_llm if cheap_llm is not None else llm
            await hooks.register(
                SessionSummaryHook(
                    self.db,
                    workspace_resolver,
                    summary_llm,
                )
            )

        agent_session_manager = AgentSessionManager().with_hooks(hooks)

        # 在 init_extensions 之前构建工作区支持的 `SettingsStore`，
        # 以便在那里注册的工具（`register_permission_tools`、
        # `upgrade_tool_list`）从一开始就可以使用适配器连接。
        # 相同的适配器实例随后在 `AppComponents.settings_store` 上暴露，
        # 并由 main.rs 重用（例如用于 SIGHUP 重载处理器）。
        settings_store: Optional[SettingsStore] = None
        settings_cache: Optional[CachedSettingsStore] = None
        if workspace is not None and self.db is not None:
            adapter = WorkspaceSettingsAdapter(workspace, self.db)
            try:
                await adapter.ensure_system_config()
            except Exception as e:
                logger.debug(
                    "WorkspaceSettingsAdapter 急切种子失败（延迟种子将重试）: %s", e
                )
            cached = CachedSettingsStore(adapter)
            settings_store = cached
            settings_cache = cached

        ownership_cache = OwnershipCache()
        (
            mcp_session_manager,
            mcp_process_manager,
            wasm_tool_runtime,
            extension_manager,
            catalog_entries,
            dev_loaded_tool_names,
        ) = await self.init_extensions(
            tools, hooks, settings_store, ownership_cache
        )

        # 从设置加载引导完成标志，以便已完成引导的现有用户
        # 不会重新获得引导注入。
        if workspace is not None:
            toml_path = Settings.default_toml_path()
            try:
                settings = Settings.load_toml(toml_path)
                if settings is not None and settings.profile_onboarding_completed:
                    workspace.mark_bootstrap_completed()
            except Exception:
                pass

        # 播种工作区并回填嵌入
        if workspace is not None:
            # 如果设置了 WORKSPACE_IMPORT_DIR，首先从磁盘导入工作区文件。
            # 这让 Docker 镜像/部署脚本可以交付定制的工作区模板
            # （例如 AGENTS.md、TOOLS.md），这些模板会覆盖通用种子。
            # 仅导入数据库中尚不存在的文件——永远不会覆盖用户编辑。
            #
            # 在 seed_if_empty() 之前运行，以便自定义模板优先于通用种子。
            # seed_if_empty() 随后填充任何剩余的空白。
            import_dir = os.environ.get("WORKSPACE_IMPORT_DIR")
            if import_dir:
                import_path = Path(import_dir)
                try:
                    count = await workspace.import_from_directory(import_path)
                    if count > 0:
                        logger.debug(
                            "已从 %s 导入 %d 个工作区文件", import_dir, count
                        )
                except Exception as e:
                    logger.warning(
                        "从 %s 导入工作区文件失败: %s", import_dir, e
                    )

            try:
                await workspace.seed_if_empty()
            except Exception as e:
                logger.warning("播种工作区失败: %s", e)

            if embeddings is not None:
                ws_bg = workspace
                asyncio.create_task(self._backfill_embeddings(ws_bg))

        # 技能系统
        skill_registry = None
        skill_catalog = None
        if self.config.skills.enabled:
            registry = SkillRegistry(self.config.skills.local_dir)
            registry = registry.with_installed_dir(self.config.skills.installed_dir)
            registry = registry.with_bundled_content(load_bundled_skills())
            registry = registry.with_max_scan_depth(self.config.skills.max_scan_depth)
            loaded = await registry.discover_all()
            if loaded:
                logger.debug("已加载 %d 个技能: %s", len(loaded), ", ".join(loaded))

            # 将技能 frontmatter 中的凭证映射注册到共享注册表，
            # 以便 HTTP 工具可以自动注入凭证。
            register_skill_credentials(registry.skills(), credential_registry)
            if self.db is not None:
                await persist_skill_auth_descriptors(
                    registry.skills(), self.db, self.config.owner_id
                )

            skill_registry = registry
            skill_catalog = shared_catalog()
            tools.register_skill_tools(skill_registry, skill_catalog)

        context_manager = ContextManager(self.config.agent.max_parallel_jobs)
        cost_guard = CostGuard(
            CostGuardConfig(
                max_cost_per_day_cents=self.config.agent.max_cost_per_day_cents,
                max_actions_per_hour=self.config.agent.max_actions_per_hour,
                max_cost_per_user_per_day_cents=self.config.agent.max_cost_per_user_per_day_cents,
            )
        )

        logger.debug("工具注册表已初始化，共 %d 个工具", tools.count())

        # 一次性清理所有者幽灵播种的工具权限行。
        # 在 #3559 之前，`seed_tool_permissions` 将代码级默认值
        # （例如 `tool_install` → `AskEachTime`）写入数据库，
        # 以便权限面板可以渲染它们。这些行与用户显式覆盖无法区分，
        # 因此无法区分用户和从未接触过设置的人，
        # 并且 `AGENT_AUTO_APPROVE_TOOLS=true` 最终绕过了
        # 用户显式的 `AskEachTime` 选择（#3559 安全审查）。
        # 播种器已移除；此迁移一次性删除幽灵行，
        # 之后任何剩余的行在构造上都是用户显式的，
        # `resolve_permission` 可以信任其值。
        await cleanup_ghost_seeded_tool_permissions(self.db, self.config.owner_id)

        return AppComponents(
            config=self.config,
            db=self.db,
            secrets_store=self.secrets_store,
            llm=llm,
            cheap_llm=cheap_llm,
            llm_reload=llm_reload,
            safety=safety,
            tools=tools,
            embeddings=embeddings,
            workspace=workspace,
            settings_store=settings_store,
            settings_cache=settings_cache,
            extension_manager=extension_manager,
            mcp_session_manager=mcp_session_manager,
            mcp_process_manager=mcp_process_manager,
            wasm_tool_runtime=wasm_tool_runtime,
            log_broadcaster=self.log_broadcaster,
            context_manager=context_manager,
            hooks=hooks,
            agent_session_manager=agent_session_manager,
            skill_registry=skill_registry,
            skill_catalog=skill_catalog,
            cost_guard=cost_guard,
            recording_handle=recording_handle,
            http_interceptor=http_interceptor,
            session=self.session,
            catalog_entries=catalog_entries,
            dev_loaded_tool_names=dev_loaded_tool_names,
            builder=builder,
            ownership_cache=ownership_cache,
        )

    @staticmethod
    async def _backfill_embeddings(workspace: Workspace) -> None:
        """后台任务：回填嵌入。"""
        try:
            count = await workspace.backfill_embeddings()
            if count > 0:
                logger.debug("已为 %d 个块回填嵌入", count)
        except Exception as e:
            logger.warning("回填嵌入失败: %s", e)
