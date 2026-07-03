# 基于宿主 `CostGuard` 的 `BudgetGate` 适配器。
#
# 实现引擎任务系统的 `BudgetGate` 特质，以便在用户用尽每日大语言模型预算时拒绝触发任务。
# 任务管理器在每次生成线程前调用 `allow_mission_fire`；返回 `false` 将中止生成，且不消耗任务的每日配额。

from dataclasses import dataclass
import logging
from engine import BudgetGate, MissionId
from agent import CostGuard

logger = logging.getLogger(__name__)


@dataclass
class CostGuardBudgetGate:
    """将主机 `CostGuard` 适配为引擎 `BudgetGate` 接口"""
    cost_guard: CostGuard  # CostGuard

    async def allow_mission_fire(self, user_id: str, mission_id: MissionId) -> bool:
        """如果允许 `user_id` 触发任务则返回 `True`。
        包含 `mission_id` 以便适配器可以根据需要应用每个任务的策略；
        大多数实现将仅参考 `user_id`

        Args:
            user_id: 用户标识符
            mission_id: 任务标识符

        Returns:
            如果成本守卫允许该用户则返回 True，否则返回 False
        """
        try:
            await self.cost_guard.check_allowed_for_user(user_id)
            return True
        except Exception as error:
            logger.debug(
                f"任务触发被拒绝 — 成本守卫拒绝用户: "
                f"user_id={user_id}, mission_id={mission_id}, error={error}"
            )
            return False