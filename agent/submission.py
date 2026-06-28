from enum import Enum, auto
from typing import TypedDict, ClassVar, Optional, Any, List
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


@dataclass
class UserInput:
    """用户文本输入（开始新的一轮对话）。"""
    content: str

@dataclass
class ExecApproval:
    """
    对执行批准请求的响应（附带明确的请求 ID）。
    """
    # 所响应的批准请求的 ID。
    request_id: str
    # 执行是否被批准。
    approved: bool
    # 若为 true，则在本会话剩余时间内自动批准此工具。
    always: bool


@dataclass
class ExternalCallback:
    """
    外部系统解决了待处理的门控（例如 OAuth 回调或调用者执行工具的结果）。
    """
    # 被解决的待处理门控请求的 ID。
    request_id: str
    # 随解决过程提供的可选负载。
    # OAuth 风格的回调（原始用例）传递 None；
    # 调用者在 Responses API 中执行的外部工具调用传递描述工具输出的 JSON 对象。
    # 在线路上默认为 None，这样现有的 OAuth 调用者无需更新。
    payload: Optional[Any] = None

@dataclass
class GateAuthResolution:
    """
    使用精确的请求 ID 解决认证门控。
    """
    # 被解决的待处理认证门控的 ID。
    request_id: str
    # 该门控的凭据提交或取消操作。  TODO
    resolution: "AuthGateResolution"  # 前向引用 AuthGateResolution 类


@dataclass
class ApprovalResponse:
    """
    对当前待处理审批的简单审批响应（是/否/始终）。
    """
    # 执行是否被批准。
    approved: bool
    # 如果为 True，则在此会话的剩余时间内自动批准此工具。
    always: bool


# ---------- 简单变体（无字段） ----------
class Interrupt:
    """中断当前回合。"""
    pass


class Compact:
    """请求上下文压缩。"""
    pass


class Undo:
    """撤销上一个回合。"""
    pass


class Redo:
    """重做之前撤销的回合（如果可用）。"""
    pass


class Clear:
    """清除当前线程并重新开始。"""
    pass


class NewThread:
    """创建一个新线程。"""
    pass


class ListThreads:
    """列出线程以供交互式恢复选择器使用。"""
    pass


class Heartbeat:
    """触发手动心跳检查。"""
    pass


class Summarize:
    """总结当前线程。"""
    pass


class Suggest:
    """基于当前线程建议后续步骤。"""
    pass


class Quit:
    """退出代理。绕过线程状态检查。"""
    pass


# ---------- 带字段的变体 ----------
@dataclass
class Resume:
    """从特定检查点恢复。
    """
    # 要恢复的检查点的 ID。
    checkpoint_id: str


@dataclass
class SwitchThread:
    """切换到不同的线程。
    """
    # 要切换到的线程的 ID。
    thread_id: str


@dataclass
class Expected:
    """用户提供的对上次交互的预期行为。
    触发自我改进管道，并附加上下文信息。
    """
    # 用户期望发生什么。
    description: str


@dataclass
class JobStatus:
    """检查作业状态。不提供 job_id 则显示所有作业；提供 job_id 则显示特定作业。
    """
    # 可选的作业 ID（UUID 或短前缀）。如果为 None，则显示所有作业。
    job_id: Optional[str] = None


@dataclass
class JobCancel:
    """取消正在运行的作业。
    """
    # 作业 ID（UUID 或短前缀）。
    job_id: str


@dataclass
class SystemCommand:
    """系统命令（help、model、version、tools、ping、debug）。
    绕过线程状态检查和安全验证。
    """
    # 命令名称（例如 "help"、"model"、"version"）。
    command: str
    # 命令的参数。
    args: List[str] = field(default_factory=list)


@dataclass
class Plan:
    """计划模式命令（/plan）。
    所有子命令会被重写为带有 [PLAN MODE] 前缀的 UserInput，
    以激活计划模式技能。

    Attributes:
        sub: 计划子命令。
    """
    sub: "PlanSubcommand"  # 前向引用 PlanSubcommand 类


@dataclass
class PairingClaim:
    """从任何聊天界面认领配对码（例如 `approve telegram CODE`）。

    面向用户的配对流程告诉用户在任何 IronClaw 聊天中准确输入此内容。
    处理程序委托给与 `POST /api/pairing/{channel}/approve` 使用的
    相同的配对存储和扩展管理器钩子，因此 TUI/CLI/web/telegram 各界面行为一致。
    """
    # 频道名称（例如 `telegram`、`slack-relay`）。
    channel: str
    # 配对码，由 `SubmissionParser::parse` 转为小写。
    # 配对存储不区分大小写，因此线路格式的大写形式仍然匹配。
    code: str


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
