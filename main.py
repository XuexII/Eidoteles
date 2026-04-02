from agent import Agent, AgentDeps
from app import AppBuilder, AppBuilderFlags
from ironclaw.channels import ChannelManager, GatewayChannel, HttpChannel, ReplChannel, SignalChannel, WebhookServer, \
    WebhookServerConfig, ChannelSecretUpdater
from ironclaw.channels.wasm import WasmChannelRouter, WasmChannelRuntime
from ironclaw.channels.web.log_layer import LogBroadcaster
import ironclaw.channels.web.log_layer as log_layer

from ironclaw.cli import Cli, Command, run_mcp_command, run_pairing_command, run_service_command, run_status_command, \
    run_tool_command, config

from ironclaw.config import Config
from ironclaw.hooks import bootstrap_hooks
from ironclaw.llm import create_session_manager
from ironclaw.orchestrator import ReaperConfig, SandboxReaper
from ironclaw.pairing::PairingStore
from ironclaw.tracing_fmt import init_cli_tracing, init_worker_tracing
from ironclaw.webhooks import self, ToolWebhookState
from ironclaw.setup import SetupConfig, SetupWizard, check_onboard_needed

import asyncio
from ironclaw import bootstrap
import logging
import sys
import argparse
from pathlib import Path
from transcription import TranscriptionMiddleware
from datetime import timedelta

