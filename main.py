from __future__ import annotations
import logging
import asyncio
from pathlib import Path
import sys

from agent import Agent, AgentDeps
from app import AppBuilder, AppBuilderFlags
from channels import (
    ChannelManager,
    GatewayChannel,
    HttpChannel,
    ReplChannel,
    SignalChannel,
    WebhookServer,
    WebhookServerConfig
)
from channels.wasm import WasmChannelRouter, WasmChannelRuntime
from channels.web.log_layer import LogBroadcaster, init_tracing
from cli import Cli, Command
from config import Config, RuntimeConfigOverrides
from bootstrap import PidLock, PidLockError, pid_lock_path
import time
from llm import create_session_manager, SessionManager

logger = logging.getLogger(__name__)


def main() -> None:
    """
    同步入口点。在 Tokio 运行时启动前加载 `.env` 文件，
    以确保 `os.environ` 操作是安全的（还没有工作线程）。
    """
    from dotenv import load_dotenv
    # 加载 .env 文件（忽略文件不存在等错误）
    load_dotenv()
    # load_ironclaw_env()

    # 创建多线程异步运行时并阻塞运行 async_main
    try:
        asyncio.run(async_main())
    except Exception as e:
        format_top_level_error(e)
        sys.exit(1)


def format_top_level_error(err):
    pass

def non_cli_channels_enabled(cli_only: bool) -> bool:
    """
    当应启用非 CLI 网络服务时返回 `True`。
    `--cli-only` 会抑制所有此类服务：webhooks、WASM 通道、HTTP、
    Signal、中继通道、网关、管理隧道和沙箱编排器 API。
    """
    return not cli_only

