import os
from pathlib import Path
from typing import Optional

# 缓存 base_dir，确保只计算一次（对应 Rust 中的 LazyLock）
_base_dir_cache: Optional[Path] = None


def ironclaw_base_dir() -> Path:
    """
    获取 IronClaw 基础目录。

    可通过 `IRONCLAW_BASE_DIR` 环境变量覆盖。默认为 `~/.ironclaw`（如果无法确定主目录，则回退到 `./.ironclaw`）。

    线程安全：该值仅计算一次并缓存。
    """
    global _base_dir_cache
    if _base_dir_cache is None:
        _base_dir_cache = _compute_base_dir()
    return _base_dir_cache


def _compute_base_dir() -> Path:
    """计算基础目录的实际路径。"""
    # 优先使用环境变量
    env_dir = os.environ.get('IRONCLAW_BASE_DIR')
    if env_dir:
        return Path(env_dir)

    # 尝试使用用户主目录
    home = Path.home()
    if home is not None and home.exists():
        return home / '.ironclaw'

    # 回退到当前目录下的 .ironclaw
    return Path('./.ironclaw')