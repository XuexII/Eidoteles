from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


# ── 门控决策 ───────────────────────────────────────────

class GateDecision:
    """执行门控的评估结果。"""
    pass


@dataclass
class GateDecisionAllow(GateDecision):
    """允许执行继续。"""
    pass


@dataclass
class GateDecisionPause(GateDecision):
    """执行必须暂停，直到用户提供输入。"""
    reason: str
    resume_kind: "ResumeKind"


@dataclass
class GateDecisionDeny(GateDecision):
    """执行被直接拒绝。"""
    reason: str


# ── 恢复类型 ───────────────────────────────────────────

class ResumeKind:
    """将解析暂停门控的外部输入类型。"""
    pass


@dataclass
class ResumeKindApproval(ResumeKind):
    """用户必须批准或拒绝工具调用。"""
    # 是否应提供"始终批准此工具"选项
    allow_always: bool


@dataclass
class ResumeKindAuthentication(ResumeKind):
    """用户必须提供凭证（令牌、API 密钥、OAuth 流）。"""
    # 缺失的凭证名称
    credential_name: CredentialName
    # 面向用户的设置说明
    instructions: str
    # 可选的基于浏览器的流的 OAuth URL
    auth_url: Optional[str] = None


@dataclass
class ResumeKindExternal(ResumeKind):
    """外部系统必须响应（webhook 确认等）。"""
    callback_id: str


def resume_kind_name(kind: ResumeKind) -> str:
    """此类型的简短人类可读标签。"""
    if isinstance(kind, ResumeKindApproval):
        return "approval"
    elif isinstance(kind, ResumeKindAuthentication):
        return "authentication"
    elif isinstance(kind, ResumeKindExternal):
        return "external confirmation"
    return "unknown"


# ── 门控解析 ───────────────────────────────────────────

class GateResolution:
    """用户或外部系统如何解析暂停的门控。"""
    pass


@dataclass
class GateResolutionApproved(GateResolution):
    """用户批准了工具调用。"""
    always: bool


@dataclass
class GateResolutionDenied(GateResolution):
    """用户拒绝了工具调用。"""
    reason: Optional[str] = None


@dataclass
class GateResolutionCredentialProvided(GateResolution):
    """用户提供了凭证值。"""
    token: str


@dataclass
class GateResolutionCancelled(GateResolution):
    """用户或系统完全取消了待处理门控。"""
    pass


@dataclass
class GateResolutionExternalCallback(GateResolution):
    """收到了外部回调。"""
    payload: Dict[str, Any]


# ── 执行模式 ──────────────────────────────────────────

class ExecutionMode(Enum):
    """评估工具调用的执行上下文。"""
    # 交互式会话——用户可以批准/认证
    Interactive = "interactive"
    # 启用了自动批准的交互式会话
    #
    # `UnlessAutoApproved` 工具无需提示即可通过（shell、file_write、
    # http 等）。`Always` 门控工具（破坏性操作）仍然暂停等待显式批准。
    # 所有其他安全措施仍然有效：租约、速率限制、钩子、
    # 中继通道检查、认证门控。
    #
    # 通过 `AGENT_AUTO_APPROVE_TOOLS=true` 或设置激活。
    InteractiveAutoApprove = "interactive_auto_approve"
    # 自主后台作业——无交互用户。
    # 租约集合确定哪些工具可用。
    Autonomous = "autonomous"
    # 容器沙箱执行
    Container = "container"


# ── 门控上下文 ──────────────────────────────────────────

@dataclass
class GateContext:
    """
    门控做出决策所需的一切的不可变快照。

    字符串和 Value 字段被借用，以避免在热路径中克隆。
    `ThreadId` 和 `ExecutionMode` 是 `Copy` 类型并内联存储。
    """
    user_id: str
    thread_id: ThreadId
    source_channel: str
    action_name: str
    call_id: str
    parameters: Dict[str, Any]
    action_def: ActionDef
    execution_mode: ExecutionMode
    # 会话已自动批准的工具（"始终"按钮）
    auto_approved: Set[str]


# ── 门控 trait ───────────────────────────────────────────────

