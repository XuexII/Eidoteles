"""
截断终端写入器的日志模块。
LLM 提供商的追踪事件可能向 stderr 输出超过 10KB 的 JSON 体。
我们在写入器级别处理截断：fmt 层使用 TruncatingStderr 在刷新前截断每个事件，
而 web 网关 WebLogLayer 仍然看到完整的未截断内容。

本模块提供两个简单的初始化函数（对应 Rust 中的 init_cli_tracing 和 init_worker_tracing），
用于快速配置命令行工具的日志。
"""

import logging
import os
import sys
from typing import Optional


def init_cli_tracing():
    """
    初始化 CLI 命令的简单日志配置（级别为 WARNING，不使用复杂层）。
    对应 Rust 中的 init_cli_tracing。

    从环境变量 `LOG_LEVEL` 读取日志级别，默认为 WARNING。
    输出到 stderr，格式为 "LEVEL: message"。
    """
    # 读取环境变量中的日志级别，默认为 WARNING
    log_level_str = os.environ.get("LOG_LEVEL", "WARNING").upper()
    log_level = getattr(logging, log_level_str, logging.WARNING)

    # 配置根日志记录器
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True  # 覆盖任何已有配置
    )


def init_worker_tracing():
    """
    初始化 worker/bridge 进程的日志配置。
    对应 Rust 中的 init_worker_tracing。

    默认行为：
        - 根记录器级别为 WARNING（其他模块仅输出警告及以上）
        - `ironclaw` 命名空间的记录器级别为 INFO（可从环境变量 `IRONCLAW_LOG_LEVEL` 覆盖）

    输出到 stderr，格式为 "LEVEL: message"。
    """
    # 根记录器默认 WARNING
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.WARNING)

    # ironclaw 命名空间的记录器默认 INFO，可被环境变量覆盖
    ironclaw_log_level_str = os.environ.get("IRONCLAW_LOG_LEVEL", "INFO").upper()
    ironclaw_log_level = getattr(logging, ironclaw_log_level_str, logging.INFO)

    ironclaw_logger = logging.getLogger("ironclaw")
    ironclaw_logger.setLevel(ironclaw_log_level)

    # 如果尚未添加任何处理器，添加一个 stderr 处理器
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter("%(levelname)s: %(message)s")
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

    # 注意：由于日志记录器层次结构，ironclaw 记录器的日志也会传播到根处理器，
    # 并且级别由 ironclaw 记录器控制（根级别 WARNING 不影响已设置更高级别的子记录器）。
    # 这模拟了 Rust 中 EnvFilter 针对不同目标设置不同级别的行为。