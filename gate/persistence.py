# 用于待处理门控的基于文件的持久化存储。
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List

from bootstrap import ironclaw_base_dir
from gate.pending import PendingGate, PendingGateKey
from gate.store import (
    GatePersistence,
    GateStoreError,
    GateStorePersistenceError
)


@dataclass
class PendingGateFile:
    version: int
    gates: List[PendingGate]

    def dict(self):
        return asdict(self)


class FileGatePersistence(GatePersistence):
    """
    `~/.ironclaw/` 下待处理门控的 JSON 文件持久化。
    """

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def default_path() -> Path:
        """默认持久化文件路径。"""
        return ironclaw_base_dir() / "pending-gates.json"

    @classmethod
    def with_default_path(cls) -> "FileGatePersistence":
        """使用默认路径创建持久化实例。"""
        return cls(cls.default_path())

    def open_locked_file(self):
        """打开并排他锁定持久化文件。"""
        # 确保父目录存在
        parent = self.path.parent
        if parent:
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                raise GateStorePersistenceError(
                    reason=f"创建父目录 '{parent}': {e}"
                )

        # 打开文件（读写，不存在则创建，不截断）
        try:
            file = open(self.path, 'a+')
        except Exception as e:
            raise GateStorePersistenceError(
                reason=f"打开 '{self.path}': {e}"
            )

        # 在 Unix/macOS 上使用 fcntl.flock，在 Windows 上使用 msvcrt.locking
        # msvcrt：Windows 专属，提供对微软 C 运行时库的访问，常用于控制台输入输出、文件锁定、获取终端尺寸等操作。
        # fcntl：Unix/Linux/macOS 专属，对文件描述符进行控制操作，最经典的用途是文件锁（排他锁/共享锁），以及设置非阻塞 I/O、管理文件描述符标志等
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        except Exception as e:
            file.close()
            raise GateStorePersistenceError(
                reason=f"锁定 '{self.path}': {e}"
            )

        return file

    def read_state(self, file) -> PendingGateFile:
        """从文件中读取状态。"""
        file.seek(0)
        content = file.read()

        if not content.strip():
            return PendingGateFile(version=1, gates=[])

        try:
            return PendingGateFile(**json.loads(content))
        except Exception as e:
            raise GateStorePersistenceError(
                reason=f"解析 '{self.path}': {e}"
            )

    def write_state(self, file, state: PendingGateFile) -> None:
        """将状态写入文件。"""
        try:
            json_bytes = json.dumps(state.dict(), indent=2, ensure_ascii=False).encode('utf-8')
        except Exception as e:
            raise GateStorePersistenceError(
                reason=f"序列化 '{self.path}': {e}"
            )

        # 清空文件
        try:
            file.seek(0)
            file.truncate(0)
        except OSError as e:
            raise GateStoreError(
                f"truncate '{self.path}': {e}"
            ) from e

        # 移动到文件开头（truncate 后已经在开头，但显式写出来保持对称）
        try:
            file.seek(0)
        except OSError as e:
            raise GateStoreError(
                f"seek '{self.path}': {e}"
            )

        # 写入、刷新、同步到磁盘
        try:
            file.write(json_bytes)
            file.flush()
            os.fsync(file.fileno())
        except OSError as e:
            raise GateStoreError(
                f"write '{self.path}': {e}"
            ) from e

    async def save(self, gate: PendingGate) -> None:
        """保存待处理门控。"""
        file = self.open_locked_file()
        try:
            state = self.read_state(file)
            # 保留不匹配的现有门控，替换匹配的
            state.gates = [
                g for g in state.gates
                if g.key != gate.key
            ]
            state.gates.append(gate)
            self.write_state(file, state)
        finally:
            file.close()

    async def remove(self, key: PendingGateKey) -> None:
        """移除待处理门控。"""
        file = self.open_locked_file()
        try:
            state = self.read_state(file)
            state.gates = [
                g for g in state.gates
                if g.key != key
            ]
            self.write_state(file, state)
        finally:
            file.close()

    async def load_all(self) -> List[PendingGate]:
        """加载所有待处理门控。"""
        file = self.open_locked_file()
        try:
            state = self.read_state(file)
            return state.gates
        finally:
            file.close()
