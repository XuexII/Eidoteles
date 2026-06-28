from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from channels import IncomingMessage

@dataclass
class CreateJob:
    """
    新任务
    """
    title: str
    description: str
    category: Optional[str] = None

@dataclass
class CheckJobStatus:
    """
    检查任务状态
    """
    job_id: Optional[str] = None


@dataclass
class CancelJob:
    """
    取消任务
    """
    job_id: str

@dataclass
class ListJobs:
    """
    任务列表
    """
    filter: Optional[str] = None

@dataclass
class HelpJob:
    """
    帮助棘手的工作
    """
    job_id: str


@dataclass
class Chat:
    """
    一般对话/问题。
    """
    content: str

@dataclass
class Command:
    """
    系统指令
    """
    command: str
    args: List[str]

@dataclass
class Unknown:
    pass


class Router:
    """
    根据显式命令将消息路由到相应的处理程序。
    对于自然语言消息，请改用 `IntentClassifier`。
    """

    def __init__(self, command_prefix="/"):
        self.command_prefix = command_prefix

    def route_command(self, message: IncomingMessage):
        pass