async def async_main() -> None:
    """
    异步主函数 - 解析命令行参数并执行相应的命令
    """
    cli = Cli.parse()  # 解析命令行参数
    enable_non_cli = non_cli_channels_enabled(cli.cli_only)  # 检查是否启用非CLI通道

    # 首先处理非代理命令（它们不需要完整的环境设置）

    # ── PID锁（防止多个实例同时运行） ────────────────────────
    _pid_lock = None
    try:
        _pid_lock = PidLock.acquire()
    except PidLockError as e:
        if e.is_already_running:
            pid_path = pid_lock_path()
            raise Exception(
                f"另一个 IronClaw 实例已在运行（PID {e.pid}）。"
                f"如果这不正确，请删除过期的 PID 文件：{pid_path}"
            )
        else:
            logger.warning(f"警告：无法获取 PID 锁：: {e}")
            logger.warning("继续执行，无 PID 锁保护。")

    startup_start = time.time()  # 记录启动开始时间

    # ── 代理启动 ──────────────────────────────────────────────────
    # 增强的首次运行检测（需要postgres或libsql特性）

    # # CLI标志覆盖配置
    # if cli.auto_approve:
    #     ironclaw.config.set_runtime_env("AGENT_AUTO_APPROVE_TOOLS", "true")

    # 从环境变量、磁盘和可选的TOML文件加载初始配置（在数据库可用之前）。
    # 此时可能缺少凭证——这是可以的。crate::config::llm::resolve()会延迟处理，
    # AppBuilder::build_all()在加载加密数据库中的密钥后会重新解析。
    toml_path = Path(cli.config) if hasattr(cli, 'config') else None
    runtime_overrides = RuntimeConfigOverrides(
        deployment=cli.deployment_mode,
        profile=cli.runtime_profile,
        # CLI标志是一个裸布尔值：--yolo-disclosure将其设置为true，
        # 缺失则保持None以便环境变量回退应用。
        yolo_disclosure_acknowledged=True if cli.yolo_disclosure else None
    )

    try:
        config = await Config.from_env_with_toml(toml_path)
        config = config.with_runtime_overrides(runtime_overrides)
    except Exception as e:
        raise Exception(
            f"配置错误: . "
            "请运行 'ironclaw onboard' 进行配置，或设置必需的环境变量"
        )

    # 在通道设置之前初始化会话管理器
    # config.llm.session = SessionConfig(auth_base_url, session_path)
    session = await create_session_manager(config.llm.session)

    # 在追踪初始化之前创建日志广播器，以便WebLogLayer可以捕获所有事件。
    log_broadcaster = LogBroadcaster()  # 创建日志广播器

    # 使用可重载的EnvFilter初始化追踪，以便网关可以在运行时切换日志级别而无需重启。
    suppress_stderr = (
            config.channels.tui is not None and
            cli.message is None and
            hasattr(sys.modules, 'tui')  # 检查tui特性是否启用
    )

    log_level_handle = init_tracing(
        log_broadcaster,  # 日志广播器在Python中通常是单例，不需要Arc包装
        suppress_stderr
    )

    logger.debug("启动 IronClaw...")
    logger.debug(f"已加载智能体配置: {config.agent.name}")
    logger.debug(f"LLM backend: {config.llm.backend}")

    # ── 阶段1-5：通过AppBuilder构建所有核心组件 ────────────

    flags = AppBuilderFlags(no_db=cli.no_db)
    toml_path_buf = Path(toml_path) if toml_path else None

    components = await AppBuilder(
        config,
        flags,
        toml_path_buf,
        session,
        log_broadcaster
    ).build_all()

    config = components.config

    # ── 隧道设置 ───────────────────────────────────────────────────

    if enable_non_cli:
        config, active_tunnel = await ironclaw.tunnel.start_managed_tunnel(config)
    else:
        config = config
        active_tunnel = None

    # ── 编排器/容器作业管理器 ────────────────────────────
    # 编排器启动一个内部HTTP API（默认0.0.0.0:50051）用于沙箱工作器通信。
    # 在--cli-only模式下完全跳过，以遵守"无网络监听器"的约定。

    if enable_non_cli:
        orch = await ironclaw.orchestrator.setup_orchestrator(
            config,
            components.llm,
            components.db,
            components.secrets_store
        )
        container_job_manager = orch.container_job_manager
        job_event_tx = orch.job_event_tx
        prompt_queue = orch.prompt_queue
        docker_status = orch.docker_status
    else:
        container_job_manager = None
        job_event_tx = None
        prompt_queue = asyncio.Queue()  # 在Python中使用asyncio.Queue替代Arc<Mutex<HashMap>>
        docker_status = ironclaw.sandbox.DockerStatus.Disabled

    # 从docker_status派生用户可见的警告信息用于通道通知
    if docker_status == ironclaw.sandbox.DockerStatus.NotInstalled:
        docker_user_warning = (
            "Sandbox is enabled but Docker is not installed -- "
            "full_job routines will fail until Docker is available."
        )
    elif docker_status == ironclaw.sandbox.DockerStatus.NotRunning:
        docker_user_warning = (
            "Sandbox is enabled but Docker is not running -- "
            "full_job routines will fail until Docker is started."
        )
    else:
        docker_user_warning = None

    # ── 通道设置 ──────────────────────────────────────────────────

    # 扩展操作的默认用户ID（单用户模式）。
    ext_user_id = config.owner_id

    # 启动时激活的WASM通道在下面的enable_non_cli && wasm_channels_enabled门控内延迟解析。
    # 此处默认为空集合，以便后续自动激活块（以wasm_channel_runtime_state为条件）能够编译，
    # 而无需在--cli-only/WASM_CHANNELS_ENABLED=false运行时计算（并可能失败）设置存储读取。
    startup_active_wasm_channels = set()  # Python中使用set替代HashSet

    channels = ChannelManager()  # 创建通道管理器
    channel_names = []  # 通道名称列表
    loaded_wasm_channel_names = []  # 已加载的WASM通道名称列表
    wasm_channel_runtime_state = None  # WASM通道运行时状态

    # 创建stdin通道（REPL或TUI——互斥，两者都占用stdin）。
    # TUI有自己的配置，因此它不能依赖于CLI通道的启用。
    tui_mode = config.channels.tui is not None

    # TUI通道设置（需要tui特性）
    if tui_mode and cli.message is None:
        try:
            # 检查tui模块是否可用
            import ironclaw_tui
            import ironclaw.channels.tui as tui_module

            tool_names = await components.tools.list()
            tool_categories = tui_module.group_tools_by_prefix(tool_names)

            # 获取技能分类
            if components.skill_registry is not None:
                registry = components.skill_registry.read()  # 在Python中使用线程锁
                try:
                    skill_data = [
                        (s.manifest.name, s.manifest.activation.tags)
                        for s in registry.skills()
                    ]
                    skill_categories = tui_module.group_skills_by_tag(skill_data)
                finally:
                    pass  # 在Python中通常使用with语句管理锁
            else:
                skill_categories = []

            # 获取工作空间根目录
            workspace_root = Path.cwd()
            workspace_path = str(workspace_root)

            # 解析TUI布局
            if config.channels.tui is not None:
                layout = tui_module.resolve_tui_layout(config.channels.tui, workspace_root)
            else:
                layout = ironclaw_tui.TuiLayout.default()

            # 获取记忆计数和身份文件
            memory_count = 0
            identity_files = []
            if components.workspace is not None:
                try:
                    docs = await components.workspace.list_all()
                    memory_count = len(docs)
                    identity_names = ["AGENTS.md", "SOUL.md", "USER.md", "IDENTITY.md"]
                    for name in identity_names:
                        try:
                            await components.workspace.read(name)
                            identity_files.append(name)
                        except Exception:
                            pass
                except Exception:
                    memory_count = 0

            # 获取当前模型信息
            current_model = components.llm.model_name()

            # 获取上下文窗口大小
            context_window = None
            try:
                metadata = await asyncio.wait_for(
                    components.llm.model_metadata(),
                    timeout=5.0
                )
                if metadata.context_length is not None:
                    context_window = int(metadata.context_length)
            except asyncio.TimeoutError:
                logger.debug("TUI context metadata unavailable: model metadata timed out")
            except Exception as e:
                logger.debug(f"TUI context metadata unavailable: could not fetch model metadata: {e}")

            # 获取可用模型列表
            available_models = []
            try:
                models = await asyncio.wait_for(
                    components.llm.list_models(),
                    timeout=5.0
                )
                if models:
                    # 将当前模型移到列表首位
                    if current_model in models:
                        models.remove(current_model)
                    models.insert(0, current_model)
                    available_models = models
            except asyncio.TimeoutError:
                logger.debug("TUI model picker unavailable: model discovery timed out")
            except Exception as e:
                logger.debug(f"TUI model picker unavailable: could not list models: {e}")

            # 创建TUI通道
            tui_channel = ironclaw.channels.TuiChannel(
                config.owner_id,
                os.environ.get("CARGO_PKG_VERSION", "unknown"),  # Python中从环境变量获取版本
                current_model
            )
            tui_channel.with_context_window(context_window or 128000)
            tui_channel.with_layout(layout)
            tui_channel.with_log_broadcaster(log_broadcaster)
            tui_channel.with_tools(tool_categories)
            tui_channel.with_skills(skill_categories)
            tui_channel.with_workspace_path(workspace_path)
            tui_channel.with_memory_count(memory_count)
            tui_channel.with_identity_files(identity_files)
            tui_channel.with_available_models(available_models)

            await channels.add(tui_channel)
            channel_names.append("tui")
            logger.debug("TUI mode enabled")

        except ImportError:
            logger.warning(
                "TUI mode is configured but the 'tui' feature is not enabled. "
                "Falling back to REPL if CLI is enabled."
            )

    # REPL通道设置
    use_repl = not tui_mode or 'ironclaw_tui' not in sys.modules

    repl_channel = None
    if cli.message is not None:
        # 单消息模式
        repl_channel = ReplChannel.with_message_for_user(
            config.owner_id,
            cli.message
        )
    elif use_repl and config.channels.cli.enabled:
        repl_channel = ReplChannel.with_user_id(config.owner_id)
        repl_channel.suppress_banner()

    if repl_channel is not None:
        await channels.add(repl_channel)
        if cli.message is not None:
            logger.debug("Single message mode")
        else:
            channel_names.append("repl")
            logger.debug("REPL mode enabled")

    # 共享例程引擎插槽，用于网关和通用webhook入口。
    shared_routine_engine_slot = asyncio.Lock()  # 在Python中使用asyncio.Lock替代RwLock<Option>
    shared_routine_engine_slot._value = None  # 初始化为None

    # 收集webhook路由片段；单个WebhookServer托管所有路由。
    webhook_routes = []

    if enable_non_cli:
        webhook_state = ToolWebhookState(
            tools=components.tools,
            routine_engine=shared_routine_engine_slot,
            user_id=config.owner_id,
            secrets_store=components.secrets_store
        )
        webhook_routes.append(webhooks.routes(webhook_state))

    # 加载WASM通道并注册它们的webhook路由。
    # 确保通道目录存在，以便即使尚未安装任何通道时WASM运行时也能初始化——
    # 热激活需要运行时可用。
    if enable_non_cli and config.channels.wasm_channels_enabled:
        wasm_dir = Path(config.channels.wasm_channels_dir)
        try:
            wasm_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(
                f"Failed to create WASM channels directory: {wasm_dir} - {e}"
            )

    if (enable_non_cli and
            config.channels.wasm_channels_enabled and
            Path(config.channels.wasm_channels_dir).exists()):

        # 解析启动时激活的通道：当存在持久化状态时，以持久化状态为准；
        # 否则回退到设置向导的channels.wasm_channels，以便无头安装
        # （无数据库、无Web UI）仍然自动激活配置中列出的通道。
        # 设置存储错误会传播——掩盖它们会静默地重新激活用户已停用的通道。
        # 在此处解析（而非外部作用域），以便损坏的activated_channels行
        # 仅在通道即将恢复时才会导致启动失败。
        if components.extension_manager is not None:
            startup_active_channels = await components.extension_manager.load_startup_active_channels(
                ext_user_id,
                config.channels.configured_wasm_channels
            )
        else:
            startup_active_channels = ironclaw.extensions.naming.normalize_extension_names(
                config.channels.configured_wasm_channels
            )

        if components.extension_manager is not None:
            startup_active_wasm_channels = await startup_active_wasm_channel_names(
                components.extension_manager,
                ext_user_id,
                startup_active_channels
            )
        else:
            startup_active_wasm_channels = set(startup_active_channels)

        # 运行时层级的Telegram v1/v2排他性检查。之前的配置解析调用
        # （在ChannelsConfig::resolve中）只能看到v1的环境变量视图。
        # 持久化的activated_channels行可以独立于WASM_CHANNELS携带telegram，
        # 并且setup_wasm_channels会自动加载它们——因此仅环境变量的守卫
        # 会让v1与v2在同一webhook安装中共存。在此处使用持久化集合
        # 重新运行验证器来关闭这个漏洞。
        ironclaw.config.validate_telegram_v1_v2_exclusivity(
            config.channels,
            startup_active_wasm_channels if startup_active_wasm_channels else None
        )

        # 设置WASM通道
        wasm_result = await ironclaw.channels.wasm.setup_wasm_channels(
            config,
            components.secrets_store,
            components.extension_manager,
            components.db,
            channel_names,
            startup_active_wasm_channels,
            components.ownership_cache
        )

        if wasm_result is not None:
            loaded_wasm_channel_names = wasm_result.channel_names
            wasm_channel_runtime_state = (
                wasm_result.wasm_channel_runtime,
                wasm_result.pairing_store,
                wasm_result.wasm_channel_router
            )

            for name, channel in wasm_result.channels:
                channel_names.append(name)
                await channels.add(channel)

            if wasm_result.webhook_routes is not None:
                webhook_routes.append(wasm_result.webhook_routes)

    # 如果配置了Signal通道且非CLI-only模式，则添加Signal通道。
    if enable_non_cli and config.channels.signal is not None:
        signal_config = config.channels.signal
        signal_channel = SignalChannel(
            signal_config,
            components.db,
            components.ownership_cache
        )
        channel_names.append("signal")
        await channels.add(signal_channel)

        safe_url = SignalChannel.redact_url(signal_config.http_url)
        logger.debug(f"Signal channel enabled - url: {safe_url}")

        if not signal_config.allow_from:
            logger.warning(
                "Signal channel has empty allow_from list - ALL messages will be DENIED."
            )

    # 如果配置了HTTP通道且非CLI-only模式，则添加HTTP通道。
    webhook_server_addr = None
    http_channel_state = None

    if enable_non_cli and config.channels.http is not None:
        http_config = config.channels.http
        http_channel = HttpChannel(http_config)

        if hasattr(http_channel, 'shared_state'):
            http_channel_state = http_channel.shared_state()

        webhook_routes.append(http_channel.routes())
        host, port = http_channel.addr()
        webhook_server_addr = socket.AddressFamily.AF_INET, (host, port)
        channel_names.append("http")
        await channels.add(http_channel)

        logger.debug(f"HTTP channel enabled on {http_config.host}:{http_config.port}")

    # 如果有路由注册，则启动统一的webhook服务器。
    webhook_server = None
    if webhook_routes:
        if webhook_server_addr is None:
            webhook_server_addr = ('127.0.0.1', 8080)

        if webhook_server_addr[0] == '0.0.0.0':
            logger.warning(
                f"Webhook server is binding to {webhook_server_addr[0]} — "
                "it will be reachable from all network interfaces. "
                "Set HTTP_HOST=127.0.0.1 to restrict to localhost."
            )

        server = WebhookServer(WebhookServerConfig(addr=webhook_server_addr))
        for routes in webhook_routes:
            server.add_routes(routes)

        await server.start()
        webhook_server = asyncio.Lock()
        webhook_server._server = server

    # 注册生命周期钩子。
    active_tool_names = await components.tools.list()

    hook_bootstrap = await bootstrap_hooks(
        components.hooks,
        components.workspace,
        config.wasm.tools_dir,
        config.channels.wasm_channels_dir,
        active_tool_names,
        loaded_wasm_channel_names,
        components.dev_loaded_tool_names
    )

    logger.debug(
        f"Lifecycle hooks initialized - "
        f"bundled: {hook_bootstrap.bundled_hooks}, "
        f"plugin: {hook_bootstrap.plugin_hooks}, "
        f"workspace: {hook_bootstrap.workspace_hooks}, "
        f"outbound_webhooks: {hook_bootstrap.outbound_webhooks}, "
        f"errors: {hook_bootstrap.errors}"
    )

    # 重用AppBuilder准备的共享代理会话管理器。
    session_manager = components.agent_session_manager

    # 延迟调度器插槽——在Agent::new创建调度器后填充。
    # 允许CreateJobTool通过调度器分派本地作业，即使调度器在工具注册后才创建（先有鸡还是先有蛋的问题）。
    scheduler_slot = asyncio.Lock()
    scheduler_slot._value = None  # 初始化为None

    # 即使在--cli-only下也注册作业工具，以便调度器支持的作业仍然可用。
    # 仅当容器管理器运行时才注入沙箱依赖项。
    await components.tools.register_job_tools(
        components.context_manager,
        scheduler_slot,
        container_job_manager,
        components.db,
        job_event_tx,
        channels.inject_sender() if hasattr(channels, 'inject_sender') else None,
        prompt_queue if (config.sandbox.enabled and container_job_manager is not None) else None,
        components.secrets_store
    )

    # ── 网关通道 ────────────────────────────────────────────────

    gateway_url = None
    sse_manager = None

    if enable_non_cli and config.channels.gateway is not None:
        gw_config = config.channels.gateway
        gw = GatewayChannel(gw_config, config.owner_id)
        gw = gw.with_multi_tenant_mode(config.is_multi_tenant_deployment())
        gw = gw.with_llm_provider(components.llm)

        if components.workspace is not None:
            gw = gw.with_workspace(components.workspace)

        if components.db is not None:
            gw = gw.with_db_backing_from_config(
                config,
                components.db,
                components.embeddings
            )

        gw = gw.with_session_manager(session_manager)
        gw = gw.with_llm_session_manager(components.session)

        if components.llm_reload is not None:
            gw = gw.with_llm_reload(components.llm_reload)

        if toml_path_buf is not None:
            gw = gw.with_config_toml_path(toml_path_buf)

        gw = gw.with_log_broadcaster(log_broadcaster)
        gw = gw.with_log_level_handle(log_level_handle)
        gw = gw.with_tool_registry(components.tools)

        if components.db is not None:
            dispatcher = ironclaw.tools.dispatch.ToolDispatcher(
                components.tools,
                components.safety,
                components.db
            )
            gw = gw.with_tool_dispatcher(dispatcher)

        if components.extension_manager is not None:
            # 启用网关模式，以便MCP OAuth返回认证URL给前端，
            # 而不是在服务器上调用open::that()。
            gw_base = (
                    config.tunnel.public_url or
                    oauth_base_url(gw_config.host, gw_config.port)
            )
            await components.extension_manager.enable_gateway_mode(gw_base)
            gw = gw.with_extension_manager(components.extension_manager)

        if components.catalog_entries:
            gw = gw.with_registry_entries(components.catalog_entries)

        if components.db is not None:
            gw = gw.with_store(components.db)

            if components.settings_cache is not None:
                gw = gw.with_settings_cache(components.settings_cache)

            gw = gw.with_db_auth(components.db)

            pairing_store = ironclaw.pairing.PairingStore(
                components.db,
                components.ownership_cache
            )
            gw = gw.with_pairing_store(pairing_store)

            if components.secrets_store is not None:
                gw = gw.with_secrets_store(components.secrets_store)

            # 引导：从单用户配置创建第一个管理员用户，
            # 以便所有者立即出现在用户管理面板中。
            try:
                has_users = await components.db.has_any_users()
                if not has_users:
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc)
                    user = ironclaw.db.UserRecord(
                        id=config.owner_id,
                        email=None,
                        display_name=config.owner_id,
                        status="active",
                        role="admin",
                        created_at=now,
                        updated_at=now,
                        last_login_at=None,
                        created_by=None,
                        metadata={"source": "bootstrap"}
                    )

                    # 原子性地创建管理员用户和引导令牌。
                    auth_token = gw.auth_token()
                    if not auth_token:
                        try:
                            await components.db.create_user(user)
                        except Exception as e:
                            logger.warning(f"Failed to bootstrap admin user: {e}")
                    else:
                        hash_token = ironclaw.channels.web.auth.hash_token
                        token_hash = hash_token(auth_token)
                        prefix = auth_token[:8] if len(auth_token) >= 8 else auth_token
                        try:
                            await components.db.create_user_with_token(
                                user, "bootstrap", token_hash, prefix, None
                            )
                            logger.debug(f"Bootstrapped admin user from gateway config - user_id: {config.owner_id}")
                        except Exception as e:
                            logger.warning(f"Failed to bootstrap admin user: {e}")
            except Exception as e:
                logger.warning(f"Failed to check for existing users: {e}")

        if components.secrets_store is not None:
            gw = gw.with_secrets_store(components.secrets_store)

        if container_job_manager is not None:
            gw = gw.with_job_manager(container_job_manager)

        gw = gw.with_scheduler(scheduler_slot)
        gw = gw.with_routine_engine_slot(shared_routine_engine_slot)

        if components.skill_registry is not None:
            gw = gw.with_skill_registry(components.skill_registry)

        if components.skill_catalog is not None:
            gw = gw.with_skill_catalog(components.skill_catalog)

        gw = gw.with_cost_guard(components.cost_guard)
        gw = gw.with_oauth(config.oauth, gw_config.port)

        # 设置活动配置快照
        active_model = components.llm.model_name()
        enabled = channel_names.copy()
        enabled.append("gateway")

        gw = gw.with_active_config(
            ironclaw.channels.web.platform.state.ActiveConfigSnapshot(
                llm_backend=str(config.llm.backend),
                llm_model=active_model,
                enabled_channels=enabled,
                default_timezone=config.agent.default_timezone
            )
        )

        if config.sandbox.enabled:
            gw = gw.with_prompt_queue(prompt_queue)

            if job_event_tx is not None:
                # 在Python中设置事件转发任务
                async def forward_job_events():
                    while True:
                        try:
                            job_id, user_id, event = await job_event_tx.recv()  # 假设job_event_tx是asyncio.Queue
                            gw_state = gw.state()  # 假设返回网关状态对象
                            user_id_opt = user_id if user_id else None
                            await ironclaw.channels.web.dispatch_status_event(
                                gw_state.sse,
                                gw_state.multi_tenant_mode,
                                user_id_opt,
                                event
                            )
                        except asyncio.CancelledError:
                            break
                        except Exception:
                            pass

                asyncio.create_task(forward_job_events())

        # 持久化自动生成的认证令牌，使其在重启后仍然有效。
        # 网关认证仅依赖环境变量，因此写入引导.env文件而不是数据库设置，
        # 并机会性地删除任何遗留的数据库副本。
        if gw_config.auth_token is None:
            token_to_persist = gw.auth_token()

            async def persist_token():
                try:
                    await ironclaw.bootstrap.upsert_bootstrap_var(
                        "GATEWAY_AUTH_TOKEN",
                        token_to_persist
                    )
                    logger.debug("Persisted auto-generated gateway auth token to bootstrap env")
                except Exception as e:
                    logger.warning(f"Failed to persist auto-generated gateway auth token: {e}")

            asyncio.create_task(persist_token())

            if components.db is not None:
                async def remove_legacy_token():
                    try:
                        deleted = await components.db.delete_setting(
                            "default", "channels.gateway_auth_token"
                        )
                        if deleted:
                            logger.debug("Removed legacy gateway auth token from DB settings")
                    except Exception as e:
                        logger.warning(f"Failed to remove legacy gateway auth token from DB settings: {e}")

                asyncio.create_task(remove_legacy_token())

        gateway_url = f"http://{gw_config.host}:{gw_config.port}/?token={gw.auth_token()}"
        logger.debug(f"Web UI: http://{gw_config.host}:{gw_config.port}/")

        # 在将gw移入channels之前捕获SSE发送器和例程引擎插槽。
        # 重要提示：这必须在所有with_*调用之后进行，因为rebuild_state会创建新的SseManager，这会使此发送器孤立。
        sse_manager = gw.state().sse  # 获取SSE管理器
        channel_names.append("gateway")
        await channels.add(gw)

    # ── 启动画面 ────────────────────────────────────────────────────

    boot_tool_count = components.tools.count()
    boot_llm_model = components.llm.model_name()
    boot_cheap_model = (
        components.cheap_llm.model_name()
        if components.cheap_llm is not None
        else None
    )

    if config.channels.cli.enabled and cli.message is None:
        boot_info = ironclaw.boot_screen.BootInfo(
            version=os.environ.get("CARGO_PKG_VERSION", "unknown"),
            agent_name=config.agent.name,
            llm_backend=str(config.llm.backend),
            llm_model=boot_llm_model,
            cheap_model=boot_cheap_model,
            db_backend="none" if cli.no_db else str(config.database.backend),
            db_connected=not cli.no_db,
            tool_count=boot_tool_count,
            gateway_url=gateway_url,
            embeddings_enabled=config.embeddings.enabled,
            embeddings_provider=config.embeddings.provider if config.embeddings.enabled else None,
            heartbeat_enabled=config.heartbeat.enabled,
            heartbeat_interval_secs=config.heartbeat.interval_secs,
            sandbox_enabled=config.sandbox.enabled,
            docker_status=docker_status,
            claude_code_enabled=config.claude_code.enabled,
            acp_enabled=config.acp.enabled,
            routines_enabled=config.routines.enabled,
            skills_enabled=config.skills.enabled,
            channels=channel_names,
            tunnel_url=(
                active_tunnel.public_url() if active_tunnel and active_tunnel.public_url()
                else config.tunnel.public_url
            ),
            tunnel_provider=active_tunnel.name() if active_tunnel else None,
            startup_elapsed=time.time() - startup_start
        )
        ironclaw.boot_screen.print_boot_screen(boot_info)

    # ── 运行代理 ──────────────────────────────────────────────────

    channels = channels  # 在Python中直接使用，无需Arc包装

    # 注册消息工具，用于向已连接的通道发送消息
    await components.tools.register_message_tools(
        channels,
        components.extension_manager
    )

    # 为WASM通道的热激活配置通道运行时。
    if components.extension_manager is not None and wasm_channel_runtime_state is not None:
        rt, ps, router = wasm_channel_runtime_state

        active_at_startup = set(loaded_wasm_channel_names)
        await components.extension_manager.set_active_channels(loaded_wasm_channel_names)
        await components.extension_manager.set_channel_runtime(
            channels,
            rt,
            ps,
            router,
            config.channels.wasm_channel_owner_ids
        )
        logger.debug("Channel runtime wired into extension manager for hot-activation")

        # 自动激活在启动时解析的WASM通道——要么从上一个会话持久化，
        # 要么在没有设置存储时由设置向导的channels.wasm_channels配置提供。
        # 中继通道通过下面的restore_relay_channels()单独处理。
        for name in startup_active_wasm_channels:
            if name in active_at_startup:
                continue
            if await components.extension_manager.is_relay_channel(name, ext_user_id):
                continue

            try:
                result = await components.extension_manager.ensure_extension_ready(
                    name,
                    ext_user_id,
                    ironclaw.extensions.EnsureReadyIntent.ExplicitActivate
                )

                if result.outcome == ironclaw.extensions.EnsureReadyOutcome.Ready:
                    message = (
                        result.activation.message
                        if result.activation and result.activation.message
                        else f"Channel '{name}' already ready"
                    )
                    logger.debug(f"Auto-activated startup WASM channel - channel: {name}, message: {message}")

                elif result.outcome == ironclaw.extensions.EnsureReadyOutcome.NeedsAuth:
                    logger.warning(
                        f"Startup WASM channel still needs authentication - "
                        f"channel: {name}, instructions: {result.auth.instructions()}"
                    )

                elif result.outcome == ironclaw.extensions.EnsureReadyOutcome.NeedsSetup:
                    logger.warning(
                        f"Startup WASM channel still needs setup - "
                        f"channel: {name}, instructions: {result.instructions}"
                    )

            except Exception as e:
                logger.warning(f"Failed to auto-activate startup WASM channel - channel: {name}, error: {e}")

    # 中继恢复可以发出出站中继调用并热添加活动通道。
    # 在--cli-only下与其它非CLI通道激活路径一起抑制。
    if enable_non_cli and components.extension_manager is not None:
        await components.extension_manager.set_relay_channel_manager(channels)
        await components.extension_manager.restore_relay_channels(ext_user_id)

    # 将SSE发送器接入扩展管理器以广播状态事件。
    if components.extension_manager is not None and sse_manager is not None:
        await components.extension_manager.set_sse_sender(sse_manager)

    # 将SSE接入plan_update工具以实时广播计划进度。
    if sse_manager is not None:
        components.tools.register_plan_tools(sse_manager)

    # 在代理启动前为跟踪记录快照记忆。
    # 记录器位于ironclaw_llm中，不能依赖主机的Workspace类型，
    # 因此我们在此处物化条目。
    if components.recording_handle is not None and components.workspace is not None:
        entries = []
        try:
            paths = await components.workspace.list_all()
            for path in paths:
                try:
                    doc = await components.workspace.read(path)
                    entries.append(ironclaw_llm.MemorySnapshotEntry(
                        path=doc.path,
                        content=doc.content
                    ))
                except Exception as e:
                    logger.debug(f"Skipped memory doc in snapshot - path: {path}, error: {e}")
        except Exception as e:
            logger.warning(f"Failed to list memory documents; trace will have empty memory snapshot - error: {e}")

        await components.recording_handle.snapshot_memory(entries)

    # 创建HTTP拦截器链
    interceptors = []
    if components.http_interceptor is not None:
        interceptors.append(components.http_interceptor)
    if components.recording_handle is not None:
        recording_interceptor = components.recording_handle.http_interceptor()
        if recording_interceptor is not None:
            interceptors.append(recording_interceptor)

    http_interceptor = ironclaw.http_intercept.chain(interceptors)

    # 在上下文管理器被移动到Agent::new()之前克隆它，用于回收器
    reaper_context_manager = components.context_manager

    # 在AppComponents被消费之前捕获设置存储，用于SIGHUP处理器。
    # 优先使用工作空间支持的适配器（以便SIGHUP驱动的配置重载能够
    # 获取通过工作空间写入的设置），当没有配置工作空间时回退到原始数据库。
    sighup_settings_store = (
            components.settings_store or
            components.db
    )

    sighup_settings_cache = components.settings_cache

    # 创建认证管理器
    secrets_store = components.tools.secrets_store() if hasattr(components.tools, 'secrets_store') else None
    auth_manager = None
    if secrets_store is not None:
        auth_manager = ironclaw.auth.extension.AuthManager(
            secrets_store,
            components.skill_registry,
            components.extension_manager,
            components.tools
        )

    # 创建代理依赖项
    deps = AgentDeps(
        owner_id=config.owner_id,
        settings_store=components.settings_store,
        store=components.db,
        llm=components.llm,
        cheap_llm=components.cheap_llm,
        safety=components.safety,
        tools=components.tools,
        workspace=components.workspace,
        extension_manager=components.extension_manager,
        skill_registry=components.skill_registry,
        skill_catalog=components.skill_catalog,
        skills_config=config.skills,
        hooks=components.hooks,
        auth_manager=auth_manager,
        cost_guard=components.cost_guard,
        sse_tx=sse_manager,
        http_interceptor=http_interceptor,
        transcription=(
            ironclaw_llm.transcription.TranscriptionMiddleware(config.transcription.create_provider())
            if config.transcription.create_provider() is not None
            else None
        ),
        document_extraction=ironclaw.document_extraction.DocumentExtractionMiddleware(),
        sandbox_readiness=(
            ironclaw.agent.routine_engine.SandboxReadiness.DisabledByConfig
            if not config.sandbox.enabled or docker_status == ironclaw.sandbox.DockerStatus.Disabled
            else ironclaw.agent.routine_engine.SandboxReadiness.Available
            if docker_status.is_ok()
            else ironclaw.agent.routine_engine.SandboxReadiness.DockerUnavailable
        ),
        builder=components.builder,
        llm_backend=config.llm.backend,
        tenant_rates=ironclaw.tenant.TenantRateRegistry(
            config.agent.max_llm_concurrent_per_user or 4,
            config.agent.max_jobs_concurrent_per_user or 3
        ),
        # 在配置加载时由Config::with_runtime_overrides解析。
        # 调度器通过tool_definitions_visible_under(policy)路由面向模型的工具列表，
        # 以便在模型调用之前隐藏配置文件不可能的功能
        # （例如在多租户托管下的提供者主机shell）。（#3045 PR 4 + PR 5）。
        runtime_policy=config.runtime.effective_policy if config.runtime.effective_policy else None
    )

    channels_for_warnings = channels

    # 创建代理
    agent = Agent(
        config.agent,
        deps,
        channels,
        config.heartbeat,
        config.hygiene,
        config.routines,
        components.context_manager,
        session_manager
    )

    # 现在Agent（及其调度器）存在后，填充调度器插槽。
    async with scheduler_slot:
        scheduler_slot._value = agent.scheduler()

    # 为孤儿容器清理启动沙箱回收器
    if container_job_manager is not None:
        reaper_config = ReaperConfig(
            scan_interval=config.sandbox.reaper_interval_secs,
            orphan_threshold=config.sandbox.orphan_threshold_secs
        )

        async def run_reaper():
            try:
                reaper = await SandboxReaper(
                    container_job_manager,
                    reaper_context_manager,
                    reaper_config
                )
                await reaper.run()
            except Exception as e:
                logger.error(f"Sandbox reaper failed to initialize: {e}")

        asyncio.create_task(run_reaper())

    # 给代理提供例程引擎插槽，以便它可以将引擎暴露给网关。
    agent.set_routine_engine_slot(shared_routine_engine_slot)

    # 为后台任务的干净关闭准备广播通道
    shutdown_tx = asyncio.Queue()  # 在Python中使用asyncio.Queue作为关闭信号通道
    await shutdown_tx.put(())  # 初始化为空

    # ── SIGHUP处理器（仅Unix系统） ────────────────────────────────

    if hasattr(signal, 'SIGHUP'):  # Unix系统
        # 收集所有支持密钥更新的通道
        secret_updaters = []
        if http_channel_state is not None:
            secret_updaters.append(http_channel_state)

        async def sighup_handler():
            """处理SIGHUP信号以热重载HTTP webhook配置"""
            try:
                # 设置信号处理器
                loop = asyncio.get_event_loop()
                sighup_event = asyncio.Event()

                def handle_sighup():
                    sighup_event.set()

                loop.add_signal_handler(signal.SIGHUP, handle_sighup)

                while True:
                    # 等待SIGHUP信号或关闭信号
                    await sighup_event.wait()
                    sighup_event.clear()

                    logger.info("SIGHUP received — reloading HTTP webhook config")

                    # 刷新设置缓存，以便直接数据库编辑被获取。
                    if sighup_settings_cache is not None:
                        await sighup_settings_cache.flush()
                        logger.debug("flushed settings cache")

                    # 从数据库注入通道密钥到线程安全覆盖层
                    # （类似于LLM提供者的inject_llm_keys_from_secrets）
                    if components.secrets_store is not None:
                        try:
                            webhook_secret = await components.secrets_store.get_decrypted(
                                config.owner_id, "http_webhook_secret"
                            )
                            if webhook_secret is not None:
                                # 线程安全：使用INJECTED_VARS互斥锁而不是不安全的std::env::set_var
                                # Config::from_env()将通过optional_env()从覆盖层读取
                                ironclaw.config.inject_single_var(
                                    "HTTP_WEBHOOK_SECRET",
                                    webhook_secret.expose()
                                )
                                logger.debug("Injected HTTP_WEBHOOK_SECRET from secrets store")
                        except Exception as e:
                            logger.warning(f"Failed to inject HTTP webhook secret: {e}")

                    # 重新加载配置（现在已将密钥注入到环境中）
                    try:
                        if sighup_settings_store is not None:
                            new_config = await ironclaw.config.Config.from_db(
                                sighup_settings_store,
                                config.owner_id
                            )
                        else:
                            new_config = await ironclaw.config.Config.from_env()
                    except Exception as e:
                        logger.error(f"SIGHUP config reload failed: {e}")
                        continue

                    new_http = new_config.channels.http
                    if new_http is None:
                        logger.warning("SIGHUP: HTTP channel no longer configured, skipping")
                        continue

                    # 计算新的套接字地址
                    try:
                        new_addr = (new_http.host, new_http.port)
                    except Exception as e:
                        logger.error(f"SIGHUP: invalid addr in config: {e}")
                        continue

                    # 如果地址更改，则重启监听器。
                    # 两阶段方法：在锁外绑定，然后在锁下交换。
                    restart_failed = False
                    if webhook_server is not None and hasattr(webhook_server, '_server'):
                        server = webhook_server._server
                        old_addr = server.current_addr()

                        if old_addr != new_addr:
                            logger.info(f"SIGHUP: HTTP addr {old_addr} -> {new_addr}, restarting listener")

                            router = server.merged_router_clone()
                            if router is not None:
                                try:
                                    # 阶段1：在不持有锁的情况下绑定新监听器。
                                    listener = await asyncio.start_server(
                                        router,
                                        host=new_addr[0],
                                        port=new_addr[1]
                                    )

                                    # 阶段2：在锁下交换状态（内部无await）。
                                    # 注意：这是简化实现
                                    server.install_listener(new_addr, listener, router)

                                    logger.info(f"SIGHUP: webhook server restarted on {new_addr}")
                                except Exception as e:
                                    logger.error(f"SIGHUP: failed to bind to {new_addr}: {e}")
                                    restart_failed = True
                            else:
                                logger.error("SIGHUP: cannot restart — server was never started")
                                restart_failed = True
                        else:
                            logger.debug(f"SIGHUP: addr unchanged ({old_addr})")

                    # 更新所有配置通道中的密钥（如果重启成功或不需要重启）
                    if not restart_failed:
                        new_secret = (
                            new_http.webhook_secret.expose_secret()
                            if new_http.webhook_secret is not None
                            else None
                        )

                        # 更新所有支持密钥交换的通道
                        for updater in secret_updaters:
                            await updater.update_secret(new_secret)

            except asyncio.CancelledError:
                logger.debug("SIGHUP handler shutting down")
            except Exception as e:
                logger.error(f"SIGHUP handler error: {e}")

        asyncio.create_task(sighup_handler())

    # 如果沙箱不可用（Docker缺失/未运行），则通知用户
    if docker_user_warning is not None:
        async def send_warning():
            # 延迟让通道在发送警告之前完成连接。
            # 5秒是慷慨的，但可以避免消息在慢速启动时丢失。
            await asyncio.sleep(5)
            logger.debug("Sending sandbox-unavailable warning to connected channels")

            response = ironclaw.channels.OutgoingResponse(
                content=f"Warning: {docker_user_warning}",
                thread_id=None,
                attachments=[],
                inline_attachments=[],
                metadata={
                    "source": "system",
                    "type": "warning"
                }
            )

            try:
                await channels_for_warnings.broadcast_all("default", response)
            except Exception:
                pass

        asyncio.create_task(send_warning())

    # 运行代理
    await agent.run()

    # ── 关闭 ────────────────────────────────────────────────────────

    # 发送关闭信号
    await shutdown_tx.put(())

    # 关闭所有stdio MCP服务器子进程。
    await components.mcp_process_manager.shutdown_all()

    # 如果启用了LLM跟踪记录，则刷新
    if components.recording_handle is not None:
        try:
            await components.recording_handle.flush()
        except Exception as e:
            logger.warning(f"Failed to write LLM trace: {e}")

    # 关闭webhook服务器
    if webhook_server is not None and hasattr(webhook_server, '_server'):
        server = webhook_server._server
        shutdown_tx, handle = server.begin_shutdown()
        if shutdown_tx is not None:
            await shutdown_tx
        if handle is not None:
            await handle

    # 停止活动隧道
    if active_tunnel is not None:
        logger.debug(f"Stopping {active_tunnel.name()} tunnel...")
        try:
            await active_tunnel.stop()
        except Exception as e:
            logger.warning(f"Failed to stop tunnel cleanly: {e}")

    logger.debug("Agent shutdown complete")
