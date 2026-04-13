from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Optional, Any
from uuid import UUID
from agentic_loop import LoopDelegate


# 聊天（分发器）上下文的委托实现。

# 实现 `LoopDelegate` 特质，为交互式聊天会话定制共享的智能体循环，
# 包含完整的三个阶段工具执行（预检 → 并行执行 → 后置处理）、审批流程、
# 钩子、认证拦截和成本跟踪。
class ChatDelegate(LoopDelegate):
    agent: Agent
    session: Session
    thread_id: UUID
    message: IncomingMessage
    job_ctx: JobContext
    active_skills: List[LoadedSkill]
    cached_prompt: str
    cached_prompt_no_tools: str
    nudge_at: int
    force_text_at: int
    user_tz: int


