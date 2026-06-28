import os
import logging
from pathlib import Path
from typing import Optional
from ironclaw_common.paths import ironclaw_base_dir
from dotenv import load_dotenv
import json

logger = logging.getLogger(__name__)


def ironclaw_env_path() -> Path:
    """
    IronClaw 专用 `.env` 文件的路径：`~/.ironclaw/.env`。
    """
    return ironclaw_base_dir() / ".env"


def load_ironclaw_env() -> None:
    """
    从 `~/.ironclaw/.env` 加载环境变量（除了标准的 `.env` 之外）。

    在 `dotenvy.dotenv()` **之后**调用此函数，以便标准的 `./.env`
    优先于 `~/.ironclaw/.env`。dotenvy 永远不会覆盖已存在的
    环境变量，因此有效的优先级为：

      explicit env vars > `./.env` > `~/.ironclaw/.env` > auto-detect

    如果 `~/.ironclaw/.env` 不存在但遗留的 `bootstrap.json` 存在，
    则从中提取 `DATABASE_URL` 并写入 `.env` 文件
    （从旧配置格式一次性升级）。

    加载 `.env` 文件后，自动检测 libsql 后端：如果
    `DATABASE_BACKEND` 仍未设置且 `~/.ironclaw/ironclaw.db` 存在，
    则默认为 `libsql`，以便云实例开箱即用，无需任何手动配置。
    """
    path = ironclaw_env_path()

    if not path.exists():
        # 一次性升级：从遗留的 bootstrap.json 中提取 DATABASE_URL
        migrate_bootstrap_json_to_env(path)

    if path.exists():
        load_dotenv(path)

    # 自动检测 libsql：如果在加载所有 env 文件后 DATABASE_BACKEND 仍未设置，
    # 并且本地 SQLite 数据库存在，则默认为 libsql。
    # 这避免了云实例上的先有鸡还是先有蛋的问题，即没有配置
    # DATABASE_URL 但 ironclaw.db 已经存在。
    if "DATABASE_BACKEND" not in os.environ:
        home = Path.home() if hasattr(Path, 'home') else Path(os.path.expanduser("~"))
        default_db = home / ".ironclaw" / "ironclaw.db"
        if default_db.exists():
            # 检查是否在异步上下文中运行
            try:
                import asyncio
                asyncio.get_running_loop()
                # 异步运行时处于活动状态；os.environ 的修改可能不安全。
                # 回退到线程安全的运行时覆盖层，以便始终设置该值。
                logger.warning(
                    "在活动的事件循环中调用 load_ironclaw_env；"
                    "对 DATABASE_BACKEND 使用运行时环境覆盖层"
                )
                set_runtime_env("DATABASE_BACKEND", "libsql")
            except RuntimeError:
                # 安全：没有活动的事件循环 = 没有其他协程 = 安全地设置环境变量。
                os.environ["DATABASE_BACKEND"] = "libsql"

def migrate_bootstrap_json_to_env(env_path: Path) -> None:
    """
    如果 `bootstrap.json` 存在，则从中提取 `database_url` 并写入 `.env`。
    """
    ironclaw_dir = env_path.parent if env_path.parent else Path(".")
    bootstrap_path = ironclaw_dir / "bootstrap.json"

    if not bootstrap_path.exists():
        return

    try:
        content = bootstrap_path.read_text()
    except Exception:
        return

    # 最小化解析：仅从 JSON 中获取 database_url
    try:
        parsed = json.loads(content)
    except Exception:
        return

    url = parsed.get("database_url")
    if url is not None and isinstance(url, str):
        # 确保父目录存在
        parent = env_path.parent
        if parent:
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning("警告：无法创建 %s: %s", parent, e)
                return

        try:
            env_path.write_text(f'DATABASE_URL="{url}"\n')
        except Exception as e:
            logger.warning("警告：无法将 bootstrap.json 迁移到 .env: %s", e)
            return

        _rename_to_migrated(bootstrap_path)
        logger.info("已将 DATABASE_URL 从 bootstrap.json 迁移到 %s", env_path)

