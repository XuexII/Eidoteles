# 待处理门控存储——原子性、经频道验证、持久化。
#
# 使用单个 [`Mutex`]（而非 `RwLock`），因为每次有意义的读取之后都会伴随写入操作。
# 这样设计可以消除检查时间与使用时间之间的竞态条件。

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from engine import ThreadId
from gate.pending import PendingGate, PendingGateKey, PendingGateView

logger = logging.getLogger(__name__)

# ── 可信通道 ────────────────────────────────────────

# 可以解析任何源通道创建的门控的通道。
# Web 网关是可信的，因为它在服务器端认证用户。
TRUSTED_GATE_CHANNELS = ["web", "gateway"]

# 系统保留的通道名称。WASM 扩展无法注册这些名称，防止冒充攻击。
RESERVED_CHANNEL_NAMES = [
    "web",
    "gateway",
    "telegram",
    "signal",
    "slack",
    "discord",
    "repl",
    "cli",
    "http",
    "__bootstrap__",
]


# ── 错误类型 ──────────────────────────────────────────────

class GateStoreError(Exception):
    """待处理门控存储的错误。"""
    pass


class GateStoreNotFoundError(GateStoreError):
    """此线程没有待处理门控。"""

    def __init__(self):
        super().__init__(f"此线程没有待处理的gate")


class GateStoreRequestIdMismatchError(GateStoreError):
    """请求 ID 不匹配（过时的批准）。"""

    def __init__(self):
        super().__init__(f"请求 ID 不匹配（过期的批准）")


class GateStoreChannelMismatchError(GateStoreError):
    """通道无法解析来自其他通道的门控。"""

    def __init__(self, expected: str, actual: str):
        super().__init__(f"通道 '{actual}' 无法解析来自通道 '{expected}' 的gate")


class GateStoreExpiredError(GateStoreError):
    """待处理门控已过期。"""

    def __init__(self):
        super().__init__(f"待处理gate已过期。")


class GateStoreAlreadyExistsError(GateStoreError):
    """此线程已有待处理门控。"""

    def __init__(self):
        super().__init__(f"此线程已存在待处理的gate")


class GateStoreUnauthorizedError(GateStoreError):
    """
    门控匹配提供的请求 ID，但属于不同的用户。
    与 `NotFound` 不同，以便调用者（HTTP 处理程序）可以返回 403
    而不泄露门控是否存在。
    """

    def __init__(self):
        super().__init__(f"无权解析此gate")


class GateStorePersistenceError(GateStoreError):
    """持久化错误。"""

    def __init__(self, reason: str):
        super().__init__(f"持久化错误: {reason}")


# ── 持久化 trait ───────────────────────────────────────

class GatePersistence(ABC):
    """跨重启持久化待处理门控的后端。"""

    @abstractmethod
    async def save(self, gate: PendingGate) -> None:
        raise NotImplementedError

    @abstractmethod
    async def remove(self, key: PendingGateKey) -> None:
        raise NotImplementedError

    @abstractmethod
    async def load_all(self) -> List[PendingGate]:
        raise NotImplementedError


# ── 存储 ───────────────────────────────────────────────────
@dataclass
class StoreInner:
    by_key: Dict[PendingGateKey, PendingGate] = field(default_factory=dict)
    by_request_id: Dict[str, PendingGateKey] = field(default_factory=dict)


