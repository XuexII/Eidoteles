import asyncio
import argparse
from enum import Enum


class Command(str, Enum):
    # 运行智能体（未提供子命令时的默认操作）Example: ironclaw run
    RUN = "run"
    # 交互式设置向导
    # 引导完成初始配置。
    # 示例：
    # ironclaw onboard --skip-auth # 跳过认证步骤
    # ironclaw onboard --channels-only # 重新配置频道
    # ironclaw onboard --provider-only # 更改大语言模型提供商和模型
    ONBOARD = "onboard"






class Cli:
    """命令行参数解析类（模拟 Rust 中的 Cli）"""

    @staticmethod
    def parse() -> argparse.Namespace:
        """
        解析命令行参数。
        对应 Rust 中的 Cli::parse()，返回一个包含参数的对象。
        """
        parser = argparse.ArgumentParser(prog="ironclaw", description="示例程序")
        parser.add_argument('-v', '--version', action='version', version='v0.0.1')
        # 添加子命令，对应 Cli 结构体中的子命令字段（例如 `run`, `config` 等）
        subparsers = parser.add_subparsers(title="子命令", dest="command", help="可用的子命令")

        return parser.parse_args()