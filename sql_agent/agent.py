from asyncio import set_event_loop
from typing import List, Dict, Optional, TypedDict, Any
from llm import LLM, Message, Roles
from tools import Tool, ToolCall, validate_tool_arguments
from context_manager import Contexts, PlanStep, QueryStep, ActionStep
from jinja2 import StrictUndefined, Template
from utils import Timing, parse_json_if_needed
import logging
import time
from copy import deepcopy
from erros import (
    AgentError,
    AgentExecutionError,
    AgentGenerationError,
    AgentMaxStepsError,
    AgentParsingError,
    AgentToolCallError,
    AgentToolExecutionError
)
import asyncio
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class PromptTemplates(TypedDict):
    system: str
    plan: str

def populate_template(template: str, variables: dict[str, Any]) -> str:
    compiled_template = Template(template, undefined=StrictUndefined)
    try:
        return compiled_template.render(**variables)
    except Exception as e:
        raise Exception(f"Error during jinja template rendering: {type(e).__name__}: {e}")


@dataclass
class ToolOutput:
    id: str
    output: Any
    is_final_answer: bool
    observation: str
    tool_call: ToolCall

class Agent:

    def __init__(
            self,
            llm: LLM,
            prompt_templates: PromptTemplates,
            tools: List[Tool],
            max_steps: int = 20

    ):
        self.llm = llm
        self.prompt_templates = prompt_templates
        self.tools = tools
        self.max_steps = max_steps

    @property
    def system_prompt(self) -> str:
        system_prompt = populate_template(
            self.prompt_templates["system"],
            variables={
                "tools": self.tools
            },
        )
        return system_prompt

    def write_memory_to_messages(
        self,
        summary_mode: bool = False,
    ) -> list[Message]:
        """
        Reads past llm_outputs, actions, and observations or errors from the memory into a series of messages
        that can be used as input to the LLM. Adds a number of keywords (such as PLAN, error, etc) to help
        the LLM.
        """
        messages = self.memory.system_prompt.to_messages(summary_mode=summary_mode)
        for memory_step in self.memory.steps:
            messages.extend(memory_step.to_messages(summary_mode=summary_mode))
        return messages

    async def _generate_plan(self, query: str):
        """
        生成计划
        """
        prompt_template = self.prompt_templates["plan"]
        messages = [
            Message(
                role=Roles.USER,
                content=populate_template(prompt_template, variables={"query": query, "tools": self.tools})
            )
        ]

        response = await self.llm.agenerate(messages, stop=["<end_plan>"])
        plan = response.content
        # TODO 清除标签

        step = PlanStep(
            model_input_messages=messages,
            plan=plan
        )
        return step

    async def exe_tool(self, tool_call: ToolCall):
        tool_name = tool_call.name
        args = tool_name.arguments or {}
        # 检查工具是否为提供的工具
        if tool_name not in self.tools:
            raise AgentToolExecutionError(f"未知工具 {tool_name}，应该是以下之一：{', '.join(self.tools)}。")

        tool = self.tools[tool_name]
        # 检查工具参数
        try:
            validate_tool_arguments(tool, args)
        except (ValueError, TypeError) as e:
            raise AgentToolCallError(str(e)) from e
        except Exception as e:
            error_msg = f"执行工具 '{tool_name}' 时出错, 参数为 {str(args)}: {type(e).__name__}: {e}"
            raise AgentToolExecutionError(error_msg) from e

        # 执行工具
        try:
            return await tool(**args)
        except Exception as e:
            error_msg = f"执行工具 '{tool_name}' 时出错，参数为 {str(args)}：{type(e).__name__}：{e}\n请重试或使用其他工具"

            raise AgentToolExecutionError(error_msg) from e


    async def _execute(self, message: Message, action_step: ActionStep):
        parallel_calls = {}
        tasks = []
        for tool_call in message.tool_calls:
            tool_call = ToolCall(
                name=tool_call.function.name, arguments=tool_call.function.arguments, id=tool_call.id
            )
            parallel_calls[tool_call.id] = tool_call
            tasks.append(self.exe_tool(tool_call))

        results = {}
        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
            except Exception as e:
                # 某个任务报错会在这里捕获
                print(f"任务出错: {e}")
            else:
                print(f"完成: {result}")


    async def _react(self, contexts: Contexts, action_step: ActionStep):
        """
        执行计划
        """
        messages = contexts.write_memory_to_messages()
        input_messages = deepcopy(messages)
        action_step.model_input_messages = input_messages

        try:
            response = await self.llm.agenerate(
                input_messages,
                self.tools,
            )
            action_step.model_output_message = response
            action_step.model_output = response.content

        except Exception as e:
            raise RuntimeError(f"请求大模型是报错: {e}")

        if not response.tool_calls:
            raise RuntimeError(f"模型没有调用工具")

        for tool_call in response.tool_calls:
            tool_call.function.arguments = parse_json_if_needed(tool_call.function.arguments)





    async def run(self, query: str):
        # 初始化上下文管理器
        contexts = Contexts(system_prompt=self.system_prompt)
        logger.info(f"收到请求任务: {query}")

        contexts.steps.append(QueryStep(query=query))

        # 生成计划
        plan_start_time = time.time()
        plan_step = await self._generate_plan(query)
        plan_end_time = time.time()
        plan_step.timing = Timing(
            start_time=plan_start_time,
            end_time=plan_end_time,
        )
        contexts.steps.append(plan_step)
        # 执行计划
        step_number = 1
        action_start_time = time.time()
        action_step = ActionStep(
            step_number=step_number,
            timing=Timing(start_time=time.time()),
        )




