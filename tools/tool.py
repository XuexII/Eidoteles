from abc import ABC, abstractmethod
from context import JobContext
from pydantic import BaseModel, ConfigDict, Field
from typing import Dict


class ToolSchema(BaseModel):
    name: str
    description: str
    parameters: Dict


class Tool(ABC):

    @abstractmethod
    def name(self):
        """
        工具名称
        """
        pass

    @abstractmethod
    def description(self):
        """
        工具描述
        """
        pass

    @abstractmethod
    def parameters_schema(self):
        """
        工具参数的json结构
        """
        pass

    @abstractmethod
    async def execute(self, params, ctx: JobContext):
        """
        执行工具
        """
        pass

    def estimated_cost(self, params):
        """
        估算使用给定参数运行此工具的成本。
        """
        return None

    def estimated_duration(self, params):
        """
        估算使用给定参数运行此工具所需的时间。
        """
        return None

    def schema(self):
        """
        获取用于大语言模型函数调用的工具架构。
        """
        tool_schema = ToolSchema(
            name=self.name(),
            description=self.description(),
            parameters=self.parameters_schema()
        )