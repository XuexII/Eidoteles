from dataclasses import dataclass, field
from typing import Dict, List, Optional
from context.state import JobContext
from context.memory import Memory

@dataclass
class ContextManager:
    """
    管理多个并发作业的上下文。
    """
    # 活跃作业的上下文
    contexts: Dict[str, JobContext]
    # 每个作业的记忆
    memories: Dict[str, Memory]
    # 最大并发作业数。
    max_jobs: int
