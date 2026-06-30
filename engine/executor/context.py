# 大语言模型调用的上下文构建。
#
# 从线程状态、活动租约以及通过 [`RetrievalEngine`] 检索到的项目记忆文档中，组装消息序列和动作定义。

import asyncio
from typing import List, Optional

from ..memory import RetrievalEngine
from ..traits.effect import EffectExecutor, ThreadExecutionContext
from ..types.capability import CapabilityLease
from ..types.memory import MemoryDoc, DocType
from ..types.message import ThreadMessage, MessageRole

# 注入到上下文中的记忆文档最大数量
MAX_CONTEXT_DOCS = 5


async def build_step_context(
        messages: List[ThreadMessage],
        leases: List[CapabilityLease],
        effects: EffectExecutor,
        retrieval: Optional[RetrievalEngine] = None,
        context: ThreadExecutionContext = None,
) -> tuple:
    """构建 LLM 调用的上下文：消息和可用动作

    从项目中检索相关的记忆文档，并将其作为系统消息
    注入到主系统提示之后。这使 LLM 能够访问先前线程中
    的经验教训、技能和已知问题
    """

    # 并行获取动作和记忆文档 — 它们是独立的
    async def fetch_actions():
        return await effects.available_actions(leases, context)

    async def fetch_docs():
        if retrieval is not None:
            return await retrieval.retrieve_context(
                context.project_id,
                context.user_id,
                context.thread_goal if context.thread_goal else "",
                MAX_CONTEXT_DOCS,
            )
        else:
            return []

    # 并行执行两个任务
    actions_result, docs = await asyncio.gather(
        fetch_actions(),
        fetch_docs(),
    )

    actions = actions_result
    ctx_messages = list(messages)

    # 将检索到的记忆文档注入到现有的系统提示中。
    # 许多提供商要求所有系统消息都在开头（或单个系统消息），
    # 因此我们追加到第一条系统消息，而不是插入单独的一条
    if docs:
        context_section = format_docs_as_context(docs)
        if ctx_messages and ctx_messages[0].role == MessageRole.System:
            # 追加到现有系统提示
            ctx_messages[0].content += "\n\n"
            ctx_messages[0].content += context_section
        else:
            # 没有系统消息 — 前置一条
            ctx_messages.insert(0, ThreadMessage.system(context_section))

    return (ctx_messages, actions)


def format_docs_as_context(docs: List[MemoryDoc]) -> str:
    """将记忆文档格式化为系统消息以用于上下文注入"""
    parts = ["## 先前知识（来自已完成的线程）\n"]

    for doc in docs:
        type_label = {
            DocType.Lesson: "经验教训",
            DocType.Spec: "缺失的能力",
            DocType.Issue: "已知问题",
            DocType.Summary: "上下文",
            DocType.Note: "笔记",
            DocType.Skill: "技能",
            DocType.Plan: "计划",
        }.get(doc.doc_type, "未知")

        # 截断长文档以避免上下文膨胀
        content = doc.content[:500]
        truncated = "..." if len(doc.content) > 500 else ""

        parts.append(
            f"### [{type_label}] {doc.title}\n{content}{truncated}\n"
        )

    return "\n".join(parts)
