from dataclasses import dataclass
import logging
from llm import ChatMessage, LlmProvider
from typing import List, Optional
from agent.context_monitor import CompactionStrategy
from agent.session import Thread
from workspace import Workspace

logger = logging.getLogger(__name__)

class ContextCompactor:

    def __init__(self, llm: LlmProvider):
        self.llm = llm

    async def compact(self,
                      thread: Thread,
                      strategy: CompactionStrategy,
                      workspace: Optional[Workspace]):
        """
        使用指定的策略压缩线程的上下文。
        """
        pass