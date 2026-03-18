from ironclaw.agent import Agent, AgentDeps
from ironclaw.app import AppBuilder, AppBuilderFlags
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

# 配置日志
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
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
    else:
        pass

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
    components = await AppBuilder(config, flags, Path(toml_path) if toml_path else None,
                            session,
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
