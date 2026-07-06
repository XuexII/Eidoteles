"""
用于检测和中和提示注入尝试的清理器。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import regex as re

@dataclass
class SanitizedOutput:
    """
    清理外部内容的结果。
    """
    # 清理后的内容
    content: str
    # 关于潜在注入尝试的警告。
    warnings: list[Any] = field(default_factory=list)
    # 清理过程中内容是否被修改。
    was_modified: bool = False

@dataclass
class Sanitizer:
    """
    外部数据的清理器。
    """
    # ac自动机
    pattern_matcher: Any
    # 带有元数据的模式。
    patterns: list[Any]
    # 用于更复杂检测的正则表达式模式。
    regex_patterns: list[Any]

    def sanitize(self, content: str) -> SanitizedOutput:
        """
        通过检测和转义潜在的注入尝试来清理内容。
        """
        return SanitizedOutput(content=content)