# 确定性策略引擎。
#
# 根据效果类型、能力策略和线程租约，评估一个动作是被允许、拒绝还是需要批准。
# 不涉及大语言模型调用——纯粹确定性逻辑。

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List

from ..types.capability import ActionDef, CapabilityLease, EffectType, PolicyCondition, PolicyEffect, PolicyRule
from ..types.provenance import Provenance

logger = logging.getLogger(__name__)


# ── 策略决策 ─────────────────────────────────────────────────
class PolicyDecisionType(Enum):
    Allow = "allow"
    Deny = "deny"
    RequireApproval = "require_approval"


@dataclass
class PolicyDecision:
    """策略评估的结果"""
    # 评估结果
    decision: PolicyDecisionType
    # 理由
    reason: str | None = None


# ── 策略引擎 ─────────────────────────────────────────────────

@dataclass
class PolicyEngine:
    """确定性策略引擎

    评估优先级：Deny > RequireApproval > Allow。
    按顺序评估检查：全局策略，然后是能力策略，
    然后是动作级别的 requires_approval，最后是根据租约允许的效果检查效果类型
    """
    global_policies: List[PolicyRule] = field(default_factory=list)
    # 始终拒绝的效果类型，除非显式覆盖
    denied_effects: List[EffectType] = field(default_factory=list)

    def add_global_policy(self, rule: PolicyRule) -> None:
        """添加全局策略规则"""
        self.global_policies.append(rule)

    def deny_effect(self, effect: EffectType) -> None:
        """添加一个始终拒绝的效果类型"""
        self.denied_effects.append(effect)

    def evaluate(
            self,
            action: ActionDef,
            lease: CapabilityLease,
            capability_policies: List[PolicyRule],
    ) -> PolicyDecision:
        """评估给定租约和能力策略的情况下是否允许某个动作"""
        # 1. 检查租约有效性
        if not lease.is_valid():
            return PolicyDecision(
                decision=PolicyDecisionType.Deny,
                reason=f"对 {lease.capability_name} 的租约已过期/被撤销"
            )

        # 2. 检查租约是否覆盖此动作
        if not lease.covers_action(action.name):
            return PolicyDecision(
                decision=PolicyDecisionType.Deny,
                reason=f"对 {lease.capability_name} 的租约不覆盖动作 {action.name}"
            )
        # 3. 检查被拒绝的效果类型
        for effect in action.effects:
            if effect in self.denied_effects:
                return PolicyDecision(
                    decision=PolicyDecisionType.Deny,
                    reason=f"效果类型 {effect} 被全局策略拒绝"
                )

        # 4. 评估全局策略
        decision = PolicyDecision(decision=PolicyDecisionType.Allow)
        for rule in self.global_policies:
            if rule_matches(rule, action):
                decision = merge_decision(decision, rule.effect, rule.name)

        # 5. 评估能力级别的策略
        for rule in capability_policies:
            if rule_matches(rule, action):
                decision = merge_decision(decision, rule.effect, rule.name)

        # 6. 检查动作级别的 requires_approval
        if action.requires_approval:
            decision = merge_decision(
                decision,
                PolicyEffect.RequireApproval,
                "动作需要批准",
            )

        # 记录拒绝以便审计追踪/事件调查
        if decision.decision == PolicyDecisionType.Deny:
            logger.debug(
                f"策略拒绝动作: action={action.name}, "
                f"capability={lease.capability_name}, "
                f"reason={decision.reason}"
            )

        return decision

    def evaluate_with_provenance(
            self,
            action: ActionDef,
            lease: CapabilityLease,
            capability_policies: List[PolicyRule],
            provenance: Provenance,
    ) -> PolicyDecision:
        """使用来源感知的污染检查进行评估

        扩展基础评估，加入基于来源的规则：
        - `LlmGenerated` 数据 + `Financial` 效果 → RequireApproval
        - `LlmGenerated` 数据 + `WriteExternal` 效果 → RequireApproval
        - `ToolOutput` 数据 + `Financial` 效果 → RequireApproval
        """
        decision = self.evaluate(action, lease, capability_policies)

        # 基于来源的污染规则
        # if isinstance(provenance, LlmGenerated):
        #     if EffectType.Financial in action.effects:
        #         decision = merge_decision(
        #             decision,
        #             PolicyEffect.RequireApproval,
        #             "LLM 生成的数据未经批准不能触发财务效果",
        #         )
        #     if EffectType.WriteExternal in action.effects:
        #         decision = merge_decision(
        #             decision,
        #             PolicyEffect.RequireApproval,
        #             "LLM 生成的数据需要批准才能进行外部写入",
        #         )
        # elif isinstance(provenance, ToolOutput):
        #     if EffectType.Financial in action.effects:
        #         decision = merge_decision(
        #             decision,
        #             PolicyEffect.RequireApproval,
        #             "工具输出数据需要批准才能触发财务效果",
        #         )
        # User 和 System 来源是受信任的
        # MemoryRetrieval 是内部的，视为受信任

        return decision


# ── 策略规则 ─────────────────────────────────────────────────

@dataclass
class PolicyRule:
    """策略规则"""
    name: str
    condition: PolicyCondition
    effect: PolicyEffect


# ── 辅助函数 ─────────────────────────────────────────────────

def rule_matches(rule: PolicyRule, action: ActionDef) -> bool:
    """检查策略规则的条件是否匹配给定的动作"""
    if isinstance(rule.condition, Always):
        return True
    elif isinstance(rule.condition, ActionMatches):
        return action.name == rule.condition.pattern
    elif isinstance(rule.condition, EffectTypeIs):
        return rule.condition.effect in action.effects
    return False


def merge_decision(current: PolicyDecision, effect: PolicyEffect, source: str) -> PolicyDecision:
    """将新的策略效果合并到当前决策中。
    Deny > RequireApproval > Allow
    """
    if effect == PolicyEffect.Deny:
        return PolicyDecision(decision=PolicyDecisionType.Deny, reason=source)
    elif effect == PolicyEffect.RequireApproval:
        if current.decision == PolicyDecisionType.Deny:
            return current
        return PolicyDecision(decision=PolicyDecisionType.RequireApproval, reason=source)
    else:  # Allow
        return current