class PendingGateStore:
    """
    线程安全的待处理执行门控存储。

    所有修改都在单个锁下进行，以防止 TOCTOU 竞态条件。
    `take_verified` 方法是检索待处理门控以进行解析的**唯一**方式——
    它在移除门控之前原子性地验证请求 ID、通道授权和过期时间。
    """

    def __init__(self, persistence: Optional[GatePersistence] = None):
        self.inner = StoreInner()
        self.persistence = persistence
        self._lock = asyncio.Lock()

    @classmethod
    def in_memory(cls) -> "PendingGateStore":
        """创建不带持久化的存储（仅内存）。"""
        return cls(persistence=None)

    async def insert(self, gate: PendingGate) -> None:
        """插入待处理门控。如果 (user, thread) 已存在则失败。"""
        key = gate.key
        async with self._lock:
            if key in self.inner.by_key:
                raise GateStoreAlreadyExistsError()
            self.inner.by_request_id[gate.request_id] = key
            self.inner.by_key[key] = gate

        # 锁释放后持久化（异步 I/O 在锁外）
        if self.persistence is not None:
            await self.persistence.save(gate)

    async def take_verified(
            self,
            key: PendingGateKey,
            request_id: str,
            responding_channel: str,
    ) -> PendingGate:
        """
        在验证所有不变量后原子性地取出待处理门控。

        这是在单个锁获取下检索门控以进行解析的**唯一**方式：
        1. 检查 (user_id, thread_id) 的门控是否存在
        2. 验证 `request_id` 匹配（防止过时的批准）
        3. 验证通道授权
        4. 检查过期时间
        5. 从两个索引中移除
        """
        async with self._lock:
            gate = self.inner.by_key.get(key)
            if gate is None:
                raise GateStoreNotFoundError()

            # 验证请求 ID
            if gate.request_id != request_id:
                raise GateStoreRequestIdMismatchError()

            # 验证通道授权
            channel_authorized = (
                    gate.source_channel == responding_channel
                    or responding_channel in TRUSTED_GATE_CHANNELS
            )
            if not channel_authorized:
                raise GateStoreChannelMismatchError(
                    expected=gate.source_channel,
                    actual=responding_channel,
                )

            # 检查过期
            if gate.is_expired:
                # 持有锁时清理过期门控
                removed = self.inner.by_key.pop(key, None)
                if removed is not None:
                    self.inner.by_request_id.pop(removed.request_id, None)
                raise GateStoreExpiredError()

            # 原子性地移除——无 TOCTOU 间隙
            gate = self.inner.by_key.pop(key)
            self.inner.by_request_id.pop(gate.request_id, None)

        # 锁释放后持久化移除
        if self.persistence is not None:
            try:
                await self.persistence.remove(key)
            except Exception as e:
                logger.debug(
                    "门控持久化移除失败（门控已从内存中取出）: %s", e
                )

        return gate

    async def peek(self, key: PendingGateKey) -> Optional[PendingGateView]:
        """
        只读查看待处理门控（用于历史/重连响应）。

        不删除门控。如果门控不存在或已过期，返回 `None`。
        """
        async with self._lock:
            gate = self.inner.by_key.get(key)
            if gate is None or gate.is_expired:
                return None
            return PendingGateView.from_gate(gate)

    async def peek_by_request_id(
            self,
            request_id: str,
            expected_user_id: str,
    ) -> Optional[PendingGateView]:
        """
        按 `request_id` 只读查看待处理门控，范围限定为请求用户。
        如果没有门控匹配、门控属于其他用户或已过期，返回 `None`。

        由前台取消路径使用，以在客户端在解析负载中省略 `thread_id` 时
        恢复拥有线程——没有这个，前台内联等待门控将被搁浅
        （门控标记为已取消，暂停的 VM 永远不会展开）。参见 PR #3366 审查。
        """
        async with self._lock:
            key = self.inner.by_request_id.get(request_id)
            if key is None:
                return None
            if key.user_id != expected_user_id:
                return None
            gate = self.inner.by_key.get(key)
            if gate is None or gate.is_expired:
                return None
            return PendingGateView.from_gate(gate)

    async def list_for_user(self, user_id: str) -> List[PendingGate]:
        """列出用户所有未过期的门控。"""
        async with self._lock:
            return [
                gate
                for gate in self.inner.by_key.values()
                if gate.user_id == user_id and not gate.is_expired
            ]

    async def take_verified_by_request_id(
            self,
            request_id: str,
            expected_user_id: str,
            responding_channel: str,
    ) -> PendingGate:
        """
        按 `request_id` 原子性地取出待处理门控，在单个锁下验证
        用户所有权、通道授权和过期时间。

        镜像 [`take_verified`]，但首先从线 `request_id` 解析复合键。
        由 HTTP 表面（内联等待快速路径）使用，其中调用者只有通道可见的
        线程标识符——对于 Web 网关，该标识符在门控上记录为
        `scope_thread_id`，而不是内部引擎 `ThreadId`。
        按 `request_id`（系统范围内唯一）查找避免了线 vs. 引擎标识符混淆，
        否则会完全错过门控。

        返回：
        - `Ok(gate)` 成功——门控从两个索引中移除。
        - `Err(NotFound)` 没有门控匹配 `request_id`
          （已解析、从未存在或重启后无法恢复）。
        - `Err(Unauthorized)` 门控存在但 `expected_user_id` 不拥有它。
          这与 `NotFound` 有意区分，以便调用者可以显示 403
          而不跨租户泄露门控存在性。
        - `Err(ChannelMismatch | Expired)` ——与 [`take_verified`] 相同的语义。
        """
        async with self._lock:
            key = self.inner.by_request_id.get(request_id)
            if key is None:
                raise GateStoreNotFoundError()

            if key.user_id != expected_user_id:
                raise GateStoreUnauthorizedError()

            gate = self.inner.by_key.get(key)
            if gate is None:
                raise GateStoreNotFoundError()

            # 验证通道授权
            channel_authorized = (
                    gate.source_channel == responding_channel
                    or responding_channel in TRUSTED_GATE_CHANNELS
            )
            if not channel_authorized:
                raise GateStoreChannelMismatchError(
                    expected=gate.source_channel,
                    actual=responding_channel,
                )

            # 检查过期——持有锁时清理过期门控
            if gate.is_expired:
                removed = self.inner.by_key.pop(key, None)
                if removed is not None:
                    self.inner.by_request_id.pop(removed.request_id, None)
                raise GateStoreExpiredError()

            # 原子性地移除——无 TOCTOU 间隙
            gate = self.inner.by_key.pop(key)
            self.inner.by_request_id.pop(gate.request_id, None)

        # 锁释放后持久化移除
        if self.persistence is not None:
            try:
                await self.persistence.remove(key)
            except Exception as e:
                logger.debug(
                    "门控持久化移除失败（门控已从内存中取出）: %s", e
                )

        return gate

    async def list_all(self) -> List[PendingGate]:
        """列出所有未过期的门控。"""
        async with self._lock:
            return [
                gate
                for gate in self.inner.by_key.values()
                if not gate.is_expired
            ]

    async def discard_for_thread(self, thread_id: ThreadId) -> List[PendingGate]:
        """
        删除给定线程的所有门控，无论用户是谁。

        返回被删除的门控。用于线程被删除或变得不可达而门控仍待处理时——
        防止产生永远无法解析的孤立门控。
        """
        keys_to_remove: List[PendingGateKey] = []
        removed_gates: List[PendingGate] = []

        async with self._lock:
            for key, gate in list(self.inner.by_key.items()):
                if key.thread_id == thread_id:
                    keys_to_remove.append(key)
                    self.inner.by_request_id.pop(gate.request_id, None)
                    removed_gates.append(gate)

            for key in keys_to_remove:
                self.inner.by_key.pop(key, None)

        if self.persistence is not None:
            for key in keys_to_remove:
                try:
                    await self.persistence.remove(key)
                except Exception as e:
                    logger.debug("从持久化中移除孤立门控失败: %s", e)

        return removed_gates

    async def discard(self, key: PendingGateKey) -> None:
        """
        在无验证的情况下按键移除门控。

        用于清理路径，如对话清除或显式取消流。
        """
        async with self._lock:
            gate = self.inner.by_key.pop(key, None)
            if gate is None:
                raise GateStoreNotFoundError()
            self.inner.by_request_id.pop(gate.request_id, None)

        if self.persistence is not None:
            await self.persistence.remove(key)

    async def restore_from_persistence(self) -> int:
        """
        在启动时从持久存储恢复待处理门控。
        返回恢复的未过期门控数量。
        """
        if self.persistence is None:
            return 0

        gates = await self.persistence.load_all()
        count = 0
        async with self._lock:
            for gate in gates:
                if gate.is_expired:
                    continue
                # `approval_already_granted` 是一个内存提示，表示 Approval 门控
                # 在同一路由周期中较早被满足，并且在链接到后续门控时不应重新提示
                # （例如 Approval -> Authentication）。它不能在进程重启后存活——
                # 重新水合后，用户必须重新批准，即使他们在崩溃前已授予批准。
                # 在此处清除标志，以便持久化的门控始终从干净状态开始。
                gate.approval_already_granted = False
                key = gate.key
                self.inner.by_request_id[gate.request_id] = key
                self.inner.by_key[key] = gate
                count += 1
        return count

    async def expire_stale(self) -> int:
        """移除所有过期门控。返回移除的数量。"""
        expired_keys: List[PendingGateKey] = []

        async with self._lock:
            for key, gate in list(self.inner.by_key.items()):
                if gate.is_expired:
                    expired_keys.append(key)
                    self.inner.by_key.pop(key, None)
                    self.inner.by_request_id.pop(gate.request_id, None)

        # 锁外持久化移除
        count = len(expired_keys)
        if self.persistence is not None:
            for key in expired_keys:
                try:
                    await self.persistence.remove(key)
                except Exception as e:
                    logger.debug("从持久化中移除过期门控失败: %s", e)

        return count

    @staticmethod
    def is_channel_reserved(name: str) -> bool:
        """检查通道名称是否被保留（WASM 无法注册）。"""
        return name in RESERVED_CHANNEL_NAMES