# ── PID 锁 ──────────────────────────────────────────────────────────────

# 注意：Python 标准库没有提供等同于 `fs4::try_lock_exclusive()` 的跨平台文件锁。
# 此处使用 `fcntl.flock`（Unix/macOS）或 `msvcrt.locking`（Windows）提供等效功能。
# 如果平台不支持文件锁，将回退到仅检查 PID 文件的存在性（不具备原子性，存在 TOCTOU 竞态条件）。

def _try_lock_exclusive(file) -> bool:
    """
    尝试获取文件的排他锁，不阻塞。
    返回 True 表示获取成功，False 表示锁已被其他进程持有。
    """
    try:
        if os.name == 'nt':
            # Windows
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        else:
            # Unix / macOS
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
    except (IOError, OSError):
        return False


def pid_lock_path() -> Path:
    """PID 锁文件的路径：`~/.ironclaw/ironclaw.pid`。"""
    # 此处使用原代码中引用的 ironclaw_base_dir 函数，保持名称不变
    return ironclaw_base_dir().joinpath("ironclaw.pid")


class PidLockError(Exception):
    """PID 锁获取错误。"""
    pass


class AlreadyRunningError(PidLockError):
    """另一个 IronClaw 实例已在运行。"""

    def __init__(self, pid: int):
        self.pid = pid
        super().__init__(f"另一个 IronClaw 实例已在运行 (PID {pid})")


class PidLock:
    """
    基于 PID 的锁，防止多个 IronClaw 实例同时运行。

    使用文件排他锁进行原子锁定（无 TOCTOU 竞态条件），
    然后将当前 PID 写入锁定的文件以供诊断。
    操作系统级别的锁在此结构体的生命周期内持有，
    并在回收时自动释放（同时清理 PID 文件）。
    """

    def __init__(self, path: Path, file):
        """
        初始化 PidLock。

        参数:
            path: 锁文件的路径
            file: 保持打开以维护操作系统级别排他锁的文件对象
        """
        self.path = path
        self._file = file

    @classmethod
    def acquire(cls) -> "PidLock":
        """
        尝试获取 PID 锁。

        使用排他文件锁，以便两个并发进程不能同时获取锁——无 TOCTOU 竞态条件。
        如果锁文件存在但持有进程已不存在（过期），操作系统会自动回收该锁。
        """
        return cls._acquire_at(pid_lock_path())

    @classmethod
    def _acquire_at(cls, path: Path) -> "PidLock":
        """在特定路径获取锁（用于测试）。"""
        # 确保父目录存在
        path.parent.mkdir(parents=True, exist_ok=True)

        # 打开（或创建）锁文件
        file = open(path, 'a+')  # 'a+' 读写/追加，若不存在则创建，不截断

        # 尝试非阻塞排他锁——如果另一个进程持有该锁，
        # 此操作将立即失败，而不是阻塞。
        if not _try_lock_exclusive(file):
            # 锁被另一个进程持有——读取其 PID 用于错误消息
            try:
                pid_str = path.read_text().strip()
                pid = int(pid_str)
            except (ValueError, FileNotFoundError):
                pid = 0
            file.close()
            raise AlreadyRunningError(pid)

        # 我们持有排他锁——写入我们的 PID
        file.seek(0)
        file.truncate()
        file.write(str(os.getpid()))
        file.flush()

        return cls(path, file)

    def __del__(self):
        """回收时删除 PID 文件；操作系统级别的锁在 _file 关闭时释放。"""
        try:
            path = getattr(self, 'path', None)
            file = getattr(self, '_file', None)
            if path is not None and path.exists():
                try:
                    path.unlink()
                except OSError:
                    logger.debug("无法删除 PID 锁文件: %s", path, exc_info=True)
            if file is not None and not file.closed:
                file.close()
        except Exception:
            # 在 __del__ 中静默处理所有异常
            pass
