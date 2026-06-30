# 门控管道——多个 [`ExecutionGate`] 的有序评估。
#
# 门控在构造时按优先级排序。第一个返回 [`GateDecision::Pause`] 或 [`GateDecision::Deny`] 的门控胜出。
# 如果所有门控都返回 [`GateDecision::Allow`]，则继续执行。
#
# 门控实现不得 panic。发生 panic 的门控会将 panic 传播给调用者（不使用异步 `catch_unwind`，因为门控评估借用了非 `UnwindSafe` 的上下文）。

import logging
from dataclasses import dataclass, field
from typing import List

from ..gate import ExecutionGate, GateContext, GateDecision

logger = logging.getLogger(__name__)


@dataclass
class GatePipeline:
    """执行门控的有序管道"""
    gates: List[ExecutionGate] = field(default_factory=list)

    @classmethod
    def new(cls, gates: List[ExecutionGate]) -> "GatePipeline":
        """从给定的门控构建管道，按优先级排序（升序）"""
        sorted_gates = sorted(gates, key=lambda g: g.priority())
        return cls(gates=sorted_gates)

    @classmethod
    def allow_all(cls) -> "GatePipeline":
        """构建一个允许所有操作的空白管道（用于测试）"""
        return cls(gates=[])

    async def evaluate(self, ctx: GateContext) -> GateDecision:
        """按优先级顺序评估所有门控。第一个 `Pause` 或 `Deny` 胜出

        门控实现不得引发异常 — 异常会传播给调用者
        """
        for gate in self.gates:
            decision = await gate.evaluate(ctx)

            if isinstance(decision, GateDecision.Allow):
                continue
            elif isinstance(decision, (GateDecision.Pause, GateDecision.Deny)):
                logger.debug(
                    f"门控停止执行: gate={gate.name()}, tool={ctx.action_name}"
                )
                return decision

        return GateDecision.Allow()
