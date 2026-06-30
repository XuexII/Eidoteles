# 新线程的租约规划。
#
# 将能力注册表内容与线程类型结合，生成明确的能力授予。
# 线程类型感知：前台线程获得所有层级，研究线程获得只读+有状态能力，任务线程排除管理工具。

from dataclasses import dataclass, field
from typing import List
from .registry import CapabilityRegistry
from ..gate.tool_tier import ToolTier, classify_tool_tier, is_autonomous_denylisted
from ..types.capability import GrantedType, GrantedActions
from ..types.thread import ThreadType


# ── 能力授予计划 ─────────────────────────────────────────────


@dataclass
class CapabilityGrantPlan:
    """单个能力的显式授予计划"""
    capability_name: str
    granted_actions: GrantedActions


# ── 租约规划器 ───────────────────────────────────────────────

@dataclass
class LeasePlanner:
    """为新线程规划显式能力租约

    使用 [`ToolTier`] 分类按线程类型限定授予范围：
    - **Foreground**：所有层级（交互式批准门控保护 Privileged/Admin）
    - **Research**：仅 `ReadOnly` 和 `Stateful`
    - **Mission**：`ReadOnly`、`Stateful` 和非拒绝列表中的 `Privileged`
    """

    def plan_for_thread(
            self,
            thread_type: ThreadType,
            capabilities: CapabilityRegistry,
    ) -> List[CapabilityGrantPlan]:
        """为新线程构建能力授予计划"""
        plans = []
        for cap in capabilities.list():
            granted_actions = []
            for action in cap.actions:
                tier = classify_tool_tier(action)
                if self.tier_allowed(thread_type, action.name, tier):
                    granted_actions.append(action.name)

            if granted_actions:
                plans.append(CapabilityGrantPlan(
                    capability_name=cap.name,
                    granted_actions=GrantedActions(type=GrantedType.Specific, granted_actions),
                ))

        return plans

    @staticmethod
    def tier_allowed(thread_type: ThreadType, action_name: str, tier: ToolTier) -> bool:
        """检查给定线程类型是否允许某个工具层级"""
        if thread_type == ThreadType.Foreground:
            # Foreground 可以获得一切 — 交互式批准门控
            # 保护 Privileged 和 Administrative 工具
            return True
        elif thread_type == ThreadType.Research:
            # Research 线程：仅只读和有状态
            return tier in (ToolTier.ReadOnly, ToolTier.Stateful)
        elif thread_type == ThreadType.Mission:
            # Mission 线程：无 Administrative，无拒绝列表中的 Privileged
            if tier in (ToolTier.ReadOnly, ToolTier.Stateful):
                return True
            elif tier == ToolTier.Privileged:
                return not is_autonomous_denylisted(action_name)
            elif tier == ToolTier.Administrative:
                return False
        return False
