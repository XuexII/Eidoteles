# Missions——长期运行的目标，会随时间生成线程。
#
# 任务代表一个持续进行的目标，它会定期生成线程以推进工作。
# 任务可以按计划（cron）运行、响应事件触发或手动触发。
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Dict

from engine.gate import ResumeKind
from ironclaw_common import ValidTimezone
from .project import ProjectId
from .thread import ThreadId
from ..types import OwnerId

logger = logging.getLogger(__name__)


# ── 任务标识符 ───────────────────────────────────────────────

@dataclass(frozen=True)
class MissionId:
    """强类型任务标识符"""
    value: uuid.UUID = field(default_factory=uuid.uuid4)

    def __str__(self) -> str:
        return str(self.value)


# ── 任务状态 ─────────────────────────────────────────────────

class MissionStatus(Enum):
    """任务的生命周期状态"""
    # 任务正在按节奏主动生成线程
    Active = "Active"
    # 任务已暂停 — 不会生成新线程
    Paused = "Paused"
    # 任务已达到目标
    Completed = "Completed"
    # 任务在线程终止失败后停止。
    # 自动/手动触发被阻止，直到所有者在修复根本问题后显式恢复它
    Failed = "Failed"


# ── 任务节奏 ─────────────────────────────────────────────────

class MissionCadence:
    """任务如何触发新线程

    引擎定义触发*类型*。桥接/主机实现实际的触发基础设施
    （cron 计时器、webhook 端点、事件匹配器）。引擎只需要被告知"现在触发此任务"
    """
    pass


@dataclass
class Cron(MissionCadence):
    """按 cron 计划生成（例如 "0 */6 * * *" 表示每 6 小时）"""
    expression: str
    timezone: Optional[ValidTimezone] = None


@dataclass
class OnEvent(MissionCadence):
    """响应匹配正则表达式模式的频道消息生成。
    `channel` 设置后，将触发限制为来自特定频道名称（不区分大小写）的消息
    """
    event_pattern: str
    channel: Optional[str] = None


@dataclass
class OnSystemEvent(MissionCadence):
    """响应结构化系统事件生成（来自工具或外部）。
    `filters` 非空时，要求每个键/值对精确匹配事件负载的顶级字段
    """
    source: str
    event_type: str
    filters: Dict[str, any] = field(default_factory=dict)


@dataclass
class Webhook(MissionCadence):
    """在注册路径收到外部 webhook 时生成。
    桥接注册 webhook 端点并将负载路由到这里
    """
    path: str
    secret: Optional[str] = None


@dataclass
class Manual(MissionCadence):
    """仅在手动触发时生成（通过 mission_fire 工具或 API）"""
    pass


# ── 任务门控信息 ─────────────────────────────────────────────

@dataclass
class MissionGateInfo:
    """子线程在未解决的门控（认证、批准或外部回调）上暂停的任务的持久化门控元数据

    携带两个标识符因为它们服务于不同的消费者：

    - `call_id` — 引擎端 LLM 工具调用 ID（例如 `call_657a9167...`）。
      引擎内部用于恢复。
    - `gate_request_id` — 向面向用户的认证托盘和
      `/api/chat/gate/resolve` 处理器展示的 UUID。处理器对该字段
      进行 `Uuid::parse_str` 解析，因此它必须是 UUID，而非引擎 call_id

    同时用于 #3133 的 half-1（`MissionNotification.gate`）和 half-2（`Mission.paused_gate`）
    """
    # 引擎端门控名称（例如 `auth_required`、`approval_required`）
    gate_name: str
    # 暂停的动作（例如 `gmail`、`http`、`slack_send`）
    action_name: str
    # 暂停时的原始动作参数
    parameters: dict
    # 引擎端 LLM 工具调用 ID（例如 `call_657a9167...`）
    call_id: str
    # 新生成的 UUID，向用户标识此展示的门控
    gate_request_id: str
    # 解除此门控阻塞的恢复类型
    resume_kind: ResumeKind


# ── 任务 ─────────────────────────────────────────────────────

