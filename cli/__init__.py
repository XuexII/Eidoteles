"""
该模块将原 Rust 的 clap 命令行定义转换为 Python 的 argparse 实现。
使用 @dataclass 存储参数，通过 ArgumentParser 解析命令行。
"""
from __future__ import annotations
import argparse
import os
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Union

# ============================================================================
# 子命令可能用到的占位类型（原 Rust 代码引用了但未给出定义，保留名称）
# ============================================================================

@dataclass
class ConfigCommand:
    """管理配置设置（子命令集，此处仅保留名称占位）"""
    pass

@dataclass
class ToolCommand:
    """管理 WASM 工具（子命令集占位）"""
    pass

@dataclass
class RegistryCommand:
    """浏览和安装扩展（子命令集占位）"""
    pass

@dataclass
class ChannelsCommand:
    """管理消息通道（子命令集占位）"""
    pass

@dataclass
class RoutinesCommand:
    """管理例行任务（子命令集占位）"""
    pass

@dataclass
class McpCommand:
    """管理 MCP 服务器（子命令集占位）"""
    pass

@dataclass
class MemoryCommand:
    """管理工作区内存（子命令集占位）"""
    pass

@dataclass
class PairingCommand:
    """管理 DM 配对（子命令集占位）"""
    pass

@dataclass
class ProfileCommand:
    """管理部署配置文件（子命令集占位）"""
    pass

@dataclass
class ServiceCommand:
    """管理操作系统服务（子命令集占位）"""
    pass

@dataclass
class SkillsCommand:
    """管理基于 SKILL.md 的技能（子命令集占位）"""
    pass

@dataclass
class HooksCommand:
    """管理生命周期钩子（子命令集占位）"""
    pass

@dataclass
class ModelsCommand:
    """管理 LLM 提供者和模型（子命令集占位）"""
    pass

@dataclass
class LogsCommand:
    """查看和管理网关日志（子命令集占位）"""
    pass

@dataclass
class Completion:
    """生成 Shell 补全脚本（参数暂略）"""
    pass

@dataclass
class ImportCommand:
    """从其他 AI 系统导入数据（子命令集占位）"""
    pass

@dataclass
class AcpCommand:
    """管理 ACP 代理（子命令集占位）"""
    pass


# ============================================================================
# 具体子命令参数的数据类
# ============================================================================

@dataclass
class OnboardArgs:
    """交互式引导向导的参数"""
    # 跳过认证（使用已有会话）
    skip_auth: bool = False
    # 已弃用：仅重新配置通道 (使用 --step channels)
    channels_only: bool = False
    # 已弃用：仅重新配置 LLM 提供者和模型 (使用 --step provider)
    provider_only: bool = False
    # 快速设置：除了 LLM 提供者和模型外，全部使用自动默认值
    quick: bool = False
    # 仅运行特定的设置步骤（逗号分隔：provider, channels, model, database, security）
    step: List[str] = field(default_factory=list)

@dataclass
class LoginArgs:
    """身份验证参数"""
    # 使用 OpenAI Codex 进行身份验证（ChatGPT 订阅）
    openai_codex: bool = False

@dataclass
class WorkerArgs:
    """作为沙箱工作器在 Docker 容器内运行（内部使用）"""
    # 要执行的作业 ID
    job_id: uuid.UUID = field(default_factory=uuid.uuid4)
    # 编排器内部 API 的 URL
    orchestrator_url: str = "http://host.docker.internal:50051"
    # 停止前的最大迭代次数
    max_iterations: int = 50

@dataclass
class ClaudeBridgeArgs:
    """作为 Claude Code 桥接在 Docker 容器内运行（内部使用）"""
    # 要执行的作业 ID
    job_id: uuid.UUID = field(default_factory=uuid.uuid4)
    # 编排器内部 API 的 URL
    orchestrator_url: str = "http://host.docker.internal:50051"
    # Claude Code 的最大代理轮次
    max_turns: int = 50
    # 要使用的 Claude 模型（例如 "sonnet", "opus"）
    model: str = "sonnet"

@dataclass
class AcpBridgeArgs:
    """作为 ACP 桥接在 Docker 容器内运行（内部使用）"""
    # 要执行的作业 ID
    job_id: uuid.UUID = field(default_factory=uuid.uuid4)
    # 编排器内部 API 的 URL
    orchestrator_url: str = "http://host.docker.internal:50051"


Command = None
# ============================================================================
# 顶层命令行参数数据类
# ============================================================================