# 配置日志
logging.basicConfig(level=logger.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ironclaw")


def main():
    """同步入口点：加载.env文件，然后启动异步主函数"""
    # 加载.env文件（与Rust的dotenvy对应）
    from dotenv import load_dotenv
    load_dotenv()

    # 加载IronClaw特定环境变量（假设bootstrap模块有该函数）
    bootstrap.load_ironclaw_env()

    # 使用asyncio运行异步主函数
    asyncio.run(async_main())


async def async_main():
    cli = Cli.parse()
    parser = argparse.ArgumentParser(description="IronClaw Agent")
    args = parser.parse_args()

    command = args.command

    if command == "tool":
        init_cli_tracing()
        return await run_tool_command(cli)
    elif command == "config":
        init_cli_tracing()
        return await run_config_command(cli)
    elif command == "registry":
        init_cli_tracing()
        return await run_registry_command(cli)
    elif command == "channels":
        init_cli_tracing()
        return await run_channels_command(cli)
    elif command == "routines":
        init_cli_tracing()
        return await run_routines_cli(cli)
    elif command == "mcp":
        init_cli_tracing()
        return await run_mcp_command(cli)
    elif command == "memory":
        init_cli_tracing()
        return await run_memory_command(cli)
    elif command == "pairing":
        init_cli_tracing()
        return await run_pairing_command(cli)
    elif command == "service":
        init_cli_tracing()
        return await run_service_command(cli)
    elif command == "skills":
        init_cli_tracing()
        return await run_skills_command(cli)
    elif command == "logs":
        init_cli_tracing()
        return await run_logs_command(cli)
    elif command == "doctor":
        init_cli_tracing()
        return await run_doctor_command(cli)
    elif command == "status":
        init_cli_tracing()
        return await run_status_command(cli)
    elif command == "completion":
        init_cli_tracing()
        return await completion.run()
    elif command == "import":
        init_cli_tracing()
        return await run_import_command(cli)
    elif command == "worker":
        init_worker_tracing()
        return await run_worker(cli)
    elif command == "claude_bridge":
        init_worker_tracing()
        run_claude_bridge()
    elif command == "web":
        init_cli_tracing()

    # 继续执行

    # ---- PID锁（防止多实例） ----
    try:
        pid_lock = bootstrap.PidLock.acquire()
    except bootstrap.PidLockError.AlreadyRunning as e:
        logger.error(f"另一个IronClaw实例已在运行 (PID {e.pid})。如果错误，请删除PID文件: {bootstrap.pid_lock_path()}")
        sys.exit(1)
    except Exception as e:
        logger.warning(f"无法获取PID锁: {e}，继续运行但无保护。")
        pid_lock = None

    # ---- 首次运行检测 ----
    if args.no_onboard:
        reason = check_onboard_needed()
        if reason:
            print(f"需要配置向导: {reason}\n")
            wizard = SetupWizard.try_with_config_and_toml(SetupConfig(quick=True), args.config)
            await wizard.run()

    # ---- 加载配置 ----
    toml_path = cli.config.as_deref()
    try:
        config = await Config.rom_env_with_toml(toml_path)
    except error.ConfigError.MissingRequired as e:
        logger.error(f"配置错误: 缺少必需的设置 '{e.key}'。{e.hint} 请运行 'ironclaw onboard' 或设置环境变量。")
        sys.exit(1)
    except Exception as e:
        logger.exception("配置加载失败")
        sys.exit(1)

    # ---- 初始化会话管理器 ----
    session = await create_session_manager(config.llm.session)

    # ---- 创建日志广播器 ----
    log_broadcaster = LogBroadcaster()
    # ---- 初始化追踪（日志） ----
    log_level_handle = log_layer.init_tracing(log_broadcaster)

    logger.debug("正在启动 IronClaw...")
    logger.debug(f"已加载代理配置: {config.agent.name}")
    logger.debug(f"LLM后端: {config.llm.backend}")

    # ---- 构建核心组件 ----
    flags = AppBuilderFlags(no_db=args.no_db)
    toml_path = Path(toml_path) if toml_path else None
    components = await AppBuilder(config, flags, toml_path, session,
                                  log_broadcaster).build_all()

    config = components.config

    # ---- 隧道设置 ----
    config, active_tunnel = await ironclaw.tunnel.start_managed_tunnel(config)

    # ---- 编排器（容器作业管理器） ----
    orch = ironclaw.orchestrator.setup_orchestrator(
        config, components.llm, components.db.as_ref(),
        components.secrets_store.as_ref()
    )

    container_job_manager = orch.container_job_manager
    job_event_tx = orch.job_event_tx
    prompt_queue = orch.prompt_queue
    docker_status = orch.docker_status

    # ---- 通道设置 ----
    channels_mgr = ChannelManager()
    channel_names = []
    loaded_wasm_channel_names = []
    wasm_channel_runtime_state = None  # 用于存储WASM运行时相关组件

    # 创建REPL通道
    if args.message:
        repl_channel = ReplChannel.with_message_for_user(config.owner_id, args.message)
    elif config.channels.cli.enabled:
        repl_channel = ReplChannel.with_user_id(config.owner_id)
        repl_channel.suppress_banner()
    else:
        repl_channel = None

    if repl_channel:
        await channels_mgr.add(repl_channel)
        if args.message:
            logger.debug("单条消息模式")
        else:
            channel_names.append("repl")
            logger.debug("REPL模式启用")

    # 构建Agent依赖
    transcription = None
    if provider := config.transcription.create_provider():
        transcription = TranscriptionMiddleware(provider)

    deps = AgentDeps(
        owner_id=config.owner_id,
        store=components.db,
        llm=components.llm,
        cheap_llm=components.cheap_llm,
        safety=components.safety,
        tools=components.tools,
        workspace=components.workspace,
        extension_manager=components.extension_manager,
        skill_registry=components.skill_registry,
        skill_catalog=components.skill_catalog,
        skills_config=config.skills.clone(),
        hooks=components.hooks,
        cost_guard=components.cost_guard,
        sse_tx=sse_sender,
        http_interceptor=http_interceptor,
        transcription=transcription
    )

    # 创建代理实例
    agent = Agent(
        config = config.agent,
        deps = deps,
        channels=channels,
        heartbeat_config=config.heartbeat,
        hygiene_config=config.hygiene,
        routine_config=config.routines,
        context_manager=components.context_manager,
        session_manager=session_manager
    )

    # 现在代理（及其调度器）已存在，填充调度器槽位
    # 异步获取写锁，并将 agent.scheduler() 的结果包装为 Some 后赋值给锁保护的值
    async with scheduler_slot.write_lock() as guard:
        guard.value = agent.scheduler()

    # 生成沙盒回收器以清理孤儿容器
    if container_job_manager:
        reaper_config = ReaperConfig(
            scan_interval=timedelta(seconds=config.sandbox.reaper_interval_secs),
            orphan_threshold=timedelta(seconds=config.sandbox.orphan_threshold_secs)
        )
        # TODO 创建异步任务

    # 将例程引擎槽交给代理，以便网关可以访问引擎
    agent.set_routine_engine_slot(shared_routine_engine_slot)

    # 准备 SIGHUP 处理程序，用于热重载 HTTP Webhook 配置
    # 创建广播通道，用于干净地关闭后台任务
    shutdown_tx = None

    # 仅unix系统执行代码


    # 运行代理主循环
    await agent.run()

    # ── 关闭 ──
    # 通知后台任务（SIGHUP 处理程序等）优雅关闭
    _ = shutdown_tx.send(())

    # 关闭所有 stdio MCP 服务器子进程
    await components.mcp_process_manager.shutdown_all()
    # 如果启用了 LLM 追踪记录，则刷新
    if recorder := components.recording_handle:
        try:
            await recorder.flush()
        except Exception as e:
            # 对应 Rust 中的 tracing::warn!
            logger.warning(f"写入 LLM 追踪记录失败: {e}")

    # 关闭 Webhook 服务器
    if webhook_server:
        pass

    # 停止隧道
    if active_tunnel:
        logger.debug(f"正在停止 {active_tunnel.name} 隧道...")
        try:
            await active_tunnel.stop()
        except Exception as e:
            logger.warning(f"停止隧道时出错: {e}")

    logger.debug("代理关闭完成")