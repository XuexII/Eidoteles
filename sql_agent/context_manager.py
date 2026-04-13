from typing import List, Any, Dict, Union
from dataclasses import dataclass, asdict
from llm import Message, Roles, get_dict_from_nested_dataclasses
from utils import Timing, make_json_serializable


@dataclass
class QueryStep:
    query: str

    def to_messages(self, summary_mode: bool = False) -> list[Message]:
        content = f"收到新任务: {self.query}"

        return [Message(role=Roles.USER, content=content)]


@dataclass
class PlanStep:
    model_input_messages: list[Message]
    plan: str
    timing: Timing

    def dict(self):
        return {
            "model_input_messages": [
                make_json_serializable(get_dict_from_nested_dataclasses(msg)) for msg in self.model_input_messages
            ],
            "plan": self.plan,
            "timing": self.timing.dict(),
        }

    def to_messages(self, summary_mode: bool = False) -> list[Message]:
        if summary_mode:
            return []
        return [
            Message(role=Roles.ASSISTANT, content=self.plan.strip()),
            Message(
                role=Roles.USER, content="现在开始执行这个计划。"
            ),
        ]

@dataclass
class ActionStep:
    step_number: int
    timing: Timing
    model_input_messages: list[Message] | None = None
    tool_calls: list[ToolCall] | None = None
    error: AgentError | None = None
    model_output_message: Message | None = None
    model_output: str | list[dict[str, Any]] | None = None
    code_action: str | None = None
    observations: str | None = None
    action_output: Any = None
    is_final_answer: bool = False

@dataclass
class SystemPrompt:
    prompt: str

    def to_messages(self, summary_mode: bool = False) -> list[Message]:
        if summary_mode:
            return []
        return [Message(role=Roles.SYSTEM, content=self.prompt)]

class Contexts:

    def __init__(self, system_prompt: str):
        self.system_prompt = SystemPrompt(prompt=system_prompt)
        self.steps: List = []


    def write_memory_to_messages(
        self,
        summary_mode: bool = False,
    ) -> list[Message]:
        """
        Reads past llm_outputs, actions, and observations or errors from the memory into a series of messages
        that can be used as input to the LLM. Adds a number of keywords (such as PLAN, error, etc) to help
        the LLM.
        """
        messages = self.system_prompt.to_messages(summary_mode=summary_mode)
        for step in self.steps:
            messages.extend(step.to_messages(summary_mode=summary_mode))
        return messages


