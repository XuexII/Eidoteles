from enum import Enum, auto
from typing import TypedDict, ClassVar
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


class Submission(Enum):
    Undo = auto()
    Redo = auto()
    Interrupt = auto()
    Compact = auto()
    Clear = auto()
    Heartbeat = auto()
    Summarize = auto()
    Suggest = auto()
    NewThread = auto()
    SystemCommand = auto()
    Quit = auto()
    JobStatus = auto()
    JobCancel = auto()
    SwitchThread = auto()
    Resume = auto()
    ExecApproval = auto()
    ApprovalResponse = auto()
    UserInput = auto()

@dataclass
class Command:
    submission: Submission

    # def __init_subclass__(cls, **kwargs):
    #     """当子类被定义时自动调用，校验 submission 是否已设置"""
    #     if not hasattr(cls, 'submission') or not isinstance(cls.submission, Submission):
    #         raise TypeError(f"子类 {cls.__name__} 必须定义类变量 'submission', 且类型必须为'Submission'")
    #     super().__init_subclass__(**kwargs)

@dataclass(kw_only=True)
class UserInput(Command):
    content: str

class SubmissionParser:

    @classmethod
    def parse(cls, content: str) -> Command:
        """
        解析 message content 为BaseCommand类型
        :return:
        """

        trimmed = content.strip()
        lower = trimmed.lower()
        logger.info(f"[SubmissionParser.parse]解析输入{trimmed}")

        return UserInput(submission=Submission.UserInput, content=content)