@dataclass
class Cli:
    """
    IronClaw 的命令行接口，对应原 Rust 中的 Cli 结构体。
    包含全局标志和子命令。
    """
    command: Optional[Union[
        OnboardArgs,
        LoginArgs,
        WorkerArgs,
        ClaudeBridgeArgs,
        AcpBridgeArgs,
        ConfigCommand,
        ToolCommand,
        RegistryCommand,
        ChannelsCommand,
        RoutinesCommand,
        McpCommand,
        MemoryCommand,
        PairingCommand,
        ProfileCommand,
        ServiceCommand,
        SkillsCommand,
        HooksCommand,
        ModelsCommand,
        LogsCommand,
        Completion,
        ImportCommand,
        AcpCommand,
        str  # 用于 Run, Doctor, Status 等无参数子命令
    ]] = None

    # 仅以交互式 CLI 模式运行（禁用其他通道）
    cli_only: bool = False
    # 跳过数据库连接（用于测试）
    no_db: bool = False
    # 单消息模式 - 发送一条消息后退出
    message: Optional[str] = None
    # 配置文件路径（可选，默认使用环境变量）
    config: Optional[str] = None
    # 跳过首次运行引导检查
    no_onboard: bool = False
    # 自动批准工具执行（shell、文件写入、HTTP 等）
    # 跳过标准工具的交互式批准提示。破坏性操作仍需显式批准。
    # 其他安全措施仍然有效：速率限制、钩子、身份验证门控。
    auto_approve: bool = False
    # 部署模式：IronClaw 的运行位置以及机器边界的拥有者
    # 线名称：`local_single_user`、`hosted_multi_tenant`、`enterprise_dedicated`
    # 回退顺序：环境变量 `IRONCLAW_DEPLOYMENT_MODE`，然后 `local_single_user`
    deployment_mode: Optional[str] = None
    # 请求的运行时配置文件 (#3045)
    # 线名称：`secure_default`, `local_safe`, `local_dev`, `local_yolo`,
    # `hosted_safe`, `hosted_dev`, `hosted_yolo_tenant_scoped`,
    # `enterprise_safe`, `enterprise_dev`, `enterprise_yolo_dedicated`,
    # `sandboxed`, `experiment`
    # 回退顺序：环境变量 `IRONCLAW_RUNTIME_PROFILE`，然后 `secure_default`
    runtime_profile: Optional[str] = None
    # 确认 `*_yolo*` 配置文件所要求的披露声明
    # Yolo 配置文件在其权限边界内有意减少批准。
    # CLI 必须捕获明确的运营商确认 — 没有此标志（或 `IRONCLAW_YOLO_DISCLOSURE=true`），
    # 任何 yolo 配置文件选择都将安全关闭。
    yolo_disclosure: bool = False

    def should_run_agent(self) -> bool:
        """检查是否应运行代理（默认行为或显式的 `run` 命令）"""
        return self.command is None or self.command == "run"

    @classmethod
    def parse(cls) -> Cli:
        """
        构建 ArgumentParser 并解析命令行参数，返回 Cli 实例。
        """

        # 顶层解析器
        parser = argparse.ArgumentParser(
            prog="ironclaw",
            description="安全的个人 AI 助手，保护您的数据并扩展其功能",
            epilog=(
                "IronClaw 是一个安全的 AI 助手。\n\n"
                "入门：\n"
                "  ironclaw onboard               # 交互式设置向导（推荐首次运行）\n"
                "  ironclaw onboard --quick       # 快速设置：仅选择提供者和模型\n"
                "  ironclaw models set-provider openai  # 切换到特定提供者\n"
                "  ironclaw doctor                # 检查您的配置\n\n"
                "常用命令：\n"
                "  ironclaw run                   # 启动代理\n"
                "  ironclaw config list           # 查看所有设置\n"
                "  ironclaw models status         # 显示当前提供者和模型\n"
                "  ironclaw models list           # 列出可用的提供者\n\n"
                "使用 'ironclaw <subcommand> --help' 查看任何命令的详细信息。"
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument('--version', action='version', version='%(prog)s 1.0')

        # 创建全局参数父解析器，以便子命令也能识别（模拟 clap 的 global = true）
        global_parent = argparse.ArgumentParser(add_help=False)
        global_parent.add_argument(
            '--cli-only', action='store_true', default=False,
            help='仅以交互式 CLI 模式运行（禁用其他通道）'
        )
        global_parent.add_argument(
            '--no-db', action='store_true', default=False,
            help='跳过数据库连接（用于测试）'
        )
        global_parent.add_argument(
            '-m', '--message', type=str, default=None,
            help='单消息模式 - 发送一条消息后退出'
        )
        global_parent.add_argument(
            '-c', '--config', type=str, default=None,
            help='配置文件路径（可选，默认使用环境变量）'
        )
        global_parent.add_argument(
            '--no-onboard', action='store_true', default=False,
            help='跳过首次运行引导检查'
        )
        global_parent.add_argument(
            '--auto-approve', action='store_true', default=False,
            help='自动批准工具执行（shell、文件写入、HTTP 等）'
        )
        global_parent.add_argument(
            '--deployment-mode', type=str, default=None, metavar='MODE',
            help='部署模式：IronClaw 的运行位置以及机器边界的拥有者'
        )
        global_parent.add_argument(
            '--runtime-profile', type=str, default=None, metavar='PROFILE',
            help='请求的运行时配置文件'
        )
        global_parent.add_argument(
            '--yolo-disclosure', action='store_true', default=False,
            help='确认 `*_yolo*` 配置文件所要求的披露声明'
        )

        # 子命令解析器
        subparsers = parser.add_subparsers(dest='command_name', title='命令')

        # ------------------------------------------------------------------------
        # Run 子命令
        run_parser = subparsers.add_parser(
            'run', parents=[global_parent],
            help='运行 AI 代理',
            description='启动 IronClaw 代理（默认模式）。\n示例：ironclaw run'
        )
        run_parser.set_defaults(command_obj='run')

        # ------------------------------------------------------------------------
        # Onboard 子命令
        onboard_parser = subparsers.add_parser(
            'onboard', parents=[global_parent],
            help='运行交互式设置向导（如果您是 IronClaw 新手，请从这里开始）',
            description=(
                '逐步指导您配置 IronClaw。\n\n'
                '这是设置 LLM 提供者、API 密钥、数据库和通道的推荐方式。'
                '随时可以再次运行以更改设置。\n\n'
                '示例：\n'
                '  ironclaw onboard                    # 完整设置向导\n'
                '  ironclaw onboard --quick            # 快速：仅提供者和模型\n'
                '  ironclaw onboard --step provider    # 仅更改 LLM 提供者\n'
                '  ironclaw onboard --step channels    # 重新配置消息通道\n'
                '  ironclaw onboard --step provider,model  # 更改提供者和模型'
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        onboard_parser.add_argument('--skip-auth', action='store_true', default=False,
                                    help='跳过认证（使用已有会话）')
        # 已弃用的选项
        onboard_parser.add_argument('--channels-only', action='store_true', default=False,
                                    help='已弃用：仅重新配置通道 (使用 --step channels)')
        onboard_parser.add_argument('--provider-only', action='store_true', default=False,
                                    help='已弃用：仅重新配置 LLM 提供者和模型 (使用 --step provider)')
        onboard_parser.add_argument('--quick', action='store_true', default=False,
                                    help='快速设置：除了 LLM 提供者和模型外，全部使用自动默认值')
        onboard_parser.add_argument('--step', action='append', default=[],
                                    help='仅运行特定的设置步骤（逗号分隔：provider, channels, model, database, security）')
        onboard_parser.set_defaults(command_obj='onboard')

        # ------------------------------------------------------------------------
        # Config 子命令（内部子命令由 ConfigCommand 承载，这里仅占位）
        config_parser = subparsers.add_parser(
            'config', parents=[global_parent],
            help='管理应用配置设置',
            description=(
                '查看和修改 IronClaw 设置（存储在数据库和 config.toml 中）。\n\n'
                '要更改 LLM 提供者/模型，请使用 `ironclaw models`。\n\n'
                '示例：\n'
                '  ironclaw config list              # 列出所有设置\n'
                '  ironclaw config list -f agent     # 按前缀筛选\n'
                '  ironclaw config get agent.name    # 获取特定值\n'
                '  ironclaw config set agent.name my-bot  # 更改值\n'
                '  ironclaw config init              # 生成 config.toml\n'
                '  ironclaw config path              # 显示设置存储位置'
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        config_parser.set_defaults(command_obj=ConfigCommand())

        # ------------------------------------------------------------------------
        # Tool 子命令
        tool_parser = subparsers.add_parser(
            'tool', parents=[global_parent],
            help='管理 WASM 工具',
            description='安装、列出或删除基于 WASM 的工具。\n示例：ironclaw tool install mytool.wasm'
        )
        tool_parser.set_defaults(command_obj=ToolCommand())

        # ------------------------------------------------------------------------
        # Registry 子命令
        registry_parser = subparsers.add_parser(
            'registry', parents=[global_parent],
            help='浏览/安装扩展',
            description='与扩展注册表交互。\n示例：ironclaw registry list'
        )
        registry_parser.set_defaults(command_obj=RegistryCommand())

        # ------------------------------------------------------------------------
        # Channels 子命令
        channels_parser = subparsers.add_parser(
            'channels', parents=[global_parent],
            help='管理通道',
            description=(
                '列出已配置的消息通道。\n'
                '示例：\n  ironclaw channels list\n'
                '  ironclaw channels list --verbose\n'
                '  ironclaw channels list --json'
            )
        )
        channels_parser.set_defaults(command_obj=ChannelsCommand())

        # ------------------------------------------------------------------------
        # Routines 子命令（带别名 "cron"）
        routines_parser = subparsers.add_parser(
            'routines', aliases=['cron'], parents=[global_parent],
            help='管理例行任务（计划任务、事件驱动、webhook、手动）',
            description=(
                '列出、创建、编辑、启用/禁用、删除例行任务，并查看历史记录。\n'
                '示例：\n  ironclaw routines list\n'
                "  ironclaw routines create --name daily-digest --schedule '0 0 9 * * *' --prompt '总结今天'"
            )
        )
        routines_parser.set_defaults(command_obj=RoutinesCommand())

        # ------------------------------------------------------------------------
        # Mcp 子命令
        mcp_parser = subparsers.add_parser(
            'mcp', parents=[global_parent],
            help='管理 MCP 服务器（托管工具提供者）',
            description='添加、认证、列出或测试 MCP 服务器。\n示例：ironclaw mcp add notion https://mcp.notion.com'
        )
        mcp_parser.set_defaults(command_obj=McpCommand())

        # ------------------------------------------------------------------------
        # Memory 子命令
        memory_parser = subparsers.add_parser(
            'memory', parents=[global_parent],
            help='管理工作区内存',
            description="搜索、读取或写入内存。\n示例：ironclaw memory search '查询'"
        )
        memory_parser.set_defaults(command_obj=MemoryCommand())

        # ------------------------------------------------------------------------
        # Pairing 子命令
        pairing_parser = subparsers.add_parser(
            'pairing', parents=[global_parent],
            help='管理 DM 配对',
            description=(
                '批准或管理配对请求。\n'
                '示例：\n  ironclaw pairing list telegram\n'
                '  ironclaw pairing approve telegram ABC12345'
            )
        )
        pairing_parser.set_defaults(command_obj=PairingCommand())

        # ------------------------------------------------------------------------
        # Profile 子命令
        profile_parser = subparsers.add_parser(
            'profile', parents=[global_parent],
            help='管理部署配置文件',
            description=(
                '列出可用的部署配置文件并查看当前激活的配置文件。\n'
                '示例：\n  ironclaw profile list\n  ironclaw profile list --json'
            )
        )
        profile_parser.set_defaults(command_obj=ProfileCommand())

        # ------------------------------------------------------------------------
        # Service 子命令
        service_parser = subparsers.add_parser(
            'service', parents=[global_parent],
            help='管理操作系统服务',
            description='安装、启动或停止服务。\n示例：ironclaw service install'
        )
        service_parser.set_defaults(command_obj=ServiceCommand())

        # ------------------------------------------------------------------------
        # Skills 子命令
        skills_parser = subparsers.add_parser(
            'skills', parents=[global_parent],
            help='管理技能',
            description=(
                '列出、搜索和检查基于 SKILL.md 的技能。\n'
                "示例：\n  ironclaw skills list\n"
                "  ironclaw skills search '写作'\n"
                '  ironclaw skills info my-skill'
            )
        )
        skills_parser.set_defaults(command_obj=SkillsCommand())

        # ------------------------------------------------------------------------
        # Hooks 子命令
        hooks_parser = subparsers.add_parser(
            'hooks', parents=[global_parent],
            help='管理生命周期钩子',
            description=(
                '列出和检查生命周期钩子（捆绑、插件、工作区）。\n'
                '示例：\n  ironclaw hooks list\n'
                '  ironclaw hooks list --verbose\n'
                '  ironclaw hooks list --json'
            )
        )
        hooks_parser.set_defaults(command_obj=HooksCommand())

        # ------------------------------------------------------------------------
        # Models 子命令
        models_parser = subparsers.add_parser(
            'models', parents=[global_parent],
            help='管理 LLM 提供者和模型',
            description=(
                '列出提供者、查看当前配置，并设置活动的提供者/模型。\n\n'
                '使用此命令在 AI 提供者之间切换，而无需重新运行完整的设置向导。\n\n'
                '示例：\n'
                '  ironclaw models list                          # 列出所有提供者\n'
                '  ironclaw models list openai --verbose         # 显示 OpenAI 的详细信息\n'
                '  ironclaw models status                        # 显示当前提供者/模型\n'
                '  ironclaw models set gpt-4o                    # 更改模型\n'
                '  ironclaw models set-provider anthropic        # 切换到 Anthropic\n'
                '  ironclaw models set-provider ollama --model llama3  # 切换到本地 Ollama'
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        models_parser.set_defaults(command_obj=ModelsCommand())

        # ------------------------------------------------------------------------
        # Doctor 子命令
        doctor_parser = subparsers.add_parser(
            'doctor', parents=[global_parent],
            help='运行诊断（检查是否所有配置正确）',
            description=(
                '探测 LLM 提供者、数据库、通道和外部依赖项。\n'
                '在运行时出现问题之前暴露配置错误。\n\n'
                '如果有东西不工作，请运行此命令——它会告诉您需要修复什么。\n\n'
                '示例：\n  ironclaw doctor'
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        doctor_parser.set_defaults(command_obj='doctor')

        # ------------------------------------------------------------------------
        # Logs 子命令
        logs_parser = subparsers.add_parser(
            'logs', parents=[global_parent],
            help='查看和管理网关日志',
            description=(
                '查看网关日志、流式实时输出或调整日志级别。\n'
                '示例：\n  ironclaw logs                 # 显示 gateway.log 的最后 200 行\n'
                '  ironclaw logs --follow        # 通过 SSE 流式实时日志\n'
                '  ironclaw logs --level         # 显示当前日志级别\n'
                '  ironclaw logs --level debug   # 将日志级别设置为 debug'
            )
        )
        logs_parser.set_defaults(command_obj=LogsCommand())

        # ------------------------------------------------------------------------
        # Status 子命令
        status_parser = subparsers.add_parser(
            'status', parents=[global_parent],
            help='显示系统状态',
            description='显示健康和诊断信息。\n示例：ironclaw status'
        )
        status_parser.set_defaults(command_obj='status')

        # ------------------------------------------------------------------------
        # Completion 子命令
        completion_parser = subparsers.add_parser(
            'completion', parents=[global_parent],
            help='生成补全脚本',
            description='生成 Shell 补全脚本。\n示例：ironclaw completion --shell bash > ironclaw.bash'
        )
        completion_parser.set_defaults(command_obj=Completion())

        # ------------------------------------------------------------------------
        # Import 子命令 (条件编译忽略，总是包含)
        import_parser = subparsers.add_parser(
            'import', parents=[global_parent],
            help='从其他 AI 系统导入数据',
            description='从其他 AI 助手（如 OpenClaw）迁移数据。\n示例：ironclaw import openclaw'
        )
        import_parser.set_defaults(command_obj=ImportCommand())

        # ------------------------------------------------------------------------
        # Login 子命令
        login_parser = subparsers.add_parser(
            'login', parents=[global_parent],
            help='与提供者进行身份验证（重新登录）',
            description=(
                '重新验证 LLM 提供者。\n\n'
                '对于大多数提供者，应设置 API 密钥环境变量。\n'
                '交互式设置：`ironclaw onboard --step provider`\n\n'
                '示例：\n  ironclaw login --openai-codex'
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        login_parser.add_argument('--openai-codex', action='store_true', default=False,
                                  help='使用 OpenAI Codex 进行身份验证（ChatGPT 订阅）')
        login_parser.set_defaults(command_obj='login')

        # ------------------------------------------------------------------------
        # Worker 子命令（隐藏命令）
        worker_parser = subparsers.add_parser(
            'worker', parents=[global_parent],
            help=argparse.SUPPRESS,
            description='作为 Docker 容器内的沙箱工作器运行（内部使用）。由编排器自动调用，用户不应直接使用。'
        )
        worker_parser.add_argument('--job-id', type=uuid.UUID, required=True,
                                   help='要执行的作业 ID')
        worker_parser.add_argument('--orchestrator-url', type=str,
                                   default='http://host.docker.internal:50051',
                                   help='编排器内部 API 的 URL')
        worker_parser.add_argument('--max-iterations', type=int,
                                   default=int(os.environ.get('IRONCLAW_MAX_ITERATIONS', '50')),
                                   help='停止前的最大迭代次数')
        worker_parser.set_defaults(command_obj='worker')

        # ------------------------------------------------------------------------
        # Acp 子命令
        acp_parser = subparsers.add_parser(
            'acp', parents=[global_parent],
            help='管理 ACP 代理',
            description='添加、列出、删除或测试符合 ACP 的编码代理。\n示例：ironclaw acp add goose --command goose --arg "--stdio"'
        )
        acp_parser.set_defaults(command_obj=AcpCommand())

        # ------------------------------------------------------------------------
        # ClaudeBridge 子命令（隐藏）
        claude_bridge_parser = subparsers.add_parser(
            'claude-bridge', parents=[global_parent],
            help=argparse.SUPPRESS,
            description='作为 Claude Code 桥接在 Docker 容器内运行（内部使用）。生成 `claude` CLI 并将输出流式传回编排器。'
        )
        claude_bridge_parser.add_argument('--job-id', type=uuid.UUID, required=True,
                                          help='要执行的作业 ID')
        claude_bridge_parser.add_argument('--orchestrator-url', type=str,
                                          default='http://host.docker.internal:50051',
                                          help='编排器内部 API 的 URL')
        claude_bridge_parser.add_argument('--max-turns', type=int, default=50,
                                          help='Claude Code 的最大代理轮次')
        claude_bridge_parser.add_argument('--model', type=str, default='sonnet',
                                          help='要使用的 Claude 模型（例如 "sonnet", "opus"）')
        claude_bridge_parser.set_defaults(command_obj='claude_bridge')

        # ------------------------------------------------------------------------
        # AcpBridge 子命令（隐藏）
        acp_bridge_parser = subparsers.add_parser(
            'acp-bridge', parents=[global_parent],
            help=argparse.SUPPRESS,
            description='作为 ACP 桥接在 Docker 容器内运行（内部使用）。生成符合 ACP 的代理并将输出流式传回编排器。'
        )
        acp_bridge_parser.add_argument('--job-id', type=uuid.UUID, required=True,
                                       help='要执行的作业 ID')
        acp_bridge_parser.add_argument('--orchestrator-url', type=str,
                                       default='http://host.docker.internal:50051',
                                       help='编排器内部 API 的 URL')
        acp_bridge_parser.set_defaults(command_obj='acp_bridge')

        # 解析参数
        args = parser.parse_args()

        # 构建子命令对象（如果提供了子命令）
        command: Optional[object] = None
        if hasattr(args, 'command_obj'):
            raw = args.command_obj
            if raw == 'run':
                command = 'run'
            elif raw == 'onboard':
                # 处理 --step，将逗号分隔的列表展平
                steps = []
                for s in args.step:
                    steps.extend(s.split(','))
                command = OnboardArgs(
                    skip_auth=args.skip_auth,
                    channels_only=args.channels_only,
                    provider_only=args.provider_only,
                    quick=args.quick,
                    step=steps
                )
            elif raw == 'doctor':
                command = 'doctor'
            elif raw == 'status':
                command = 'status'
            elif raw == 'login':
                command = LoginArgs(openai_codex=args.openai_codex)
            elif raw == 'worker':
                command = WorkerArgs(
                    job_id=args.job_id,
                    orchestrator_url=args.orchestrator_url,
                    max_iterations=args.max_iterations
                )
            elif raw == 'claude_bridge':
                command = ClaudeBridgeArgs(
                    job_id=args.job_id,
                    orchestrator_url=args.orchestrator_url,
                    max_turns=args.max_turns,
                    model=args.model
                )
            elif raw == 'acp_bridge':
                command = AcpBridgeArgs(
                    job_id=args.job_id,
                    orchestrator_url=args.orchestrator_url
                )
            else:
                # 对于其他子命令，command_obj 已经是占位对象实例
                command = raw

        # 构建 Cli 实例
        cli = Cli(
            command=command,
            cli_only=args.cli_only,
            no_db=args.no_db,
            message=args.message,
            config=args.config,
            no_onboard=args.no_onboard,
            auto_approve=args.auto_approve,
            deployment_mode=args.deployment_mode,
            runtime_profile=args.runtime_profile,
            yolo_disclosure=args.yolo_disclosure,
        )
        return cli


# ============================================================================
# 如果作为脚本运行，进行简单测试
# ============================================================================
if __name__ == '__main__':
    cli = Cli.parse()
    print(cli)
    print("是否应运行代理？", cli.should_run_agent())