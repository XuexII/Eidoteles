# 基于回合的智能体循环的提交类型。
# 提交是智能体可以接收和处理的不同类型的输入，作为基于回合的开发循环的一部分。

import logging
from enum import Enum
from typing import Optional, List, Union
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict


class BaseCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    def __eq__(self, other):
        return type(self) is type(other)


class UserInput(BaseCommand):
    """
    # 用户输入文本（开始新回合）。
    """
    content: str


class ExecApproval(BaseCommand):
    """
    对执行批准请求（带有明确的请求 ID）的响应。
    """
    # 正在响应的审批请求的 ID。
    request_id: UUID
    # 执行是否已被批准。
    approved: bool
    # 如果为真，则在本次会话剩余时间内自动批准此工具。
    always: bool


class ApprovalResponse(BaseCommand):
    """
    对当前待审批事项的简单批准回复 (yes/no/always)
    """
    # 执行是否已被批准。
    approved: bool
    # 如果为真，则在本次会话剩余时间内自动批准此工具。
    always: bool


class Interrupt(BaseCommand):
    """
    打断当前的回合。
    """
    pass


class Compact(BaseCommand):
    """
    请求上下文压缩。
    """
    pass


class Undo(BaseCommand):
    """
    撤销上一步操作
    """
    pass


class Redo(BaseCommand):
    """
    重新执行之前未执行的操作（如果可用）。
    """
    pass


class Resume(BaseCommand):
    """
    从特定检查点恢复。
    """

    # 要从中恢复的检查点的 ID。
    checkpoint_id: UUID


class Clear(BaseCommand):
    """
    清空当前线程，重新开始。
    """
    pass


class SwitchThread(BaseCommand):
    """
    换到另一个线程
    """
    # 要切换到的线程ID
    thread_id: UUID


class NewThread(BaseCommand):
    """
    创建一个新的线程
    """
    pass


class Heartbeat(BaseCommand):
    """
    触发手动心跳检查。
    """
    pass


class Summarize(BaseCommand):
    """
    总结当前线程
    """
    pass


class Suggest(BaseCommand):
    """
    根据当前线程提出后续步骤建议。
    """
    pass


class JobStatus(BaseCommand):
    """
    检查job状态。不通过job_id则显示所有jobs，否则只显示指定的job
    """
    job_id: Optional[str] = Field(default=None)


class JobCancel(BaseCommand):
    """
    取消一个正在运行的job
    """
    job_id: Optional[str] = Field(default=None)


class Quit(BaseCommand):
    """
    退出代理。绕过线程状态检查。
    """
    pass


class SystemCommand(BaseCommand):
    """
    系统命令 (help, model, version, tools, ping, debug)
    """
    # 命令名称
    command: str
    # 命令的参数
    args: List[str]


class Submission:
    Undo = Undo
    Redo = Redo
    Interrupt = Interrupt
    Compact = Compact
    Clear = Clear
    Heartbeat = Heartbeat
    Summarize = Summarize
    Suggest = Suggest
    NewThread = NewThread
    SystemCommand = SystemCommand
    Quit = Quit
    JobStatus = JobStatus
    JobCancel = JobCancel
    SwitchThread = SwitchThread
    Resume = Resume
    ExecApproval = ExecApproval
    ApprovalResponse = ApprovalResponse
    UserInput = UserInput


SUBMISSION_TYPES = Union[
    Undo, Redo, Interrupt, Compact, Clear, Heartbeat, Summarize, Suggest, NewThread, SystemCommand, Quit, JobStatus, JobCancel, SwitchThread, Resume, ExecApproval, ApprovalResponse, UserInput]


class SubmissionParser:
    """
    将用户输入解析为Submission类型。
    """

    @classmethod
    def parse(cls, content: str) -> SUBMISSION_TYPES:
        """
        解析 message content 为Submission类型
        :return:
        """

        trimmed = content.strip()
        lower = trimmed.lower()
        logging.debug(f"[SubmissionParser.parse]解析输入{trimmed}")

        # 控制命令（精确匹配或前缀匹配）
        if lower == "/undo":
            return Submission.Undo()

        if lower == "/redo":
            return Submission.Redo()
        if lower == "/interrupt" or lower == "/stop":
            return Submission.Interrupt()

        if lower == "/compact":
            return Submission.Compact()

        if lower == "/clear":
            return Submission.Clear()

        if lower == "/heartbeat":
            return Submission.Heartbeat()

        if lower == "/summarize" or lower == "/summary":
            return Submission.Summarize()

        if lower == "/suggest":
            return Submission.Suggest()

        if lower == "/thread new" or lower == "/new":
            return Submission.NewThread()

        # 系统命令（绕过线程状态检查）
        if lower == "/help" or lower == "/?":
            return Submission.SystemCommand(command="help", args=[])

        if lower == "/version":
            return Submission.SystemCommand(command="version", args=[])

        if lower == "/tools":
            return Submission.SystemCommand(command="tools", args=[])

        if lower == "/skills":
            return Submission.SystemCommand(command="skills", args=[])

        if lower.startswith("/skills "):
            args = trimmed.strip()[1:]
            return Submission.SystemCommand(command="skills", args=args)

        if lower == "/ping":
            return Submission.SystemCommand(command="ping", args=[])

        if lower == "/debug":
            return Submission.SystemCommand(command="debug", args=[])

        if lower == "/restart":
            logging.debug("[SubmissionParser.parse] 识别到 /restart 命令")
            return Submission.SystemCommand(command="restart", args=[])

        if lower.startswith("/model"):
            args = trimmed.strip()[1:]
            return Submission.SystemCommand(command="model", args=args)

        if lower == "/quit" or lower == "/exit" or lower == "/shutdown":
            return Submission.Quit()

        # Job 命令
        if lower == "/status" or lower == "/progress":
            return Submission.JobStatus(job_id=None)

        if job_id := (lower.removeprefix("/status ") or lower.removeprefix("/progress ")).strip():
            return Submission.JobStatus(job_id=job_id)

        if lower == "/list":
            return Submission.JobStatus(job_id=None)

        if job_id := lower.removeprefix("/cancel ").strip():
            return Submission.JobCancel(job_id=job_id)

        # /thread <uuid> - 切换线程
        if rest := lower.removeprefix("/thread ").strip():
            if rest != "new":
                try:
                    thread_id = UUID(rest)
                    return Submission.SwitchThread(thread_id=thread_id)
                except Exception as e:
                    pass

        # /resume <uuid> - 从检查点恢复
        if rest := lower.removeprefix("/resume ").strip():
            try:
                checkpoint_id = UUID(rest)
                return Submission.Resume(checkpoint_id=checkpoint_id)
            except Exception as e:
                pass

        # 尝试使用结构化的 JSON 审批（通过 Web 网关的 /api/chat/approval 端点）
        if trimmed.startswith('{'):
            try:
                submission = Submission.ExecApproval.model_validate_json(trimmed)
                return submission
            except Exception as e:
                pass
            submission = ""

        # 审批响应 (用 yes/no/always 响应)
        # 这些响应足够简短，可以进行显式检查。
        if lower in ["yes", "y", "approve", "ok", "/approve", "/yes", "/y"]:
            return Submission.ApprovalResponse(approved=True, always=False)

        elif lower in ["always", "a", "yes always", "approve always", "/always", "/a"]:
            return Submission.ApprovalResponse(approved=True, always=True)

        elif lower in ["no", "n", "deny", "reject", "cancel", "/deny", "/no", "/n"]:
            return Submission.ApprovalResponse(approved=False, always=False)

        # Default: user input
        return Submission.UserInput(content=content)
