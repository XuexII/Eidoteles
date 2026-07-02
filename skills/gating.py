# 技能依赖准入校验模块
#
# 在加载技能前，校验该技能声明的全部依赖项（可执行程序、环境变量、配置文件）是否均已满足。


import asyncio
import logging
import os
import platform
import subprocess
from dataclasses import dataclass, field
from typing import List

from .types import GatingRequirements

logger = logging.getLogger(__name__)


# ── 门控结果 ─────────────────────────────────────────────────

@dataclass
class GatingResult:
    """门控检查的结果"""
    # 是否所有要求都通过了
    passed: bool = True
    # 失败要求的描述
    failures: List[str] = field(default_factory=list)


# ── 异步门控检查 ─────────────────────────────────────────────

async def check_requirements(requirements: GatingRequirements) -> GatingResult:
    """[`check_requirements_sync`] 的异步包装器，将阻塞的子进程调用
    （`which`/`where`）通过 `asyncio.to_thread` 卸载到线程池

    快速路径：如果要求不包含 bins、环境变量或配置路径需要检查，
    立即返回 `passed: true` 而不创建线程任务。伴随 `skills` 条目
    仅是咨询性的，不影响门控，因此它们不会阻塞快速路径。
    这避免了对于没有子进程可检查要求的技能（常见情况）
    每次加载技能时都进行 `which` 子进程调用
    """
    bins = getattr(requirements, 'bins', [])
    env = getattr(requirements, 'env', [])
    config = getattr(requirements, 'config', [])

    if not bins and not env and not config:
        return GatingResult(passed=True, failures=[])

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, check_requirements_sync, requirements)
    except Exception as e:
        message = f"门控检查失败: {e}"
        logger.error(message)
        return GatingResult(passed=False, failures=[message])


# ── 同步门控检查 ─────────────────────────────────────────────

def check_requirements_sync(requirements: GatingRequirements) -> GatingResult:
    """检查门控要求是否满足（同步）

    - `bins`: 检查每个二进制文件是否可通过 `which`（PATH 查找）找到
    - `env`: 检查每个环境变量是否已设置
    - `config`: 检查每个配置文件路径是否存在

    门控失败的技能应被记录并跳过，不加载

    这是同步实现；在异步上下文中调用时优先使用异步 [`check_requirements`] 包装器，
    以避免阻塞 asyncio 事件循环
    """
    failures = []

    bins = getattr(requirements, 'bins', [])
    for bin_name in bins:
        if not binary_exists(bin_name):
            failures.append(f"未找到所需二进制文件: {bin_name}")

    env_vars = getattr(requirements, 'env', [])
    for var in env_vars:
        if var not in os.environ:
            failures.append(f"未设置所需环境变量: {var}")

    config_paths = getattr(requirements, 'config', [])
    for path in config_paths:
        if not os.path.exists(path):
            failures.append(f"未找到所需配置文件: {path}")

    # 伴随技能依赖（`requirements.skills`）有意不在此处检查 —
    # 门控模块无权访问技能注册表。它们仅是咨询性元数据

    return GatingResult(
        passed=len(failures) == 0,
        failures=failures,
    )


# ── 二进制文件存在性检查 ─────────────────────────────────────

def binary_exists(name: str) -> bool:
    """使用系统命令检查 PATH 上是否存在二进制文件

    在 Unix 上使用 `which`，在 Windows 上使用 `where`
    """
    if platform.system() == "Windows":
        # Windows 使用 `where` 命令
        try:
            result = subprocess.run(
                ["where", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
    else:
        # Unix 使用 `which` 命令
        try:
            result = subprocess.run(
                ["which", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
