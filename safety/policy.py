"""
安全策略规则。
"""

from dataclasses import dataclass, field
from enum import Enum

import regex as re


@dataclass
class Severity:
    """
    安全问题的严重级别。
    """
    Low = 1
    Medium = 2
    High = 3
    Critical = 4


class PolicyAction(str, Enum):
    """策略被违反时采取的动作"""
    # 记录警告但允许
    Warn = "warn"
    # 完全阻止内容
    Block = "block"
    # 需要人工审查
    Review = "review"
    # 清理并继续
    Sanitize = "sanitize"


@dataclass
class PolicyRule:
    id: str
    # 规则描述
    description: str
    # 违反时的严重程度
    severity: Severity
    # 匹配的正则
    pattern: re.Pattern[str]
    # 违反时采取的操作。
    action: PolicyAction

    def matches(self, content: str) -> bool:
        """
        检查内容是否匹配此规则。
        """
        return bool(self.pattern.match(content))


@dataclass
class Policy:
    rules: list[PolicyRule] = field(default_factory=list)

    def check(self, content: str) -> list[PolicyRule]:
        """
        根据所有规则检查内容。
        """

        return [rule for rule in self.rules if rule.matches(content)]