@dataclass(kw_only=True)
class Mission:
    """任务 — 一个长期运行的目标，随时间推移生成线程"""
    id: MissionId = field(default_factory=MissionId)
    project_id: ProjectId
    # 租户隔离：拥有此任务的用户
    user_id: str
    name: str
    goal: str
    status: MissionStatus = MissionStatus.Active
    cadence: MissionCadence
    # 可选的人类可读描述（与目标声明分开）。
    # 例程 `description` 字段映射到这里
    description: Optional[str] = None

    # ── 演进策略 ──
    # 下一个线程应关注什么（在每个线程后更新）
    current_focus: Optional[str] = None
    # 已尝试过哪些方法以及发生了什么
    approach_history: List[str] = field(default_factory=list)

    # ── 进度跟踪 ──
    # 此任务生成的线程历史
    thread_history: List[ThreadId] = field(default_factory=list)
    # 声明任务完成的可选标准
    success_criteria: Optional[str] = None

    # ── 通知 ──
    # 任务线程完成时要通知的频道（例如 "gateway"、"repl"）。
    # 空意味着不主动通知（结果仅在 approach_history 中）
    notify_channels: List[str] = field(default_factory=list)
    # 通知的可选每频道用户/接收者目标。映射自例程 `delivery.user`。
    # 为 None 时，使用频道的最后已知接收者
    notify_user: Optional[str] = None

    # ── 上下文预加载 ──
    # 任务触发时将其内容加载到线程元提示中的工作区路径
    # （例如 `["MEMORY.md", "context/profile.json"]`）。
    # 映射自例程 `execution.context_paths`
    context_paths: List[str] = field(default_factory=list)

    # ── 预算/护栏 ──
    # 每天最大线程数（0 = 无限制）
    max_threads_per_day: int = 10
    # 今天已生成的线程数（每天由 cron 计时器重置）
    threads_today: int = 0
    # 两次触发之间的冷却时间，以秒为单位。0 = 无冷却。
    # 映射自例程 `guardrails.cooldown_secs`
    cooldown_secs: int = 0
    # 可同时运行（处于非终止状态）的最大任务线程数。
    # 0 = 无限制。映射自例程 `guardrails.max_concurrent`
    max_concurrent: int = 0
    # 事件触发触发的去重窗口，以秒为单位。0 = 不去重。
    # 设置后，此窗口内相同的事件键负载将被抑制。
    # 映射自例程 `guardrails.dedup_window`
    dedup_window_secs: int = 0
    # 最近一次成功触发的时间戳。由冷却执行机制使用
    last_fire_at: Optional[datetime] = None

    # ── 触发负载 ──
    # 最近一次触发的负载（webhook 正文、事件数据等）。
    # 注入到线程上下文中以便代码可以访问它
    last_trigger_payload: Optional[dict] = None

    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # 下一个线程应何时生成（用于 Cron 节奏）
    next_fire_at: Optional[datetime] = None

    # 当任务的子线程在未解决的门控上暂停时，
    # 此处记录门控元数据，以便凭证写入/门控解析路径
    # 可以将暂停的任务匹配回其等待的门控并自动恢复它
    # （#3166 / #3133 的 half-2）。
    # 当任务恢复（手动或自动）或成功触发时清除
    paused_gate: Optional[MissionGateInfo] = None

    def __post_init__(self):
        if isinstance(self.cadence, (OnEvent, OnSystemEvent, Webhook)):
            self.max_threads_per_day = 24
            self.cooldown_secs = 300
            self.max_concurrent = 1

    def with_success_criteria(self, criteria: str) -> "Mission":
        """设置成功标准并返回 self 以支持链式调用"""
        self.success_criteria = criteria
        return self

    @property
    def owner_id(self) -> OwnerId:
        """获取任务所有者的 ID"""
        return OwnerId.from_user_id(self.user_id)

    def is_owned_by(self, user_id: str) -> bool:
        """检查任务是否属于指定用户"""
        return self.owner_id().matches_user(user_id)

    def record_thread(self, thread_id: ThreadId) -> None:
        """记录为此任务生成了一个线程"""
        self.thread_history.append(thread_id)
        self.updated_at = datetime.now(timezone.utc)

    def is_terminal(self) -> bool:
        """任务是否处于终止状态"""
        return self.status in (MissionStatus.Completed, MissionStatus.Failed)

    def is_event_driven(self) -> bool:
        """任务是否是事件驱动的（响应外部刺激触发，而非固定计划）。
        事件驱动的任务在完成后可能合法地重新触发 — 每个事件都是新的调查
        """
        return isinstance(self.cadence, (OnSystemEvent, OnEvent, Webhook))


# ── Cron 辅助函数 ────────────────────────────────────────────

