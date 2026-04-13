from agent.context_monitor import CompactionStrategy, ContextBreakdown
from agent.session import Thread
from error import Error
from llm import ChatMessage, CompletionRequest, LlmProvider, Reasoning
from workspace import Workspace
from typing import Optional


class ContextCompactor:
    def __init__(self, llm: LlmProvider):
        self.llm = llm

    async def compact(
            self,
            thread: Thread,
            strategy: CompactionStrategy,
            workspace: Optional[Workspace]
    ):
        """
        使用指定的策略压缩线程的上下文。
        """
        messages = thread.messages()
        tokens_before = ContextBreakdown.analyze(messages).total_tokens

        match strategy:
            case CompactionStrategy.Summarize(keep_recent):
                await self.compact_with_summary(thread, keep_recent, workspace)

    async def compact_with_summary(self, thread, keep_recent, workspace):
        """
        请简洁总结以下对话内容，重点包括：
        - 做出的关键决策
        - 交流的重要信息
        - 采取的行动
        - 取得的结果

        请言简意赅，但涵盖所有重要细节，使用项目符号呈现。
        """
