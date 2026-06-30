# 租约门控——拒绝没有有效能力租约的工具调用。
#
# 优先级 10：在所有其他门控之前运行（若无租约则尽早拒绝）。
# 这取代了 v1 的 `ApprovalContext::is_blocked_or_default()`、
# `check_approval_in_context()` 以及轻量级例程中临时使用的 `allowed_tools: HashSet`。

import logging
from dataclasses import dataclass

from ..capability.lease import LeaseManager
from ..gate import GateContext, GateDecision

logger = logging.getLogger(__name__)


@dataclass
class LeaseGate:
    """拒绝未被有效能力租约覆盖的工具调用的门控

    故障关闭：如果动作不存在租约，执行被拒绝。
    这是引擎的主要授权门控
    """
    lease_manager: LeaseManager
    # 为 true 时，跳过租约检查（用于交互式线程，
    # 其中租约仍由规划器授予）
    permissive: bool = False

    @classmethod
    def new(cls, lease_manager: LeaseManager) -> "LeaseGate":
        """创建一个强制执行租约检查的租约门控"""
        return cls(lease_manager=lease_manager, permissive=False)

    @classmethod
    def permissive(cls, lease_manager: LeaseManager) -> "LeaseGate":
        """创建一个允许所有动作的宽松门控（用于 Foreground 线程，
        其中交互式批准处理授权）
        """
        return cls(lease_manager=lease_manager, permissive=True)

    def name(self) -> str:
        """门控名称"""
        return "lease"

    def priority(self) -> int:
        """门控优先级"""
        return 10

    async def evaluate(self, ctx: GateContext) -> GateDecision:
        """评估门控：检查是否存在有效的租约"""
        if self.permissive:
            return GateDecision.Allow()

        lease = await self.lease_manager.find_lease_for_action(
            ctx.thread_id, ctx.action_name
        )

        if lease is not None and lease.is_valid():
            return GateDecision.Allow()
        else:
            return GateDecision.Deny(
                reason=f"线程 {ctx.thread_id} 上动作 '{ctx.action_name}' 没有有效租约"
            )
