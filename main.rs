//! IronClaw - 主入口点。

// 导入标准库中的同步原语和时间处理
use std::sync::Arc;
use std::time::Duration;

// 导入命令行参数解析库
use clap::Parser;

// 导入 IronClaw 内部各个模块
use ironclaw::{
    agent::{Agent, AgentDeps}, // 代理及其依赖
    app::{AppBuilder, AppBuilderFlags}, // 应用构建器及标志
    channels::{ // 各种通信通道
        ChannelManager, GatewayChannel, HttpChannel, ReplChannel, SignalChannel, WebhookServer,
        WebhookServerConfig,
        wasm::{WasmChannelRouter, WasmChannelRuntime}, // WASM 通道相关
        web::log_layer::LogBroadcaster, // Web 日志广播器
    },
    cli::{ // 命令行接口子命令
        Cli, Command, run_mcp_command, run_pairing_command, run_service_command,
        run_status_command, run_tool_command,
    },
    config::Config, // 配置管理
    hooks::bootstrap_hooks, // 生命周期钩子引导
    llm::create_session_manager, // 创建会话管理器
    orchestrator::{ReaperConfig, SandboxReaper}, // 容器任务编排器
    pairing::PairingStore, // 配对存储
    tracing_fmt::{init_cli_tracing, init_worker_tracing}, // 追踪初始化
    webhooks::{self, ToolWebhookState}, // Webhook 相关
};

// 条件编译：Unix 平台特有功能
#[cfg(unix)]
use ironclaw::channels::ChannelSecretUpdater;
// 条件编译：如果启用了 postgres 或 libsql 特性，则导入设置向导
#[cfg(any(feature = "postgres", feature = "libsql"))]
use ironclaw::setup::{SetupConfig, SetupWizard};

/// 同步入口点。在 Tokio 运行时启动前加载 .env 文件，
/// 确保此时 `std::env::set_var` 是安全的（尚无工作线程）。
fn main() -> anyhow::Result<()> {
    // 加载 .env 文件中的环境变量
    let _ = dotenvy::dotenv();
    // 加载 IronClaw 特有的环境变量
    ironclaw::bootstrap::load_ironclaw_env();

    // 创建多线程 Tokio 运行时并阻塞执行异步主函数
    tokio::runtime::Builder::new_multi_thread()
        .enable_all() // 启用所有 I/O 和计时器功能
        .build()?     // 构建运行时
        .block_on(async_main()) // 阻塞运行异步主函数
}

