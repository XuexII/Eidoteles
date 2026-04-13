from enum import Enum, auto

class CompactionStrategy(Enum):
    # 总结旧消息并保留最近的消息。
    Summarize = auto()
    # 直接截断旧消息，不做总结。
    Truncate = auto()
    # 将上下文移至工作区内存。
    MoveToWorkspace = auto()