def normalize_cron_expression(expression: str) -> str:
    """将 cron 表达式规范化为 cron 库期望的 7 字段格式

    接受的字段格式：
    - **5 字段**（标准 Vixie cron）：`min hr dom mon dow` — 前置 `0`（秒）并追加 `*`（年）
    - **6 字段**：假定为 `sec min hr dom mon dow`（cron 库的原生格式减去年）并追加 `*`（年）。
      **注意：** 这*不是* Quartz 的 `min hr dom mon dow year` 解释。用户传入
      `"0 9 * * * 2027"` 意图表示"2027 年每天 09:00"将反而得到
      "在每年每月每天每小时的第 9 分钟的第 0 秒"。使用显式的 7 字段格式
      `0 0 9 * * * 2027` 来消除歧义
    - **7 字段**：`sec min hr dom mon dow year` — 原样传递

    对于任何其他字段数量返回错误，而不是将输入传递给 `cron::Schedule::from_str`，
    后者会显示令人困惑的低级解析错误
    """
    trimmed = expression.strip()
    fields = trimmed.split()

    if len(fields) == 5:
        return f"0 {' '.join(fields)} *"
    elif len(fields) == 6:
        # 消除 Quartz 风格 6 字段格式的歧义。用户（或 LLM）
        # 输入 `"0 9 * * * 2027"` 几乎肯定意味着
        # "2027 年每天 09:00"（Quartz: `min hr dom mon dow year`），
        # 而不是"在每年每月每天每小时的第 9 分钟的第 0 秒，dow=2027"。
        # cron 库会将年份形状的最后一个字段视为（无意义的）星期几
        # 并静默地产生错误的计划。
        # 提前拒绝并显示指向显式 7 字段格式的消息，
        # 这样调用者可以修复它，而不是调试一个永远不会触发的计划
        if is_year_field(fields[-1]):
            last = fields[-1]
            raise RuntimeError(f"InvalidCadence: 模糊的 6 字段 cron 表达式 '{expression}': "
                               f"末尾的 '{last}' 看起来像年份。6 字段格式是 `sec min hr dom mon dow`，"
                               f"而不是 Quartz 的 `min hr dom mon dow year`。"
                               f"使用显式的 7 字段格式 `0 {fields[0]} {fields[1]} {fields[2]} {fields[3]} {fields[4]} {last}` "
                               f"来表示 '在 {last} 年的给定时间'")
        return f"{' '.join(fields)} *"
    elif len(fields) == 7:
        return trimmed
    else:
        raise RuntimeError(f"InvalidCadence: 无效的 cron 表达式 '{expression}': "
                           f"期望 5、6 或 7 个字段，但得到 {len(fields)} 个")


def is_year_field(field: str) -> bool:
    """如果 `field` 是合理 cron 范围内的字面 4 位数字年份，则返回 True

    用于检测 6 字段输入中的 Quartz 风格 `min hr dom mon dow year` 错误。
    选择的范围涵盖 cron 库接受的年份范围，而不会触发恰好是 4 位数字但表示其他含义的字段值
    （标准 cron 字段范围都不会产生 4 位数字字面量）
    """
    if len(field) != 4:
        return False
    if not field.isdigit():
        return False
    year = int(field)
    return 1970 <= year <= 2099


def next_cron_fire(
        expression: str,
        timezone: Optional[ValidTimezone] = None,
) -> Optional[datetime]:
    """解析 cron 表达式并从现在开始计算下一个触发时间

    接受标准的 5 字段、6 字段或 7 字段 cron 表达式（自动规范化）。
    当提供 [`ValidTimezone`] 时，计划在该时区中评估，
    结果转换回 UTC。否则使用 UTC

    Cron 解析失败返回 [`RuntimeError::InvalidCadence`]（验证，而非存储），
    因此调用者可以将其映射为面向用户的错误
    """
    from croniter import croniter

    normalized = normalize_cron_expression(expression)
    now = datetime.now(timezone.utc)

    try:
        if timezone is not None:
            # 在指定时区中计算
            tz = timezone.tz()
            local_now = now.astimezone(tz)
            cron = croniter(normalized, local_now)
            next_time = cron.get_next(datetime)
            # 转换回 UTC
            return next_time.astimezone(timezone.utc)
        else:
            cron = croniter(normalized, now)
            return cron.get_next(datetime)
    except Exception as e:
        raise RuntimeError(f"InvalidCadence: 无效的 cron 表达式 '{expression}': {e}")


def next_cron_fire_required(
        expression: str,
        timezone: Optional[ValidTimezone] = None,
) -> datetime:
    """类似于 [`next_cron_fire`]，但将 `Ok(None)` 视为验证错误

    `next_cron_fire` 对于语法有效但永远不会再触发的 cron 表达式
    （例如 `0 0 9 * * * 2020` — 年份锁定到已过去的年份）返回 `Ok(None)`。
    在生命周期入口点（`create_mission`、节奏更新、`resume_mission`），
    这与原始 #1944 错误是相同的失败模式：一个 Active 任务带有
    `next_fire_at = None`，计时器永远无法拾取它。
    将其显示为 `InvalidCadence`，这样调用者可以快速失败，操作员得到明确的错误

    `fire_mission` 和 `bootstrap_project` 有意容忍 `Ok(None)`（记录日志）
    并应继续直接使用 `next_cron_fire` — 线程已经在运行或数据已经持久化，
    中止比记录日志会造成更多伤害
    """
    result = next_cron_fire(expression, timezone)
    if result is None:
        raise RuntimeError(
            f"InvalidCadence: cron 表达式 '{expression}' 没有即将到来的触发时间 "
            f"（年份锁定或其他原因导致无法调度）"
        )
    return result
