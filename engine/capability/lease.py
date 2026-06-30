# 租约管理器——授予、验证和过期能力租约。

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, List, Set

from ..types.capability import CapabilityLease, GrantedActions, LeaseId
from ..types.thread import ThreadId

logger = logging.getLogger(__name__)


# ── 租约管理器 ───────────────────────────────────────────────

@dataclass
class LeaseManager:
    """管理能力租约的生命周期

    租约是线程获得能力访问权限的机制。
    它们是作用域受限的（时间限制、使用次数限制、动作限制），
    以限制任何单个线程的影响范围
    """
    active: Dict[LeaseId, CapabilityLease] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def grant(
            self,
            thread_id: ThreadId,
            capability_name: str,
            granted_actions: GrantedActions,
            duration: Optional[timedelta] = None,
            max_uses: Optional[int] = None,
    ) -> CapabilityLease:
        """向线程授予新租约

        如果 `duration` 为非正值或 `max_uses` 为零，返回 `RuntimeError::Effect` —
        这些会创建立即过期或无法使用的租约
        """
        if duration is not None and duration <= timedelta(0):
            raise RuntimeError(f"Effect: 租约持续时间必须为正数，实际为 {duration.total_seconds()}s")
        if max_uses is not None and max_uses == 0:
            raise RuntimeError("Effect: 租约 max_uses 必须 > 0")

        now = datetime.now(timezone.utc)
        lease = CapabilityLease(
            id=LeaseId(),
            thread_id=thread_id,
            capability_name=capability_name,
            granted_actions=granted_actions,
            granted_at=now,
            expires_at=now + duration if duration else None,
            max_uses=max_uses,
            uses_remaining=max_uses,
            revoked=False,
            revoked_reason=None,
        )

        async with self._lock:
            self.active[lease.id] = lease

        return lease

    async def check(self, lease_id: LeaseId) -> CapabilityLease:
        """检查租约是否仍然有效。如果有效则返回租约"""
        async with self._lock:
            lease = self.active.get(lease_id)
            if lease is None:
                raise RuntimeError(f"LeaseNotFound: lease_id={lease_id}")
            if not lease.is_valid():
                raise RuntimeError(f"LeaseExpired: capability_name={lease.capability_name}")
            return lease

    async def consume_use(self, lease_id: LeaseId) -> None:
        """消耗租约的一次使用次数。如果租约无效或已耗尽则返回错误"""
        async with self._lock:
            lease = self.active.get(lease_id)
            if lease is None:
                raise RuntimeError(f"LeaseExpired: 未找到租约 {lease_id}")
            if not lease.is_valid():
                raise RuntimeError(f"LeaseExpired: capability_name={lease.capability_name}")
            if not lease.consume_use():
                raise RuntimeError(f"LeaseExpired: capability_name={lease.capability_name}")

    async def refund_use(self, lease_id: LeaseId) -> None:
        """在动作完成前执行被中断后退还一次租约使用次数"""
        async with self._lock:
            lease = self.active.get(lease_id)
            if lease is None:
                raise RuntimeError(f"LeaseExpired: 未找到租约 {lease_id}")
            lease.refund_use()

    async def update_granted_actions(
            self,
            lease_id: LeaseId,
            granted_actions: GrantedActions,
    ) -> CapabilityLease:
        """就地更新现有租约的已授予动作"""
        async with self._lock:
            lease = self.active.get(lease_id)
            if lease is None:
                raise RuntimeError(f"LeaseNotFound: lease_id={lease_id}")
            lease.granted_actions = granted_actions
            return lease

    async def revoke(self, lease_id: LeaseId, reason: str) -> None:
        """按 ID 撤销租约，并附上审计追踪的原因"""
        async with self._lock:
            lease = self.active.get(lease_id)
            if lease is not None:
                lease.revoked = True
                lease.revoked_reason = reason
                logger.debug(f"租约已撤销: lease_id={lease_id}, capability={lease.capability_name}, reason={reason}")

    async def expire_stale(self) -> int:
        """从活跃集合中移除所有过期或已撤销的租约"""
        async with self._lock:
            before = len(self.active)
            self.active = {
                k: v for k, v in self.active.items() if v.is_valid()
            }
            return before - len(self.active)

    async def active_for_thread(self, thread_id: ThreadId) -> List[CapabilityLease]:
        """获取线程的所有活跃（有效）租约"""
        async with self._lock:
            return [
                lease for lease in self.active.values()
                if lease.thread_id == thread_id and lease.is_valid()
            ]

    async def find_lease_for_action(
            self,
            thread_id: ThreadId,
            action_name: str,
    ) -> Optional[CapabilityLease]:
        """查找向线程授予特定动作的租约"""
        hyphenated = action_name.replace('_', '-')
        underscored = action_name.replace('-', '_')

        async with self._lock:
            for lease in self.active.values():
                if (lease.thread_id == thread_id
                        and lease.is_valid()
                        and (lease.covers_action(action_name)
                             or lease.covers_action(hyphenated)
                             or lease.covers_action(underscored))):
                    return lease
            return None

    async def derive_child_leases(
            self,
            parent_thread_id: ThreadId,
            child_thread_id: ThreadId,
            requested_actions: Optional[Set[str]] = None,
    ) -> List[CapabilityLease]:
        """从父线程的活跃租约派生子租约

        实现交集语义：子线程仅获得既在父线程活跃集合中
        又在 `requested_actions` 集合中的动作的租约。
        如果 `requested_actions` 为 `None`，子线程继承父线程的所有有效租约

        不变量：
        - 子线程永远不会拥有比父线程更多的权限
        - 子租约继承父租约的过期时间（永远不会比父租约存活更久）
        - 子租约继承父租约的剩余预算
        - 过期的父租约不会产生子租约

        **预算说明：** `max_uses` 是 `parent.uses_remaining` 的时间点快照。
        派生后父租约和子租约的预算是独立的 — 父线程可以继续消耗自己的租约。
        这意味着合并使用量（父 + 子）可能超过父租约原始的 `max_uses`。
        这是有意为之：子线程获得的是生成时的预算而非共享的原子计数器，
        这避免了跨线程争用。需要严格全局预算的调用者应在线程级别
        使用基于时间的过期或基于成本的限制
        """
        parent_leases = await self.active_for_thread(parent_thread_id)
        child_leases = []

        for parent in parent_leases:
            if not parent.is_valid():
                continue

            if requested_actions is not None:
                if isinstance(parent.granted_actions, All):
                    # 父租约是通配符。子租约只获得请求的子集，而非通配符
                    child_grants = Specific(list(requested_actions))
                elif isinstance(parent.granted_actions, Specific):
                    # 交集：仅在父租约和请求中同时存在的动作
                    intersection = [
                        a for a in parent.granted_actions.actions
                        if a in requested_actions
                    ]
                    child_grants = Specific(intersection)
            else:
                child_grants = parent.granted_actions

            # 如果交集为空则跳过（无匹配动作）
            if isinstance(child_grants, Specific) and not child_grants.actions and requested_actions is not None:
                continue

            child_leases.append(CapabilityLease(
                id=LeaseId(),
                thread_id=child_thread_id,
                capability_name=parent.capability_name,
                granted_actions=child_grants,
                granted_at=datetime.now(timezone.utc),
                expires_at=parent.expires_at,  # 永远不会比父租约存活更久
                max_uses=parent.uses_remaining,  # 预算来自父租约的剩余
                uses_remaining=parent.uses_remaining,
                revoked=False,
                revoked_reason=None,
            ))

        # 在单个写锁下批量插入（M2：避免每次迭代加锁）
        async with self._lock:
            for child in child_leases:
                self.active[child.id] = child

        return child_leases

    async def find_and_consume(
            self,
            thread_id: ThreadId,
            action_name: str,
    ) -> CapabilityLease:
        """原子化地查找动作的租约并消耗一次使用次数

        避免 `find_lease_for_action`（读锁）和 `consume_use`（写锁）
        之间的 TOCTOU 竞态条件 — 两者在单个写锁下执行。
        如果找到且有效，返回租约快照（消耗后）
        """
        async with self._lock:
            for lease in self.active.values():
                if (lease.thread_id == thread_id
                        and lease.is_valid()
                        and lease.covers_action(action_name)):
                    if not lease.consume_use():
                        raise RuntimeError(f"LeaseExpired: capability_name={lease.capability_name}")
                    return lease

            raise RuntimeError(f"LeaseNotFound: 没有动作 '{action_name}' 的有效租约")
