from enum import Enum, auto
from typing import TypedDict, ClassVar, Optional, Dict, List, Tuple, Union
from dataclasses import dataclass, field
import logging
from utils.async_schems import RWLockDict
from agent.session import Session
from agent.undo import UndoManager
from hooks import HookRegistry
from llm import ChatMessage


logger = logging.getLogger(__name__)

# 默认上下文窗口限制（保守估计）
DEFAULT_CONTEXT_LIMIT = 100_000
# 压缩阈值占限制的百分比。
COMPACTION_THRESHOLD = 0.8
# 每个单词的近似令牌数（适用于英文的粗略估算）。
TOKENS_PER_WORD = 1.3

@dataclass
class Summarize:
    # 要完整保留的最近轮次数量。
    keep_recent: int

@dataclass
class Truncate:
    # 要保留的最近轮次数量。
    keep_recent: int

@dataclass
class MoveToWorkspace:
    pass

CompactionStrategy = Union[Summarize, Truncate, MoveToWorkspace]



@dataclass
class ContextMonitor:
    """
    监控上下文大小并建议压缩。
    """
    # 上下文运行的最大token数量
    context_limit: int = DEFAULT_CONTEXT_LIMIT
    # 触发压缩的阈值比例。
    threshold_ratio: float = COMPACTION_THRESHOLD


    def estimate_tokens(self, messages: List[ChatMessage]):
        """
        估计token数量
        """
        return 0

    def needs_compaction(self, messages: List[ChatMessage]):
        """
        是否需要压缩
        """
        tokens = self.estimate_tokens(messages)
        threshold = self.context_limit * self.threshold_ratio

        return tokens >= threshold

    def usage_percent(self, messages: List[ChatMessage]):
        """
        usage_percent
        """
        tokens = self.estimate_tokens(messages)
        return (tokens / self.context_limit) * 100

    def suggest_compaction(self, messages: List[ChatMessage])->Optional[CompactionStrategy]:
        if not self.needs_compaction(messages):
            return None

        tokens = self.estimate_tokens(messages)

        overage = tokens / self.context_limit

        if overage > 0.95:
            # 关键：激进截断模式
            return Truncate(keep_recent=3)
        elif overage > 0.85:
            # 高：进行总结并保留更少内容。
            return Truncate(keep_recent=5)
        else:
            # 中等：移至工作区。
            return MoveToWorkspace()


