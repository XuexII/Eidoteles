from dataclasses import dataclass, field
from typing import List, Dict, Optional
import logging
from pathlib import Path
from llm.provider import LlmProvider

logger = logging.getLogger(__name__)







@dataclass
class Reasoning:
    """
    智能体的推理引擎。
    """
    llm: LlmProvider
    # 用于加载身份/系统提示词的可选工作区。
    workspace_system_prompt: Optional[str] = None
    # 可选的技能上下文块，用于注入系统提示词。
    skill_context: Optional[str] = None
    # 频道名称（例如 "discord"、"telegram"），用于格式化提示。
    channel: Optional[str] = None
    # 运行上下文中的模型名称。
    model_name: Optional[str] = None
    is_group_chat: bool = False
    # 特定频道的对话上下文（例如发送者号码、UUID、群组 ID）。
    # 这将被传递给大语言模型，以便让其清晰了解正在与谁/哪个群组对话。
    conversation_context: Dict[str, str] = field(default_factory=dict)