class ExecutionGate:
    """
    单个预执行检查。

    实现必须对给定的上下文快照是确定性的：
    它们不能持有在单次管道运行中跨评估变化的可变状态。
    """

    def name(self) -> str:
        """用于日志记录和持久化的唯一名称。"""
        raise NotImplementedError

    def priority(self) -> int:
        """评估优先级。较低的先运行。第一个 `Pause` 或 `Deny` 获胜。"""
        raise NotImplementedError

    async def evaluate(self, ctx: GateContext) -> GateDecision:
        """评估工具调用是否应继续。"""
        raise NotImplementedError


# ── 内联门控等待 ────────────────────────────────────────

@dataclass
class GatePauseRequest:
    """
    当 `Approval` 门控在活动执行中触发时，执行器需要向用户展示的内容。

    [`GateController`] 的主机实现负责：
    1. 持久化 UI / 通道层渲染批准提示所需的任何元数据。
    2. 将提示分派到发起通道。
    3. 等待用户的响应并将其作为 [`GateResolution`] 返回，而不重新进入引擎。

    携带 `thread_id` 和 `user_id`，以便单个共享控制器可以将请求路由到
    正确的主机端每执行上下文（对话 id、通道元数据等），
    而无需引擎将桥接内部类型线程化通过。
    """
    thread_id: ThreadId
    user_id: str
    gate_name: str
    action_name: str
    call_id: str
    parameters: Dict[str, Any]
    resume_kind: ResumeKind
    # 发起对话（如果有）。让主机在同一个用户有多个并发对话时
    # （例如两个浏览器标签页）将内联门控路由到正确的 UI 表面。
    # 对于后台任务线程为 `None`。
    conversation_id: Optional[ConversationId] = None


class GateController:
    """
    主机提供的回调，暂停活动引擎执行，直到用户解析 `Approval` 门控。

    这是让 Tier 0（结构化）和 Tier 1（CodeAct/Monty）执行在**不**展开调用栈的
    情况下等待用户输入的机制。执行器在其自己的循环内保持等待 `pause()`，
    因此所有内存状态（Monty VM 帧、部分执行的并行批处理、租约）在等待期间被保留。
    解析后，执行器内联继续——无需线程重新进入、无需重放、
    无需双重执行同一步骤中先前工具调用的副作用。

    处理 `ResumeKind::Approval` 和 `ResumeKind::Authentication`。
    外部恢复类型仍然保留基于遗留重新进入的流：它们的解析安装回调负载状态，
    无法在不展开的情况下返回给暂停的调用。
    """

    async def pause(self, request: GatePauseRequest) -> GateResolution:
        """
        暂停执行，直到用户解析门控。

        实现必须最终返回某个 [`GateResolution`]
        （在关闭/超时时返回 `Cancelled` 是可以接受的）。
        它们不能永远阻塞——调用者依赖此 future 完成，
        以便周围的执行可以继续或干净地终止。
        """
        raise NotImplementedError

    async def cancel_thread(self, thread_id: ThreadId) -> None:
        """
        用 [`GateResolution::Cancelled`] 唤醒当前停在 `thread_id` 上的
        任何 [`pause`] future，并丢弃其待处理状态。

        `ThreadManager::stop_thread()` 在发送 `ThreadSignal::Stop` 之前调用此方法。
        没有它，停在 `pause()` 内部的引擎任务不会轮询线程信号通道，
        将在观察停止请求之前继续等待用户（或直到主机的门控过期窗口）——
        使运行中的任务和待处理提示成为孤立状态。

        默认实现为空操作；覆盖应是幂等的，并能容忍并发调用。
        不跟踪每线程等待者的实现可以忽略此调用。
        """
        pass


class CancellingGateController(GateController):
    """
    取消每个暂停请求的默认 [`GateController`]。

    `ThreadExecutionContext::gate_controller` 是非可选的：每个执行上下文必须
    携带某个控制器。此实现是暂停无意义或已在上游解析的代码路径的替代品：

    - **解析后重放** (`execute_pending_gate_action`)——门控在此调用点运行之前已被解析。
    - **任务受保护写入**——没有发起用户通道来显示提示的后台路径。
    - **不执行门控流的测试**。

    返回 [`GateResolution::Cancelled`] 在 Tier 0 和 Tier 1 中都表现为类型化拒绝——
    永远不会作为原始的用户可见的"执行被门控暂停"错误消息。
    """

    async def pause(self, request: GatePauseRequest) -> GateResolution:
        return GateResolutionCancelled()