/// 异步主函数，处理实际业务逻辑
async fn async_main() -> anyhow::Result<()> {
    // 解析命令行参数
    let cli = Cli::parse();

    // 首先处理非代理命令（它们不需要完整设置）
    match &cli.command {
        Some(Command::Tool(tool_cmd)) => {
            // 初始化 CLI 追踪
            init_cli_tracing();
            // 运行工具命令
            return run_tool_command(tool_cmd.clone()).await;
        }
        Some(Command::Config(config_cmd)) => {
            init_cli_tracing();
            // 运行配置命令
            return ironclaw::cli::run_config_command(config_cmd.clone()).await;
        }
        Some(Command::Registry(registry_cmd)) => {
            init_cli_tracing();
            // 运行注册表命令
            return ironclaw::cli::run_registry_command(registry_cmd.clone()).await;
        }
        Some(Command::Channels(channels_cmd)) => {
            init_cli_tracing();
            // 运行通道命令
            return ironclaw::cli::run_channels_command(
                channels_cmd.clone(),
                cli.config.as_deref(),
            )
            .await;
        }
        Some(Command::Routines(routines_cmd)) => {
            init_cli_tracing();
            // 运行例程 CLI 命令
            return ironclaw::cli::run_routines_cli(routines_cmd, cli.config.as_deref()).await;
        }
        Some(Command::Mcp(mcp_cmd)) => {
            init_cli_tracing();
            // 运行 MCP 命令
            return run_mcp_command(*mcp_cmd.clone()).await;
        }
        Some(Command::Memory(mem_cmd)) => {
            init_cli_tracing();
            // 运行内存命令
            return ironclaw::cli::run_memory_command(mem_cmd).await;
        }
        Some(Command::Pairing(pairing_cmd)) => {
            init_cli_tracing();
            // 运行配对命令
            return run_pairing_command(pairing_cmd.clone()).map_err(|e| anyhow::anyhow!("{}", e));
        }
        Some(Command::Service(service_cmd)) => {
            init_cli_tracing();
            // 运行服务命令
            return run_service_command(service_cmd);
        }
        Some(Command::Skills(skills_cmd)) => {
            init_cli_tracing();
            // 运行技能命令
            return ironclaw::cli::run_skills_command(skills_cmd.clone(), cli.config.as_deref())
                .await;
        }
        Some(Command::Logs(logs_cmd)) => {
            init_cli_tracing();
            // 运行日志命令
            return ironclaw::cli::run_logs_command(logs_cmd.clone(), cli.config.as_deref()).await;
        }
        Some(Command::Doctor) => {
            init_cli_tracing();
            // 运行诊断命令
            return ironclaw::cli::run_doctor_command().await;
        }
        Some(Command::Status) => {
            init_cli_tracing();
            // 运行状态命令
            return run_status_command().await;
        }
        Some(Command::Completion(completion)) => {
            init_cli_tracing();
            // 运行自动补全生成命令
            return completion.run();
        }
        #[cfg(feature = "import")]
        Some(Command::Import(import_cmd)) => {
            init_cli_tracing();
            // 从环境加载配置
            let config = ironclaw::config::Config::from_env().await?;
            // 运行导入命令
            return ironclaw::cli::run_import_command(import_cmd, &config).await;
        }
        Some(Command::Worker {
            job_id,
            orchestrator_url,
            max_iterations,
        }) => {
            // 初始化工作进程追踪（带线程 ID）
            init_worker_tracing();
            // 运行工作进程
            return ironclaw::worker::run_worker(*job_id, orchestrator_url, *max_iterations).await;
        }
        Some(Command::ClaudeBridge {
            job_id,
            orchestrator_url,
            max_turns,
            model,
        }) => {
            init_worker_tracing();
            // 运行 Claude 桥接模式
            return ironclaw::worker::run_claude_bridge(
                *job_id,
                orchestrator_url,
                *max_turns,
                model,
            )
            .await;
        }
        Some(Command::Onboard {
            skip_auth,
            channels_only,
            provider_only,
            quick,
        }) => {
            // 如果启用了数据库特性，则运行设置向导
            #[cfg(any(feature = "postgres", feature = "libsql"))]
            {
                // 构建设置配置
                let config = SetupConfig {
                    skip_auth: *skip_auth,
                    channels_only: *channels_only,
                    provider_only: *provider_only,
                    quick: *quick,
                };
                // 从配置和可选的 TOML 文件创建设置向导
                let mut wizard =
                    SetupWizard::try_with_config_and_toml(config, cli.config.as_deref())?;
                // 运行向导
                wizard.run().await?;
            }
            // 如果未启用数据库特性，则输出错误提示
            #[cfg(not(any(feature = "postgres", feature = "libsql")))]
            {
                let _ = (skip_auth, channels_only, provider_only, quick);
                eprintln!("Onboarding wizard requires the 'postgres' or 'libsql' feature.");
            }
            return Ok(());
        }
        None | Some(Command::Run) => {
            // 继续运行代理（无特定命令或显式 run 命令）
        }
    }

    // ── PID 锁（防止多实例运行）────────────────────────────────────────
    // 获取 PID 锁，防止多个实例同时运行
    let _pid_lock = match ironclaw::bootstrap::PidLock::acquire() {
        Ok(lock) => Some(lock),
        Err(ironclaw::bootstrap::PidLockError::AlreadyRunning { pid }) => {
            // 如果已有实例在运行，报错退出
            anyhow::bail!(
                "另一个 IronClaw 实例已在运行 (PID {}). \
                 如果确认没有实例，请删除过期的 PID 文件: {}",
                pid,
                ironclaw::bootstrap::pid_lock_path().display()
            );
        }
        Err(e) => {
            // 其他错误仅警告，继续运行
            eprintln!("警告: 无法获取 PID 锁: {}", e);
            eprintln!("继续运行，没有 PID 锁保护。");
            None
        }
    };

    // ── 代理启动 ────────────────────────────────────────────────────────

    // 增强的首次运行检测（如果未禁用 onboard 且需要引导）
    #[cfg(any(feature = "postgres", feature = "libsql"))]
    if !cli.no_onboard
        && let Some(reason) = ironclaw::setup::check_onboard_needed()
    {
        // 输出引导原因并启动快速向导
        println!("需要引导: {}", reason);
        println!();
        let mut wizard = SetupWizard::try_with_config_and_toml(
            SetupConfig {
                quick: true,
                ..Default::default()
            },
            cli.config.as_deref(),
        )?;
        wizard.run().await?;
    }

    // 从环境变量、磁盘文件以及可选的 TOML 文件加载初始配置（数据库尚不可用）。
    // 此时可能缺少凭据——这没问题。LlmConfig::resolve() 会优雅处理，
    // 而 AppBuilder::build_all() 在从加密数据库加载密钥后会重新解析。
    let toml_path = cli.config.as_deref();
    let config = match Config::from_env_with_toml(toml_path).await {
        Ok(c) => c,
        Err(ironclaw::error::ConfigError::MissingRequired { key, hint }) => {
            // 缺少必需配置项时给出友好提示
            anyhow::bail!(
                "配置错误: 缺少必需设置 '{}'. {}. \
                 请运行 'ironclaw onboard' 进行配置，或设置相应的环境变量。",
                key,
                hint
            );
        }
        Err(e) => return Err(e.into()),
    };

    // 在通道设置前初始化会话管理器
    let session = create_session_manager(config.llm.session.clone()).await;

    // 创建日志广播器，以便 WebLogLayer 能捕获所有事件
    let log_broadcaster = Arc::new(LogBroadcaster::new());

    // 使用可重载的 EnvFilter 初始化追踪，以便网关能在运行时切换日志级别而无需重启。
    let log_level_handle =
        ironclaw::channels::web::log_layer::init_tracing(Arc::clone(&log_broadcaster));

    // 记录启动信息
    tracing::debug!("正在启动 IronClaw...");
    tracing::debug!("已加载代理配置: {}", config.agent.name);
    tracing::debug!("LLM 后端: {}", config.llm.backend);

    // ── 阶段 1-5: 通过 AppBuilder 构建所有核心组件 ─────────────────────

    // 构建标志：是否禁用数据库
    let flags = AppBuilderFlags { no_db: cli.no_db };
    // 创建应用构建器并构建所有组件
    let components = AppBuilder::new(
        config,
        flags,
        toml_path.map(std::path::PathBuf::from),
        session.clone(),
        Arc::clone(&log_broadcaster),
    )
    .build_all()
    .await?;

    // 获取构建后的配置（可能已被组件更新）
    let config = components.config;

    // ── 隧道设置 ───────────────────────────────────────────────────────

    // 启动托管的隧道（如 ngrok）
    let (config, active_tunnel) = ironclaw::tunnel::start_managed_tunnel(config).await;

    // ── 编排器 / 容器任务管理器 ────────────────────────────────────────

    // 设置编排器，用于管理容器任务
    let orch = ironclaw::orchestrator::setup_orchestrator(
        &config,
        &components.llm,
        components.db.as_ref(),
        components.secrets_store.as_ref(),
    )
    .await;
    let container_job_manager = orch.container_job_manager; // 容器任务管理器
    let job_event_tx = orch.job_event_tx; // 任务事件发送器
    let prompt_queue = orch.prompt_queue; // 提示队列
    let docker_status = orch.docker_status; // Docker 状态

    // ── 通道设置 ────────────────────────────────────────────────────────

    // 创建通道管理器
    let channels = ChannelManager::new();
    let mut channel_names: Vec<String> = Vec::new(); // 存储已启用的通道名称
    let mut loaded_wasm_channel_names: Vec<String> = Vec::new(); // 已加载的 WASM 通道名称
    #[allow(clippy::type_complexity)]
    let mut wasm_channel_runtime_state: Option<(
        Arc<WasmChannelRuntime>,
        Arc<PairingStore>,
        Arc<WasmChannelRouter>,
    )> = None; // WASM 通道运行时状态

    // 创建 CLI 通道（REPL）
    let repl_channel = if let Some(ref msg) = cli.message {
        // 如果提供了单条消息，创建只执行一条消息的 REPL 通道
        Some(ReplChannel::with_message_for_user(
            config.owner_id.clone(),
            msg.clone(),
        ))
    } else if config.channels.cli.enabled {
        // 如果配置启用 CLI 通道，创建 REPL 通道
        let repl = ReplChannel::with_user_id(config.owner_id.clone());
        repl.suppress_banner(); // 抑制启动横幅
        Some(repl)
    } else {
        None
    };

    // 如果 REPL 通道存在，将其添加到通道管理器
    if let Some(repl) = repl_channel {
        channels.add(Box::new(repl)).await;
        if cli.message.is_some() {
            tracing::debug!("单条消息模式");
        } else {
            channel_names.push("repl".to_string());
            tracing::debug!("REPL 模式已启用");
        }
    }

    // 共享的例程引擎槽，供网关和通用 Webhook 入口使用
    let shared_routine_engine_slot: ironclaw::channels::web::server::RoutineEngineSlot =
        Arc::new(tokio::sync::RwLock::new(None));

    // 收集 Webhook 路由片段；一个 WebhookServer 将托管所有路由
    let mut webhook_routes: Vec<axum::Router> = Vec::new();

    // 添加工具 Webhook 路由
    webhook_routes.push(webhooks::routes(ToolWebhookState {
        tools: Arc::clone(&components.tools),
        routine_engine: Arc::clone(&shared_routine_engine_slot),
        user_id: config.owner_id.clone(),
        secrets_store: components.secrets_store.clone(),
    }));

    // 加载 WASM 通道并注册其 Webhook 路由
    if config.channels.wasm_channels_enabled && config.channels.wasm_channels_dir.exists() {
        let wasm_result = ironclaw::channels::wasm::setup_wasm_channels(
            &config,
            &components.secrets_store,
            components.extension_manager.as_ref(),
            components.db.as_ref(),
        )
        .await;

        if let Some(result) = wasm_result {
            loaded_wasm_channel_names = result.channel_names; // 记录已加载的 WASM 通道名称
            wasm_channel_runtime_state = Some((
                result.wasm_channel_runtime,
                result.pairing_store,
                result.wasm_channel_router,
            ));
            // 将每个 WASM 通道添加到通道管理器
            for (name, channel) in result.channels {
                channel_names.push(name);
                channels.add(channel).await;
            }
            // 如果有 Webhook 路由，则添加
            if let Some(routes) = result.webhook_routes {
                webhook_routes.push(routes);
            }
        }
    }

    // 如果配置了 Signal 通道且非 CLI-only 模式，添加 Signal 通道
    if !cli.cli_only
        && let Some(ref signal_config) = config.channels.signal
    {
        let signal_channel = SignalChannel::new(signal_config.clone())?;
        channel_names.push("signal".to_string());
        channels.add(Box::new(signal_channel)).await;
        let safe_url = SignalChannel::redact_url(&signal_config.http_url);
        tracing::debug!(
            url = %safe_url,
            "Signal 通道已启用"
        );
        if signal_config.allow_from.is_empty() {
            tracing::warn!(
                "Signal 通道的 allow_from 列表为空 - 所有消息都将被拒绝。"
            );
        }
    }

    // 如果配置了 HTTP 通道且非 CLI-only 模式，添加 HTTP 通道
    let mut webhook_server_addr: Option<std::net::SocketAddr> = None;
    #[cfg(unix)]
    let mut http_channel_state: Option<Arc<ironclaw::channels::HttpChannelState>> = None;
    if !cli.cli_only
        && let Some(ref http_config) = config.channels.http
    {
        let http_channel = HttpChannel::new(http_config.clone());
        #[cfg(unix)]
        {
            http_channel_state = Some(http_channel.shared_state()); // 保存状态以便后续更新密钥
        }
        webhook_routes.push(http_channel.routes()); // 获取其路由并加入统一 Webhook 服务器
        let (host, port) = http_channel.addr();
        webhook_server_addr = Some(
            format!("{}:{}", host, port)
                .parse()
                .expect("HttpConfig 的 host:port 必须是有效的 SocketAddr"),
        );
        channel_names.push("http".to_string());
        channels.add(Box::new(http_channel)).await;
        tracing::debug!(
            "HTTP 通道已启用，监听地址: {}:{}",
            http_config.host,
            http_config.port
        );
    }

    // 如果注册了任何路由，启动统一的 Webhook 服务器
    let webhook_server: Option<Arc<tokio::sync::Mutex<WebhookServer>>> = if !webhook_routes
        .is_empty()
    {
        let addr =
            webhook_server_addr.unwrap_or_else(|| std::net::SocketAddr::from(([0, 0, 0, 0], 8080)));
        if addr.ip().is_unspecified() {
            tracing::warn!(
                "Webhook 服务器绑定到 {} — 它将可从所有网络接口访问。\
                 设置 HTTP_HOST=127.0.0.1 可限制为仅本地访问。",
                addr.ip()
            );
        }
        let mut server = WebhookServer::new(WebhookServerConfig { addr });
        for routes in webhook_routes {
            server.add_routes(routes); // 添加所有收集的路由
        }
        server.start().await?; // 启动服务器
        Some(Arc::new(tokio::sync::Mutex::new(server)))
    } else {
        None
    };

    // 注册生命周期钩子
    let active_tool_names = components.tools.list().await; // 获取当前活跃的工具名称列表

    // 引导钩子：加载捆绑的、插件的、工作区的钩子以及出站 Webhook
    let hook_bootstrap = bootstrap_hooks(
        &components.hooks,
        components.workspace.as_ref(),
        &config.wasm.tools_dir,
        &config.channels.wasm_channels_dir,
        &active_tool_names,
        &loaded_wasm_channel_names,
        &components.dev_loaded_tool_names,
    )
    .await;
    tracing::debug!(
        bundled = hook_bootstrap.bundled_hooks,
        plugin = hook_bootstrap.plugin_hooks,
        workspace = hook_bootstrap.workspace_hooks,
        outbound_webhooks = hook_bootstrap.outbound_webhooks,
        errors = hook_bootstrap.errors,
        "生命周期钩子初始化完成"
    );

    // 复用 AppBuilder 准备的代理会话管理器
    let session_manager = Arc::clone(&components.agent_session_manager);

    // 懒加载调度器槽位——在 Agent::new 创建调度器后填充。
    // 这允许 CreateJobTool 通过调度器分发本地任务，尽管调度器在工具注册后才创建（鸡与蛋问题）。
    let scheduler_slot: ironclaw::tools::builtin::SchedulerSlot =
        Arc::new(tokio::sync::RwLock::new(None));

    // 注册任务工具（当 container_job_manager 可用时自动注入沙箱依赖）
    components.tools.register_job_tools(
        Arc::clone(&components.context_manager),
        Some(scheduler_slot.clone()),
        container_job_manager.clone(),
        components.db.clone(),
        job_event_tx.clone(),
        Some(channels.inject_sender()), // 注入通道消息发送器
        if config.sandbox.enabled {
            Some(Arc::clone(&prompt_queue))
        } else {
            None
        },
        components.secrets_store.clone(),
    );

    // ── 网关通道 ────────────────────────────────────────────────────────

    let mut gateway_url: Option<String> = None; // 网关 URL
    let mut sse_sender: Option<
        tokio::sync::broadcast::Sender<ironclaw::channels::web::types::SseEvent>,
    > = None; // SSE 事件发送器
    if let Some(ref gw_config) = config.channels.gateway {
        // 创建网关通道并逐步配置
        let mut gw =
            GatewayChannel::new(gw_config.clone()).with_llm_provider(Arc::clone(&components.llm));
        if let Some(ref ws) = components.workspace {
            gw = gw.with_workspace(Arc::clone(ws));
        }
        gw = gw.with_session_manager(Arc::clone(&session_manager));
        gw = gw.with_log_broadcaster(Arc::clone(&log_broadcaster));
        gw = gw.with_log_level_handle(Arc::clone(&log_level_handle));
        gw = gw.with_tool_registry(Arc::clone(&components.tools));
        if let Some(ref ext_mgr) = components.extension_manager {
            // 启用网关模式，使得 MCP OAuth 将认证 URL 返回给前端，而不是在服务器上调用 open::that()。
            let gw_base = config
                .tunnel
                .public_url
                .clone()
                .unwrap_or_else(|| format!("http://{}:{}", gw_config.host, gw_config.port));
            ext_mgr.enable_gateway_mode(gw_base).await;
            gw = gw.with_extension_manager(Arc::clone(ext_mgr));
        }
        if !components.catalog_entries.is_empty() {
            gw = gw.with_registry_entries(components.catalog_entries.clone());
        }
        if let Some(ref d) = components.db {
            gw = gw.with_store(Arc::clone(d));
        }
        if let Some(ref jm) = container_job_manager {
            gw = gw.with_job_manager(Arc::clone(jm));
        }
        gw = gw.with_scheduler(scheduler_slot.clone());
        gw = gw.with_routine_engine_slot(Arc::clone(&shared_routine_engine_slot));
        if let Some(ref sr) = components.skill_registry {
            gw = gw.with_skill_registry(Arc::clone(sr));
        }
        if let Some(ref sc) = components.skill_catalog {
            gw = gw.with_skill_catalog(Arc::clone(sc));
        }
        gw = gw.with_cost_guard(Arc::clone(&components.cost_guard));
        if config.sandbox.enabled {
            gw = gw.with_prompt_queue(Arc::clone(&prompt_queue));

            // 将任务事件广播到网关的 SSE 流
            if let Some(ref tx) = job_event_tx {
                let mut rx = tx.subscribe();
                let gw_state = Arc::clone(gw.state());
                tokio::spawn(async move {
                    while let Ok((_job_id, event)) = rx.recv().await {
                        gw_state.sse.broadcast(event);
                    }
                });
            }
        }

        // 持久化自动生成的认证令牌，使其在重启后仍然有效。
        // 写入 "default" 设置命名空间，即 Config::from_db() 读取的命名空间——注意不是网关通道的 user_id。
        if gw_config.auth_token.is_none() {
            let token_to_persist = gw.auth_token().to_string();
            if let Some(ref db) = components.db {
                let db = db.clone();
                tokio::spawn(async move {
                    if let Err(e) = db
                        .set_setting(
                            "default",
                            "channels.gateway_auth_token",
                            &serde_json::Value::String(token_to_persist),
                        )
                        .await
                    {
                        tracing::warn!("无法持久化自动生成的网关认证令牌: {e}");
                    } else {
                        tracing::debug!("已持久化自动生成的网关认证令牌到设置");
                    }
                });
            }
        }

        // 构造网关访问 URL（包含令牌）
        gateway_url = Some(format!(
            "http://{}:{}/?token={}",
            gw_config.host,
            gw_config.port,
            gw.auth_token()
        ));

        tracing::debug!("Web UI 地址: http://{}:{}/", gw_config.host, gw_config.port);

        // 捕获 SSE 发送器和例程引擎槽，然后将网关移入通道管理器。
        // 重要：这必须在所有 `with_*` 调用之后，因为 `rebuild_state` 会创建新的 SseManager，导致此发送器孤立。
        sse_sender = Some(gw.state().sse.sender());
        channel_names.push("gateway".to_string());
        channels.add(Box::new(gw)).await;
    }

    // ── 启动屏幕 ────────────────────────────────────────────────────────

    // 收集启动信息用于显示启动屏幕
    let boot_tool_count = components.tools.count();
    let boot_llm_model = components.llm.model_name().to_string();
    let boot_cheap_model = components
        .cheap_llm
        .as_ref()
        .map(|c| c.model_name().to_string());

    if config.channels.cli.enabled && cli.message.is_none() {
        let boot_info = ironclaw::boot_screen::BootInfo {
            version: env!("CARGO_PKG_VERSION").to_string(),
            agent_name: config.agent.name.clone(),
            llm_backend: config.llm.backend.to_string(),
            llm_model: boot_llm_model,
            cheap_model: boot_cheap_model,
            db_backend: if cli.no_db {
                "none".to_string()
            } else {
                config.database.backend.to_string()
            },
            db_connected: !cli.no_db,
            tool_count: boot_tool_count,
            gateway_url,
            embeddings_enabled: config.embeddings.enabled,
            embeddings_provider: if config.embeddings.enabled {
                Some(config.embeddings.provider.clone())
            } else {
                None
            },
            heartbeat_enabled: config.heartbeat.enabled,
            heartbeat_interval_secs: config.heartbeat.interval_secs,
            sandbox_enabled: config.sandbox.enabled,
            docker_status,
            claude_code_enabled: config.claude_code.enabled,
            routines_enabled: config.routines.enabled,
            skills_enabled: config.skills.enabled,
            channels: channel_names,
            tunnel_url: active_tunnel
                .as_ref()
                .and_then(|t| t.public_url())
                .or_else(|| config.tunnel.public_url.clone()),
            tunnel_provider: active_tunnel.as_ref().map(|t| t.name().to_string()),
        };
        // 打印启动屏幕
        ironclaw::boot_screen::print_boot_screen(&boot_info);
    }

    // ── 运行代理 ────────────────────────────────────────────────────────

    let channels = Arc::new(channels);

    // 注册消息工具，用于向已连接的通道发送消息
    components
        .tools
        .register_message_tools(Arc::clone(&channels), components.extension_manager.clone())
        .await;

    // 将通道运行时接入扩展管理器，以便热激活 WASM 通道
    if let Some(ref ext_mgr) = components.extension_manager
        && let Some((rt, ps, router)) = wasm_channel_runtime_state.take()
    {
        let active_at_startup: std::collections::HashSet<String> =
            loaded_wasm_channel_names.iter().cloned().collect();
        ext_mgr.set_active_channels(loaded_wasm_channel_names).await;
        ext_mgr
            .set_channel_runtime(
                Arc::clone(&channels),
                rt,
                ps,
                router,
                config.channels.wasm_channel_owner_ids.clone(),
            )
            .await;
        tracing::debug!("通道运行时已接入扩展管理器，支持热激活");

        // 自动激活先前会话中活跃的 WASM 通道。
        // 中继通道稍后通过 restore_relay_channels() 单独处理。
        let persisted = ext_mgr.load_persisted_active_channels().await;
        for name in &persisted {
            if active_at_startup.contains(name) || ext_mgr.is_relay_channel(name).await {
                continue;
            }
            match ext_mgr.activate(name).await {
                Ok(result) => {
                    tracing::debug!(
                        channel = %name,
                        message = %result.message,
                        "自动激活持久化的 WASM 通道"
                    );
                }
                Err(e) => {
                    tracing::warn!(
                        channel = %name,
                        error = %e,
                        "自动激活持久化的 WASM 通道失败"
                    );
                }
            }
        }
    }

    // 确保中继通道管理器始终设置（即使没有 WASM 运行时），然后恢复任何持久化的中继通道。
    if let Some(ref ext_mgr) = components.extension_manager {
        ext_mgr
            .set_relay_channel_manager(Arc::clone(&channels))
            .await;
        ext_mgr.restore_relay_channels().await;
    }

    // 将 SSE 发送器接入扩展管理器，用于广播状态事件
    if let Some(ref ext_mgr) = components.extension_manager
        && let Some(ref sender) = sse_sender
    {
        ext_mgr.set_sse_sender(sender.clone()).await;
    }

    // 在代理启动前，为追踪记录拍摄内存快照
    if let Some(ref recorder) = components.recording_handle
        && let Some(ref ws) = components.workspace
    {
        recorder.snapshot_memory(ws).await;
    }

    let http_interceptor = components
        .recording_handle
        .as_ref()
        .map(|r| r.http_interceptor()); // HTTP 拦截器用于记录
    // 为 reaper 克隆 context_manager（稍后会被移入 Agent::new()）
    let reaper_context_manager = Arc::clone(&components.context_manager);

    // 捕获数据库引用供 SIGHUP 处理程序使用（仅 Unix）
    #[cfg(unix)]
    let sighup_settings_store: Option<Arc<dyn ironclaw::db::SettingsStore>> = components
        .db
        .as_ref()
        .map(|db| Arc::clone(db) as Arc<dyn ironclaw::db::SettingsStore>);

    // 构建代理依赖项
    let deps = AgentDeps {
        owner_id: config.owner_id.clone(),
        store: components.db,
        llm: components.llm,
        cheap_llm: components.cheap_llm,
        safety: components.safety,
        tools: components.tools,
        workspace: components.workspace,
        extension_manager: components.extension_manager,
        skill_registry: components.skill_registry,
        skill_catalog: components.skill_catalog,
        skills_config: config.skills.clone(),
        hooks: components.hooks,
        cost_guard: components.cost_guard,
        sse_tx: sse_sender,
        http_interceptor,
        transcription: config
            .transcription
            .create_provider()
            .map(|p| Arc::new(ironclaw::transcription::TranscriptionMiddleware::new(p))),
        document_extraction: Some(Arc::new(
            ironclaw::document_extraction::DocumentExtractionMiddleware::new(),
        )),
    };

    // 创建代理实例
    let mut agent = Agent::new(
        config.agent.clone(),
        deps,
        channels,
        Some(config.heartbeat.clone()),
        Some(config.hygiene.clone()),
        Some(config.routines.clone()),
        Some(components.context_manager),
        Some(session_manager),
    );

    // 现在代理（及其调度器）已存在，填充调度器槽位
    *scheduler_slot.write().await = Some(agent.scheduler());

    // 生成沙箱收割器，清理孤儿容器
    if let Some(ref jm) = container_job_manager {
        let reaper_jm = Arc::clone(jm);
        let reaper_config = ReaperConfig {
            scan_interval: Duration::from_secs(config.sandbox.reaper_interval_secs),
            orphan_threshold: Duration::from_secs(config.sandbox.orphan_threshold_secs),
            ..ReaperConfig::default()
        };
        let reaper_ctx = Arc::clone(&reaper_context_manager);
        tokio::spawn(async move {
            match SandboxReaper::new(reaper_jm, reaper_ctx, reaper_config).await {
                Ok(reaper) => reaper.run().await,
                Err(e) => tracing::error!("沙箱收割器初始化失败: {}", e),
            }
        });
    }

    // 将例程引擎槽交给代理，以便网关可以访问引擎
    agent.set_routine_engine_slot(shared_routine_engine_slot);

    // 准备 SIGHUP 处理程序，用于热重载 HTTP Webhook 配置
    // 创建广播通道，用于干净地关闭后台任务
    let (shutdown_tx, _) = tokio::sync::broadcast::channel::<()>(1);

    #[cfg(unix)]
    {
        // 收集所有支持密钥更新的通道
        let mut secret_updaters: Vec<Arc<dyn ChannelSecretUpdater>> = Vec::new();
        if let Some(ref state) = http_channel_state {
            secret_updaters.push(Arc::clone(state) as Arc<dyn ChannelSecretUpdater>);
        }

        let sighup_webhook_server = webhook_server.clone();
        let sighup_settings_store_clone = sighup_settings_store.clone();
        let sighup_secrets_store = components.secrets_store.clone();
        let sighup_owner_id = config.owner_id.clone();
        let mut shutdown_rx = shutdown_tx.subscribe();

        // 生成 SIGHUP 处理任务
        tokio::spawn(async move {
            use tokio::signal::unix::{SignalKind, signal};
            // 注册 SIGHUP 信号
            let mut sighup = match signal(SignalKind::hangup()) {
                Ok(s) => s,
                Err(e) => {
                    tracing::warn!("无法注册 SIGHUP 处理程序: {}", e);
                    return;
                }
            };

            loop {
                // 在收到关闭信号或 SIGHUP 时退出循环
                tokio::select! {
                    _ = shutdown_rx.recv() => {
                        tracing::debug!("SIGHUP 处理程序正在关闭");
                        break;
                    }
                    _ = sighup.recv() => {
                        // 收到 SIGHUP 信号
                    }
                }
                tracing::info!("收到 SIGHUP — 重新加载 HTTP webhook 配置");

                // 从数据库注入通道密钥到线程安全的覆盖层
                // （类似于为 LLM 提供者注入 LLM 密钥）
                if let Some(ref secrets_store) = sighup_secrets_store {
                    // 从加密存储中注入 HTTP webhook 密钥
                    if let Ok(webhook_secret) = secrets_store
                        .get_decrypted(&sighup_owner_id, "http_webhook_secret")
                        .await
                    {
                        // 线程安全：使用 INJECTED_VARS 互斥锁，而不是不安全的 std::env::set_var
                        // Config::from_env() 将通过 optional_env() 读取覆盖层
                        ironclaw::config::inject_single_var(
                            "HTTP_WEBHOOK_SECRET",
                            webhook_secret.expose(),
                        );
                        tracing::debug!("已从密钥存储中注入 HTTP_WEBHOOK_SECRET");
                    }
                }

                // 重新加载配置（现在密钥已注入环境）
                let new_config = match &sighup_settings_store_clone {
                    Some(store) => {
                        ironclaw::config::Config::from_db(store.as_ref(), &sighup_owner_id).await
                    }
                    None => ironclaw::config::Config::from_env().await,
                };

                let new_config = match new_config {
                    Ok(c) => c,
                    Err(e) => {
                        tracing::error!("SIGHUP 配置重新加载失败: {}", e);
                        continue;
                    }
                };

                let new_http = match new_config.channels.http {
                    Some(c) => c,
                    None => {
                        tracing::warn!("SIGHUP: HTTP 通道不再配置，跳过");
                        continue;
                    }
                };

                // 计算新的 socket 地址
                let new_addr: std::net::SocketAddr =
                    match format!("{}:{}", new_http.host, new_http.port).parse() {
                        Ok(a) => a,
                        Err(e) => {
                            tracing::error!("SIGHUP: 配置中的地址无效: {}", e);
                            continue;
                        }
                    };

                // 如果地址发生变化，重启监听器。
                // 两阶段方法：在锁外绑定新监听器，然后在锁内交换。
                let mut restart_failed = false;
                if let Some(ref ws_arc) = sighup_webhook_server {
                    let (old_addr, router) = {
                        let ws = ws_arc.lock().await;
                        (ws.current_addr(), ws.merged_router_clone())
                    }; // 锁在此释放

                    if old_addr != new_addr {
                        tracing::info!(
                            "SIGHUP: HTTP 地址从 {} 变为 {}，重启监听器",
                            old_addr,
                            new_addr
                        );

                        match router {
                            Some(app) => {
                                // 阶段1: 绑定新监听器（不持有锁）
                                match tokio::net::TcpListener::bind(new_addr).await {
                                    Ok(listener) => {
                                        // 阶段2: 在锁内交换状态（内部无 await）
                                        let (old_tx, old_handle) = {
                                            let mut ws = ws_arc.lock().await;
                                            ws.install_listener(new_addr, listener, app)
                                        }; // 锁在此释放

                                        // 阶段3: 在锁外关闭旧监听器
                                        if let Some(tx) = old_tx {
                                            let _ = tx.send(());
                                        }
                                        if let Some(handle) = old_handle {
                                            let _ = handle.await;
                                        }

                                        tracing::info!(
                                            "SIGHUP: webhook 服务器已在 {} 上重启",
                                            new_addr
                                        );
                                    }
                                    Err(e) => {
                                        tracing::error!(
                                            "SIGHUP: 绑定到 {} 失败: {}",
                                            new_addr,
                                            e
                                        );
                                        restart_failed = true;
                                    }
                                }
                            }
                            None => {
                                tracing::error!(
                                    "SIGHUP: 无法重启 — 服务器从未启动"
                                );
                                restart_failed = true;
                            }
                        }
                    } else {
                        tracing::debug!("SIGHUP: 地址未变化 ({})", old_addr);
                    }
                }

                // 如果重启成功或无需重启，更新所有已配置通道的密钥
                if !restart_failed {
                    use secrecy::{ExposeSecret, SecretString};
                    let new_secret = new_http
                        .webhook_secret
                        .as_ref()
                        .map(|s| SecretString::from(s.expose_secret().to_string()));

                    // 更新所有支持密钥交换的通道
                    for updater in &secret_updaters {
                        updater.update_secret(new_secret.clone()).await;
                    }
                }
            }
        });
    }

    // 运行代理主循环
    agent.run().await?;

    // ── 关闭 ────────────────────────────────────────────────────────────

    // 通知后台任务（SIGHUP 处理程序等）优雅关闭
    let _ = shutdown_tx.send(());

    // 关闭所有 stdio MCP 服务器子进程
    components.mcp_process_manager.shutdown_all().await;

    // 如果启用了 LLM 追踪记录，则刷新
    if let Some(ref recorder) = components.recording_handle
        && let Err(e) = recorder.flush().await
    {
        tracing::warn!("写入 LLM 追踪记录失败: {}", e);
    }

    // 关闭 Webhook 服务器
    if let Some(ref ws_arc) = webhook_server {
        let (shutdown_tx, handle) = {
            let mut ws = ws_arc.lock().await;
            ws.begin_shutdown()
        };
        if let Some(tx) = shutdown_tx {
            let _ = tx.send(());
        }
        if let Some(handle) = handle {
            let _ = handle.await;
        }
    }

    // 停止隧道
    if let Some(tunnel) = active_tunnel {
        tracing::debug!("正在停止 {} 隧道...", tunnel.name());
        if let Err(e) = tunnel.stop().await {
            tracing::warn!("停止隧道时出错: {}", e);
        }
    }

    tracing::debug!("代理关闭完成");

    Ok(())
}