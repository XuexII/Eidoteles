import aiohttp
import asyncio
from openai import AsyncOpenAI
from openai import OpenAI
from dataclasses import dataclass, asdict
from typing import Any, Optional, List, Dict
from enum import Enum
from tools import Tool, ToolCall, get_tool_json_schema


class Roles(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL_CALL = "tool_calls"
    TOOL_RESPONSE = "tool"

    @classmethod
    def roles(cls):
        return [r.value for r in cls]


# @dataclass
# class ToolCallFunction:
#     arguments: Any
#     name: str
#     description: str | None = None
#
#
# @dataclass
# class ToolCall:
#     function: ToolCallFunction
#     id: str


def _coerce_tool_call(tool_call: Any) -> ToolCall:
    if isinstance(tool_call, ToolCall):
        return tool_call

    if isinstance(tool_call, dict):
        tool_call_dict = tool_call
    elif hasattr(tool_call, "model_dump"):
        tool_call_dict = tool_call.model_dump()
    elif hasattr(tool_call, "dict") and callable(tool_call.dict):
        tool_call_dict = tool_call.dict()

    return ToolCall(
        arguments = tool_call_dict["function"]["arguments"],
        id=tool_call_dict["id"],
    )


@dataclass
class Message:
    role: Roles
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    raw: Any | None = None  # Stores the raw output from the API

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            return
        self.tool_calls = [_coerce_tool_call(tool_call) for tool_call in self.tool_calls]

def get_dict_from_nested_dataclasses(obj, ignore_key=None):
    def convert(obj):
        if hasattr(obj, "__dataclass_fields__"):
            return {k: convert(v) for k, v in asdict(obj).items() if k != ignore_key}
        return obj

    return convert(obj)



DEFAULT_GEN_KWARGS = {
    "max_tokens": 512,
    "temperature": 0.1,
    "top_p": 0.1,
    "n": 1,
    "extra_body": {"chat_template_kwargs": {"enable_thinking": True}}
}


class LLM:
    def __init__(
            self,
            model_name,
            api_key,
            base_url,
            gen_kwargs=None,
            **kwargs
    ):
        self.model_name = model_name

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            **kwargs
        )
        self.gen_kwargs = gen_kwargs or DEFAULT_GEN_KWARGS

    def _prepare_llm_kwargs(
            self,
            messages: List[Message],
            tools: List[Tool] | None = None,
            stop: List[str] | None = None,
            **kwargs
    ):
        input_messages = []

        for msg in messages:
            role = msg.role
            imsg = {
                "role": role,
                "content": msg.content,
            }
            if role == Roles.ASSISTANT and msg.tool_calls:
                imsg[Roles.TOOL_CALL] = None

            input_messages.append(imsg)

        llm_kwargs = {**self.gen_kwargs}

        llm_kwargs["messages"] = input_messages
        if tools:
            llm_kwargs["tools"] = [get_tool_json_schema(tool) for tool in tools]

        if stop:
            llm_kwargs["stop"] = stop

        llm_kwargs.update(kwargs)

        return llm_kwargs

    async def agenerate(
            self,
            messages: List[Message],
            tools: List[Tool] | None = None,
            stop: List[str] | None = None,
            **kwargs
    ):

        llm__kwargs = self._prepare_llm_kwargs(messages, tools, stop)

        response = await self.client.chat.completions.create(**llm__kwargs)

        return Message(
            role=response.choices[0].message.role,
            content=response.choices[0].message.content,
            tool_calls=response.choices[0].message.tool_calls,
            raw=response
        )
