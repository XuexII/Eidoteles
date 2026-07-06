"""
安全层中的输入验证
"""

from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    # 是否合法
    is_valid: bool = True
    # 验证的所有错误
    errors: list[Exception] = field(default_factory=list)
    # 警告
    warnings: list[str] = field(default_factory=list)


@dataclass
class Validator:
    # 输入的最大长度
    max_length: int = 100_000
    # 输入的最小长度
    min_length: int = 1
    # 禁止的子字符串
    forbidden_patterns: set[str] = field(default_factory=set)

    def validate(self, content: str) -> ValidationResult:
        return ValidationResult()
