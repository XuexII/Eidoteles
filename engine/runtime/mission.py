# 任务管理器——编排长期运行的目标，这些目标会随时间生成线程。
#
# 任务跟踪持续进行的目标，并定期生成线程以推进工作。
# 管理器处理生命周期（创建、暂停、恢复、完成），并将线程生成委托给 [`ThreadManager`]。

import asyncio
import hashlib
import logging
import re
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, List, Dict, Any, Tuple

from skills.v2 import (CodeSnippet, SkillRepairRecord, SkillRepairType, V2SkillMetadata)

from engine.memory import (RetrievalEngine, SkillTracker)
from engine.runtime.manager import ThreadManager
from engine.traits.effect import EffectExecutor
from engine.traits.store import Store
from engine.types.error import EngineError
from engine.types.memory import (MemoryDoc)
from engine.types.mission import (
    Mission, MissionCadence, MissionGateInfo, MissionId, MissionStatus, next_cron_fire,
    next_cron_fire_required,
)
from engine.types.project import ProjectId
from engine.types.thread import (
    ThreadConfig, ThreadId, ThreadState, ThreadType,
)

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────

# 最大编译正则表达式大小，镜像 v1 例程引擎。
# 超过此限制的模式在编译时被拒绝，以防止恶意或有缺陷的任务
# 通过病态正则表达式固定匹配器
MAX_EVENT_REGEX_SIZE = 64 * 1024

# ── 事件正则表达式缓存 ──────────────────────────────────────

# 每个任务编译的正则表达式缓存。我们在首次匹配尝试时惰性编译模式，
# 并在任务更新或删除其节奏时丢弃它们。缓存是进程本地的 —
# 重启时按需重新填充。
# 类型：Dict[MissionId, re.Pattern]
EventRegexCache = Dict[str, "re.Pattern"]


# ── 触发速率限制 ─────────────────────────────────────────────

@dataclass
class FireRateLimit:
    """
    每用户触发速率上限，表示为令牌桶。独立于每个任务的 `cooldown_secs`，
    这是用户所有任务上的*全局*上限，因此拥有许多事件触发任务的用户
    不能集体淹没 LLM
    """
    # 每用户每小时 100 次任务触发。足够宽松，正常 cron +
    # 少量事件驱动的任务不会注意到它；足够紧凑，行为异常的模式受到限制

    # 在 `window` 内允许的最大触发次数
    max_fires: int = 100
    # 滑动窗口持续时间。超过此时间的触发被驱逐
    window: timedelta = timedelta(seconds=3600)


# ── 预算门控 ─────────────────────────────────────────────────

class BudgetGate(ABC):
    """引擎侧预算抽象。实现决定 `user_id` 是否仍有足够的 LLM/财务预算
    来生成另一个任务线程。主机在其现有的 `CostGuard` 上实现此功能

    当 `MissionManager` 没有附加 `BudgetGate` 时，所有触发都被允许
    （对于不使用预算的嵌入器的向后兼容）
    """

    @abstractmethod
    async def allow_mission_fire(self, user_id: str, mission_id: MissionId) -> bool:
        """如果允许 `user_id` 触发任务则返回 `True`。包含 `mission_id`
        以便适配器可以根据需要应用每个任务的策略；
        大多数实现将仅参考 `user_id`
        """
        ...


# ── 任务通知 ─────────────────────────────────────────────────

@dataclass
class MissionNotification:
    """任务线程完成时发出的通知

    桥接订阅这些通知，并通过 `ChannelManager::broadcast()` 将响应文本
    路由到任务的 `notify_channels`
    """
    mission_id: MissionId
    mission_name: str
    thread_id: ThreadId
    user_id: str
    # 要通知的频道（来自 `Mission.notify_channels`）
    notify_channels: List[str] = field(default_factory=list)
    # 可选的每频道接收者（来自 `Mission.notify_user`）。
    # 为 None 时，使用频道的默认接收者
    notify_user: Optional[str] = None
    # 线程的响应文本（如果失败/无输出则为 None）
    response: Optional[str] = None
    # 如果线程失败则为 True
    is_error: bool = False
    # 当任务的子线程在未解决的门控（认证、批准或外部回调）上暂停而不是完成时，
    # 桥接将其转换为频道侧的 `AuthRequired` / `ApprovalNeeded` 状态，
    # 以便用户的认证托盘显示提示。对于正常完成和失败为 None
    #
    # 由 issue #3133 固定：以前 `process_mission_outcome_and_notify` 中的
    # `_ => {}` 分支吞掉了 `GatePaused` 结果，使用户没有可操作的信号，
    # cron 调度器每个周期重新触发相同的损坏任务
    gate: Optional[MissionGateInfo] = None


# ── 门控解决方案结果 ─────────────────────────────────────────

class GateResolutionOutcome(Enum):
    """门控解决方案的结果，就任务自动恢复路径而言。
    Approved → 恢复 + 可能立即触发。Denied 或 Cancelled → 转换为 Failed，
    以便用户必须显式修复并恢复

    保持为小型独立枚举（而不是重用 [`GateResolution`]），
    因为自动恢复路径不需要解决方案负载（令牌、回调正文、允许始终标志）—
    只需要成功/拒绝的结果
    """
    # 用户批准了门控 — 恢复任务
    Approved = "Approved"
    # 用户拒绝了门控 — 标记任务为 Failed
    Denied = "Denied"
    # 门控被取消（超时、关闭、显式取消）— 与 Denied 相同的终端结果
    Cancelled = "Cancelled"


# ── 任务更新 ─────────────────────────────────────────────────

@dataclass
class MissionUpdate:
    """通过 [`MissionManager::update_mission`] 应用于任务的可选更新"""
    name: Optional[str] = None
    description: Optional[str] = None
    goal: Optional[str] = None
    cadence: Optional[MissionCadence] = None
    notify_channels: Optional[List[str]] = None
    notify_user: Optional[str] = None
    context_paths: Optional[List[str]] = None
    max_threads_per_day: Optional[int] = None
    success_criteria: Optional[str] = None
    cooldown_secs: Optional[int] = None
    max_concurrent: Optional[int] = None
    dedup_window_secs: Optional[int] = None


# ── 去重键 ───────────────────────────────────────────────────

# 事件触发任务的内存去重状态。键为 (mission_id, dedup-key) → 上次触发时间戳
# 类型：Tuple[MissionId, str]
DedupKey = Tuple[MissionId, str]


# ── 每日线程计数过时检查 ─────────────────────────────────────

def threads_today_is_stale(mission: Mission, now: datetime) -> bool:
    """如果 `mission.threads_today` 是从前一个日历日延续的，并且在每日预算门控之前
    必须重置，则返回 true

    日期边界是 cron 任务的配置时区（如果设置），否则为 UTC。
    具有 `cron timezone = "America/Los_Angeles"` 的用户期望预算在洛杉矶午夜刷新；
    UTC 重置会使该任务在其本地日期的许多小时内闲置
    （UTC 午夜落在前一个下午/晚上当地时间，确切时间在夏令时下变化）

    `last_fire_at = None` 且具有非零计数器被视为过时 —
    这两个字段由 `fire_mission` 一起写入，因此此状态仅来自数据损坏/迁移/带外编辑，
    重置是恢复方向。在此处返回 false 将重新引入此辅助函数存在以修复的永久耗尽错误

    `now` 被注入，以便调用站点可以在过时检查和下游触发记账之间固定单个时刻
    （避免午夜边界竞态），并且单元测试可以根据固定的合成时间戳断言时区边界
    """
    if mission.threads_today == 0:
        return False

    last = mission.last_fire_at
    if last is None:
        return True

    # 检查是否有 Cron 节奏并配置了时区
    if isinstance(mission.cadence, Cron) and mission.cadence.timezone is not None:
        tz = mission.cadence.timezone
        # 将两个时间都转换到任务配置的时区，比较日期
        last_in_tz = last.astimezone(tz) if last.tzinfo is None else last
        now_in_tz = now.astimezone(tz) if now.tzinfo is None else now
        return last_in_tz.date() < now_in_tz.date()

    # 默认使用 UTC 日期比较
    last_date = last.date() if hasattr(last, 'date') else last
    now_date = now.date() if hasattr(now, 'date') else now
    return last_date < now_date


# ── 常量 ─────────────────────────────────────────────────────

# 同一任务 ID 的连续 `fire_mission` 尝试之间的最小间隔，
# 由 `tick` 在内存中强制执行。选择为舒适地超过 60 秒的 tick 间隔，
# 以便始终尊重单个 tick 间隔，同时在存储恢复时允许在几分钟内恢复
FIRE_COOLDOWN_SECS = 90


# ── 任务管理器 ───────────────────────────────────────────────
@dataclass
class MissionManager:
    """管理任务生命周期和线程生成"""

    store: Store
    thread_manager: ThreadManager
    # 用于通过宿主工具管道分发动作的效果执行器（包括批准门控）。
    # 由任务后处理使用，通过与线程内工具调用相同的批准路径来路由受保护的写入操作。
    effects: Optional[EffectExecutor] = field(default=None, init=False)
    # 按 ID 索引的活动任务，以便快速查找。
    active: List[MissionId] = field(default_factory=list, init=False)
    # 用于任务结果通知的广播通道。
    notification_tx: asyncio.Queue = field(default=asyncio.Queue(maxsize=64), init=False)
    # 每个任务的进程内冷却时间戳，在每次 `fire_mission` 尝试后记录，无论 `save_mission` 是否成功。
    #
    # `tick` 会参考此时间戳，在短暂的存储故障阻止了 `next_fire_at` / `threads_today` 在持久化记录中推进的情况下，抑制在 [`FIRE_COOLDOWN`] 内重新触发同一任务。
    # 如果没有此保护，成功触发后若保存失败，将导致下一个 60 秒的 tick（以及之后的每个 tick）重新触发同一任务，直到存储恢复，从而在每日预算内产生重复线程。
    last_fire_attempt: Dict[MissionId, datetime] = field(default_factory=dict, init=False)
    # /// 可选的用于在触发时加载 `Mission.context_paths` 的工作区读取器。
    #     /// 当为 `None` 时，上下文预加载会被静默跳过。
    workspace: Optional[Any] = field(default=None, init=False)
    # /// 每个任务的去重表，用于事件触发的触发。当条目超出去重窗口时，会机会性地清除。
    dedup_table: Dict[DedupKey, datetime] = field(default_factory=dict, init=False)
    # /// `OnEvent` 任务模式的编译正则表达式缓存。在首次匹配尝试时惰性填充；条目在任务更新/删除时被淘汰。
    event_regex_cache: Dict = field(default=EventRegexCache, init=False)
    # /// 全局速率限制器使用的按用户滑动窗口触发日志。
    #     /// 每个 `VecDeque` 保存配置窗口内的触发时间戳。
    user_fire_log: Dict[str, deque] = field(default_factory=dict, init=False)
    # 全局按用户的触发频率上限。
    rate_limit: FireRateLimit = field(default_factory=FireRateLimit, init=False)
    # 可选的预算门控，在每次触发前会被咨询。
    budget_gate: Optional[BudgetGate] = field(default=None, init=False)
    # 对话洞察提取间隔（每完成 N 个线程执行一次）。
    insights_interval: int = field(default=5, init=False)
    _lock: asyncio.Lock = asyncio.Lock()

    def with_workspace_reader(self, reader: Any) -> "MissionManager":
        """附加工作区读取器，以便在触发时加载 `context_paths`"""
        self.workspace = reader
        return self

    def with_budget_gate(self, gate: BudgetGate) -> "MissionManager":
        """附加预算门控，以便每次触发参考主机的支出限制"""
        self.budget_gate = gate
        return self

    def with_effect_executor(self, effects: Any) -> "MissionManager":
        """附加效果执行器，以便任务后处理通过主机的工具管道路由受保护的写入"""
        self.effects = effects
        return self

    def with_rate_limit(self, limit: FireRateLimit) -> "MissionManager":
        """覆盖每用户触发速率限制。默认 100 次/小时"""
        self.rate_limit = limit
        return self

    def with_insights_interval(self, interval: int) -> "MissionManager":
        """覆盖对话洞察提取间隔。每 N 个完成的线程，从对话中提取洞察"""
        self.insights_interval = max(interval, 1)
        return self

    def subscribe_notifications(self) -> asyncio.Queue:
        """订阅任务结果通知。桥接使用此将任务结果路由到频道"""
        return self.notification_tx

    async def bootstrap_project(self, project_id: ProjectId) -> int:
        """从持久化的任务状态填充活跃任务索引

        同时为在调度修复之前创建的活跃 cron 任务回填 `next_fire_at` —
        没有此步骤，旧版 cron 任务将保持 `next_fire_at = None` 并永远不会触发
        """
        missions = await self.store.list_all_missions(project_id)
        active_ids = []

        for mission in missions:
            if mission.status != MissionStatus.Active:
                continue

            # 为在调度修复之前的 cron 任务回填 next_fire_at
            if (isinstance(mission.cadence, Cron)
                    and mission.next_fire_at is None):
                expression = mission.cadence.expression
                timezone = mission.cadence.timezone
                try:
                    next_fire = next_cron_fire(expression, timezone)
                    if next_fire is not None:
                        fresh = await self.store.load_mission(mission.id)
                        if fresh is not None and fresh.next_fire_at is None:
                            fresh.next_fire_at = next_fire
                            try:
                                await self.store.save_mission(fresh)
                                logger.debug(
                                    f"为旧版 cron 任务回填 next_fire_at: "
                                    f"mission_id={mission.id}, next={next_fire}"
                                )
                            except Exception as e:
                                logger.debug(
                                    f"持久化 next_fire_at 回填失败；任务将在下次引导时重试: "
                                    f"mission_id={mission.id}, error={e}"
                                )
                except Exception as e:
                    logger.debug(
                        f"为旧版 cron 任务计算 next_fire_at 失败；保持 next_fire_at 未设置: "
                        f"mission_id={mission.id}, error={e}"
                    )

            active_ids.append(mission.id)

        count = len(active_ids)
        async with self._lock:
            self.active = active_ids
        logger.debug(f"已引导活跃任务: project_id={project_id}, active_missions={count}")
        return count

    async def create_mission(
            self,
            project_id: ProjectId,
            user_id: str,
            name: str,
            goal: str,
            cadence: MissionCadence,
            notify_channels: List[str],
    ) -> MissionId:
        """创建并持久化新任务。返回任务 ID"""
        mission = Mission.new(project_id, user_id, name, goal, cadence)
        if isinstance(mission.cadence, Cron):
            # 在创建边界拒绝 Ok(None) — 具有 `next_fire_at = None` 的 Active cron 任务是原始 #1944 失败模式
            mission.next_fire_at = next_cron_fire_required(
                mission.cadence.expression, mission.cadence.timezone
            )
        mission.notify_channels = notify_channels
        mid = mission.id
        await self.store.save_mission(mission)
        async with self._lock:
            self.active.append(mid)
        logger.debug(f"任务已创建: mission_id={mid}")
        return mid

    async def update_mission(
            self, id: MissionId, user_id: str, updates: MissionUpdate
    ) -> None:
        """更新任务的可变字段。仅应用非 None 字段"""
        mission = await self.store.load_mission(id)
        if mission is None:
            raise EngineError(f"Store: 未找到任务 {id}")

        allowed = (
            is_shared_owner(user_id) if mission.owner_id().is_shared()
            else mission.is_owned_by(user_id)
        )
        if not allowed:
            raise EngineError(f"AccessDenied: 用户 '{user_id}' 不能访问任务 {id}")

        if updates.name is not None:
            mission.name = updates.name
        if updates.description is not None:
            mission.description = updates.description
        if updates.goal is not None:
            mission.goal = updates.goal
        if updates.cadence is not None:
            mission.cadence = updates.cadence
            if isinstance(mission.cadence, Cron):
                mission.next_fire_at = next_cron_fire_required(
                    mission.cadence.expression, mission.cadence.timezone
                )
            else:
                mission.next_fire_at = None
        if updates.notify_channels is not None:
            mission.notify_channels = updates.notify_channels
        if updates.notify_user is not None:
            mission.notify_user = updates.notify_user
        if updates.context_paths is not None:
            mission.context_paths = updates.context_paths
        if updates.max_threads_per_day is not None:
            mission.max_threads_per_day = updates.max_threads_per_day
        if updates.success_criteria is not None:
            mission.success_criteria = updates.success_criteria
        if updates.cooldown_secs is not None:
            mission.cooldown_secs = updates.cooldown_secs
        if updates.max_concurrent is not None:
            mission.max_concurrent = updates.max_concurrent
        if updates.dedup_window_secs is not None:
            mission.dedup_window_secs = updates.dedup_window_secs

        mission.updated_at = datetime.now(timezone.utc)
        await self.store.save_mission(mission)
        # 节奏（因此 event_pattern）可能已更改。丢弃缓存的编译正则表达式；
        # 下次匹配尝试从当前模式重新编译
        await self.evict_event_regex(id)
        logger.debug(f"任务已更新: mission_id={id}")

    async def pause_mission(self, id: MissionId, user_id: str) -> None:
        """暂停活跃任务。不会生成新线程。共享任务只能由共享所有者（系统用户）管理"""
        mission = await self.store.load_mission(id)
        if mission is None:
            raise EngineError(f"Store: 未找到任务 {id}")

        allowed = (
            is_shared_owner(user_id) if mission.owner_id().is_shared()
            else mission.is_owned_by(user_id)
        )
        if not allowed:
            raise EngineError(f"AccessDenied: 用户 '{user_id}' 不能访问任务 {id}")

        await self.store.update_mission_status(id, MissionStatus.Paused)
        async with self._lock:
            self.active = [mid for mid in self.active if mid != id]
        self.last_fire_attempt.pop(id, None)
        logger.debug(f"任务已暂停: mission_id={id}")

    async def resume_mission(self, id: MissionId, user_id: str) -> None:
        """恢复暂停或失败的任务。共享任务只能由共享所有者（系统用户）管理"""
        mission = await self.store.load_mission(id)
        if mission is None:
            raise EngineError(f"Store: 未找到任务 {id}")

        allowed = (
            is_shared_owner(user_id) if mission.owner_id().is_shared()
            else mission.is_owned_by(user_id)
        )
        if not allowed:
            raise EngineError(f"AccessDenied: 用户 '{user_id}' 不能访问任务 {id}")

        if mission.status not in (MissionStatus.Paused, MissionStatus.Failed):
            raise EngineError(
                f"Store: 任务 {id} 处于状态 {mission.status}，只有 Paused 或 Failed 任务可以恢复"
            )

        mission.status = MissionStatus.Active
        mission.paused_gate = None
        if isinstance(mission.cadence, Cron):
            mission.next_fire_at = next_cron_fire_required(
                mission.cadence.expression, mission.cadence.timezone
            )
        mission.updated_at = datetime.now(timezone.utc)
        await self.store.save_mission(mission)

        async with self._lock:
            if id not in self.active:
                self.active.append(id)
        logger.debug(f"任务已恢复: mission_id={id}")

    async def complete_mission(self, id: MissionId) -> None:
        """将任务标记为已完成"""
        await self.store.update_mission_status(id, MissionStatus.Completed)
        async with self._lock:
            self.active = [mid for mid in self.active if mid != id]
        self.last_fire_attempt.pop(id, None)
        await self.evict_event_regex(id)
        logger.debug(f"任务已完成: mission_id={id}")

    async def fire_mission(
            self, id: MissionId, user_id: str, trigger_payload: Optional[dict] = None
    ) -> Optional[ThreadId]:
        """触发任务 — 构建元提示，生成线程，处理结果"""
        mission = await self.store.load_mission(id)
        if mission is None:
            raise EngineError(f"Store: 未找到任务 {id}")

        # 租户隔离：验证请求用户拥有此任务
        if not mission.owner_id().is_shared() and not mission.is_owned_by(user_id):
            raise EngineError(f"AccessDenied: 用户 '{user_id}' 不能访问任务 {id}")

        # 事件驱动的已完成任务仍可在新事件上触发
        if mission.is_terminal():
            allow = mission.status == MissionStatus.Completed and mission.is_event_driven()
            if not allow:
                logger.debug(f"无法触发已终止任务: mission_id={id}, status={mission.status}")
                return None

        now = datetime.now(timezone.utc)

        # 每日重置
        if threads_today_is_stale(mission, now):
            logger.debug(
                f"重置 threads_today — 新的一天: mission_id={id}, old_threads_today={mission.threads_today}"
            )
            mission.threads_today = 0
            try:
                await self.store.save_mission(mission)
            except Exception as e:
                logger.debug(
                    f"持久化每日重置失败；继续进行内存重置: mission_id={id}, error={e}"
                )

        # 检查每日预算
        if mission.max_threads_per_day > 0 and mission.threads_today >= mission.max_threads_per_day:
            logger.debug(f"每日线程预算已耗尽: mission_id={id}")
            return None

        # 冷却时间
        if mission.cooldown_secs > 0 and mission.last_fire_at is not None:
            elapsed = (now - mission.last_fire_at).total_seconds()
            if 0 <= elapsed < mission.cooldown_secs:
                logger.debug(
                    f"任务冷却时间尚未过去: mission_id={id}, elapsed_secs={elapsed}, cooldown_secs={mission.cooldown_secs}"
                )
                return None

        # max_concurrent
        if mission.max_concurrent > 0:
            running = await self.count_running_threads(mission)
            if running >= mission.max_concurrent:
                logger.debug(
                    f"任务 max_concurrent 已达到: mission_id={id}, running={running}, max_concurrent={mission.max_concurrent}"
                )
                return None

        # 每用户全局速率限制
        if not await self.check_user_rate(mission.user_id):
            logger.debug(
                f"每用户任务触发速率限制已达到: mission_id={id}, user_id={mission.user_id}"
            )
            return None

        # 预算门控
        if not await self.budget_allows(mission.user_id, id):
            logger.debug(f"任务触发被预算门控拒绝: mission_id={id}, user_id={mission.user_id}")
            return None

        # 加载 context_paths
        context_blocks = []
        if self.workspace is not None:
            for path in mission.context_paths:
                try:
                    content = await self.workspace.read_doc(path)
                    context_blocks.append((path, content))
                except Exception as error:
                    logger.debug(
                        f"加载任务 context_path 失败；跳过: mission_id={id}, path={path}, error={error}"
                    )

        # 构建元提示
        retrieval = RetrievalEngine(self.store)
        project_docs = await retrieval.retrieve_context(
            mission.project_id, mission.user_id, mission.goal, 10
        )
        meta_prompt = build_meta_prompt(mission, project_docs, trigger_payload, context_blocks)

        # 生成线程
        thread_id = await self.thread_manager.spawn_thread_with_title(
            meta_prompt,
            mission.name,
            ThreadType.Mission,
            mission.project_id,
            ThreadConfig(),
            None,
            user_id,
        )

        fire_instant = datetime.now(timezone.utc)

        # 在持久化任务更新之前安装结果观察者
        self.spawn_mission_outcome_watcher(id, thread_id, fire_instant)

        # 记录线程 + 触发负载
        mission.record_thread(thread_id)
        mission.threads_today += 1
        mission.last_trigger_payload = trigger_payload

        # 推进 cron 任务的 next_fire_at
        cron_advanced = True
        if isinstance(mission.cadence, Cron):
            try:
                mission.next_fire_at = next_cron_fire(
                    mission.cadence.expression, mission.cadence.timezone
                )
            except Exception as e:
                cron_advanced = False
                logger.debug(
                    f"触发后推进 next_fire_at 失败；保留现有值并通过不匹配启用冷却: "
                    f"mission_id={id}, expression={mission.cadence.expression}, error={e}"
                )

        if cron_advanced:
            mission.last_fire_at = fire_instant

        # 在持久化调用之前启用内存冷却
        self.last_fire_attempt[id] = fire_instant

        # 持久化是最佳努力的
        try:
            await self.store.save_mission(mission)
        except Exception as e:
            logger.debug(
                f"触发后持久化任务更新失败；线程正在运行并受监视，内存冷却将抑制重新触发: "
                f"mission_id={id}, thread_id={thread_id}, error={e}"
            )

        # 记录每用户速率
        await self.record_user_rate(mission.user_id)

        logger.debug(f"任务已触发: mission_id={id}, thread_id={thread_id}")
        return thread_id

    async def resume_paused_for_credential(
            self, credential_name: str, user_id: str
    ) -> List[MissionId]:
        """恢复每个暂停的任务，其 `paused_gate` 正在等待给定凭证名称"""
        candidates = await self.list_paused_missions_for_user(user_id)
        resumed = []

        for mission in candidates:
            gate = mission.paused_gate
            if gate is None:
                continue
            resume_kind = getattr(gate, 'resume_kind', None)
            if resume_kind is None or resume_kind.get("type") != "Authentication":
                continue
            if resume_kind.get("credential_name") != credential_name:
                continue

            resume_owner = mission.user_id
            fire_owner = user_id if mission.owner_id().is_shared() else resume_owner

            try:
                await self.resume_mission(mission.id, resume_owner)
                logger.debug(
                    f"凭证写入后自动恢复暂停的任务: mission_id={mission.id}, credential={credential_name}"
                )
                resumed.append(mission.id)

                if not isinstance(mission.cadence, Manual):
                    try:
                        await self.fire_mission(mission.id, fire_owner, None)
                    except Exception as e:
                        logger.debug(
                            f"恢复后立即触发失败；将在下次 tick 时重试: mission_id={mission.id}, error={e}"
                        )
            except Exception as e:
                logger.debug(
                    f"自动恢复暂停任务失败: mission_id={mission.id}, error={e}"
                )

        return resumed

    async def resume_paused_for_request_id(
            self, gate_request_id: uuid.UUID, resolution: GateResolutionOutcome, user_id: str
    ) -> Optional[MissionId]:
        """根据 gate_request_id 恢复暂停的任务"""
        candidates = await self.list_paused_missions_for_user(user_id)
        snapshot = None
        for m in candidates:
            gate = m.paused_gate
            if gate is not None and getattr(gate, 'gate_request_id', None) == gate_request_id:
                snapshot = m
                break

        if snapshot is None:
            return None

        mission_id = snapshot.id
        mission = await self.store.load_mission(mission_id)
        if mission is None:
            raise EngineError(f"Store: 未找到任务 {mission_id}")

        allowed = (
            is_shared_owner(user_id) if mission.owner_id().is_shared()
            else mission.is_owned_by(user_id)
        )
        if not allowed:
            raise EngineError(f"AccessDenied: 用户 '{user_id}' 不能访问任务 {mission_id}")

        still_matches = (
                mission.status == MissionStatus.Paused
                and mission.paused_gate is not None
                and getattr(mission.paused_gate, 'gate_request_id', None) == gate_request_id
        )
        if not still_matches:
            logger.debug(
                f"任务在恢复时不再在此门控上暂停；跳过: "
                f"mission_id={mission_id}, gate_request_id={gate_request_id}, live_status={mission.status}"
            )
            return None

        cadence_for_fire = mission.cadence
        fire_owner = user_id if mission.owner_id().is_shared() else mission.user_id

        if resolution == GateResolutionOutcome.Approved:
            mission.status = MissionStatus.Active
            mission.paused_gate = None
            if isinstance(mission.cadence, Cron):
                mission.next_fire_at = next_cron_fire_required(
                    mission.cadence.expression, mission.cadence.timezone
                )
            mission.updated_at = datetime.now(timezone.utc)
            await self.store.save_mission(mission)

            async with self._lock:
                if mission_id not in self.active:
                    self.active.append(mission_id)

            logger.debug(
                f"批准门控解决后自动恢复暂停的任务: mission_id={mission_id}, gate_request_id={gate_request_id}"
            )

            if not isinstance(cadence_for_fire, Manual):
                try:
                    await self.fire_mission(mission_id, fire_owner, None)
                except Exception as e:
                    logger.debug(
                        f"恢复后立即触发失败；将在下次 tick 时重试: mission_id={mission_id}, error={e}"
                    )

            return mission_id

        elif resolution in (GateResolutionOutcome.Denied, GateResolutionOutcome.Cancelled):
            mission.status = MissionStatus.Failed
            mission.paused_gate = None
            mission.approach_history.append(
                f"FAILED: gate {gate_request_id} denied or cancelled"
            )
            mission.updated_at = datetime.now(timezone.utc)
            await self.store.save_mission(mission)
            async with self._lock:
                self.active = [mid for mid in self.active if mid != mission_id]

            logger.debug(
                f"门控拒绝/取消后将暂停的任务标记为 Failed: mission_id={mission_id}, gate_request_id={gate_request_id}"
            )
            return mission_id

        return None

    async def list_paused_missions_for_user(self, user_id: str) -> List[Mission]:
        """列出用户在所有项目中可见的每个暂停任务"""
        projects = await self.store.list_all_projects()
        paused = []
        for project in projects:
            missions = await self.store.list_missions_with_shared(project.id, user_id)
            for m in missions:
                if m.status == MissionStatus.Paused and m.paused_gate is not None:
                    paused.append(m)
        return paused

    async def fire_on_system_event(
            self, source: str, event_type: str, user_id: str, payload: Optional[dict] = None
    ) -> List[ThreadId]:
        """触发所有匹配 source 和 event_type 的活跃 OnSystemEvent 任务"""
        async with self._lock:
            active_ids = list(self.active)

        spawned = []
        for mid in active_ids:
            mission = await self.store.load_mission(mid)
            if mission is None:
                continue

            if not (
                    mission.status == MissionStatus.Active
                    or (mission.status == MissionStatus.Completed and mission.is_event_driven())
            ):
                continue

            if not mission.is_owned_by(user_id) and not mission.owner_id().is_shared():
                continue

            if not isinstance(mission.cadence, OnSystemEvent):
                continue

            if mission.cadence.source != source or mission.cadence.event_type != event_type:
                continue

            if not payload_matches_filters(mission.cadence.filters, payload):
                continue

            # 去重
            if mission.dedup_window_secs > 0:
                key = payload_dedup_key(payload)
                if await self.dedup_event(mid, key, mission.dedup_window_secs):
                    logger.debug(
                        f"跳过 system_event 触发 — 去重窗口尚未过去: "
                        f"mission_id={mid}, dedup_window_secs={mission.dedup_window_secs}"
                    )
                    continue

            tid = await self.fire_mission(mid, user_id, payload)
            if tid is not None:
                spawned.append(tid)

        return spawned

    async def fire_on_message_event(
            self, channel: str, text: str, user_id: str, payload: Optional[dict] = None
    ) -> List[ThreadId]:
        """触发所有匹配消息的活跃 OnEvent 任务"""
        async with self._lock:
            active_ids = list(self.active)

        spawned = []
        for mid in active_ids:
            mission = await self.store.load_mission(mid)
            if mission is None:
                continue

            if not (
                    mission.status == MissionStatus.Active
                    or (mission.status == MissionStatus.Completed and mission.is_event_driven())
            ):
                continue

            if not mission.is_owned_by(user_id) and not mission.owner_id().is_shared():
                continue

            if not isinstance(mission.cadence, OnEvent):
                continue

            # 频道过滤（不区分大小写）
            cadence_channel = mission.cadence.channel
            if cadence_channel is not None and cadence_channel.lower() != channel.lower():
                continue

            # 正则表达式匹配
            if not await self.event_regex_matches(mission, text):
                continue

            if mission.dedup_window_secs > 0:
                key = payload_dedup_key(payload)
                if await self.dedup_event(mid, key, mission.dedup_window_secs):
                    continue

            tid = await self.fire_mission(mid, user_id, payload)
            if tid is not None:
                spawned.append(tid)

        return spawned

    async def fire_on_webhook(
            self, webhook_path: str, user_id: str, payload: Optional[dict] = None
    ) -> List[ThreadId]:
        """触发匹配 webhook 路径的活跃 Webhook 任务"""
        async with self._lock:
            active_ids = list(self.active)

        spawned = []
        for mid in active_ids:
            mission = await self.store.load_mission(mid)
            if mission is None:
                continue

            if not (
                    mission.status == MissionStatus.Active
                    or (mission.status == MissionStatus.Completed and mission.is_event_driven())
            ):
                continue

            if not mission.is_owned_by(user_id) and not mission.owner_id().is_shared():
                continue

            if not isinstance(mission.cadence, Webhook):
                continue

            if mission.cadence.path != webhook_path:
                continue

            if mission.dedup_window_secs > 0:
                key = payload_dedup_key(payload)
                if await self.dedup_event(mid, key, mission.dedup_window_secs):
                    continue

            tid = await self.fire_mission(mid, user_id, payload)
            if tid is not None:
                spawned.append(tid)

        return spawned

    async def list_missions(self, project_id: ProjectId, user_id: str) -> List[Mission]:
        """列出项目中用户可见的任务（自己的 + 共享的）"""
        return await self.store.list_missions_with_shared(project_id, user_id)

    async def get_mission(self, id: MissionId) -> Optional[Mission]:
        """按 ID 获取任务"""
        return await self.store.load_mission(id)

    async def find_by_name(
            self, project_id: ProjectId, user_id: str, name: str
    ) -> Optional[Mission]:
        """按名称查找任务，限定在 (project_id, user_id) 范围内"""
        missions = await self.store.list_missions_with_shared(project_id, user_id)
        for m in missions:
            if m.name == name:
                return m
        return None

    async def tick(self, fallback_user_id: str = "") -> List[ThreadId]:
        """Tick — 检查所有活跃任务并触发任何到期的任务。返回生成的线程 ID 列表"""
        async with self._lock:
            active_ids = list(self.active)

        spawned = []
        now = datetime.now(timezone.utc)
        cooldown = timedelta(seconds=FIRE_COOLDOWN_SECS)

        # 机会主义清理 last_fire_attempt
        stale_keys = [
            k for k, v in self.last_fire_attempt.items()
            if now - v >= cooldown
        ]
        for k in stale_keys:
            self.last_fire_attempt.pop(k, None)

        for mid in active_ids:
            try:
                mission = await self.store.load_mission(mid)
            except Exception as e:
                logger.debug(f"tick: 加载任务失败；跳过: mission_id={mid}, error={e}")
                continue

            if mission is None or mission.status != MissionStatus.Active:
                continue

            should_fire = False
            if isinstance(mission.cadence, Cron):
                should_fire = mission.next_fire_at is not None and mission.next_fire_at <= now

            if not should_fire:
                continue

            # 内存冷却
            on_cooldown = False
            in_mem_last = self.last_fire_attempt.get(mid)
            if in_mem_last is not None:
                if (now - in_mem_last < cooldown
                        and mission.last_fire_at != in_mem_last):
                    on_cooldown = True

            if on_cooldown:
                logger.debug(
                    f"tick: 检测到触发后持久化 last_fire_at 过时；在协调之前抑制重新触发: mission_id={mid}"
                )
                continue

            try:
                tid = await self.fire_mission(mid, mission.user_id, None)
                if tid is not None:
                    spawned.append(tid)
            except Exception as e:
                logger.debug(
                    f"tick: fire_mission 失败；继续处理剩余任务: mission_id={mid}, error={e}"
                )

        return spawned

    async def count_running_threads(self, mission: Mission) -> int:
        """计算任务生成的非终止状态线程数"""
        running = 0
        for tid in reversed(mission.history_thread_ids):
            try:
                thread = await self.store.load_thread(tid)
            except Exception:
                continue
            if thread is not None and thread.state not in (ThreadState.Done, ThreadState.Failed):
                running += 1
        return running

    async def dedup_event(self, mission_id: MissionId, dedup_key: str, window_secs: int) -> bool:
        """检查去重：如果 (mission_id, dedup_key) 在 window_secs 内已存在则返回 True"""
        if window_secs == 0:
            return False

        now = datetime.now(timezone.utc)
        window = timedelta(seconds=window_secs)
        key = (mission_id, dedup_key)

        last = self.dedup_table.get(key)
        if last is not None and (now - last) < window:
            return True

        self.dedup_table[key] = now
        return False

    async def event_regex_matches(self, mission: Mission, text: str) -> bool:
        """测试文本是否匹配任务的 OnEvent 正则表达式"""
        if not isinstance(mission.cadence, OnEvent):
            return False

        pattern_str = mission.cadence.event_pattern

        # 缓存命中快速路径
        if mission.id in self.event_regex_cache:
            return bool(self.event_regex_cache[mission.id].search(text))

        # 编译并缓存
        try:
            compiled = re.compile(pattern_str)
            self.event_regex_cache[mission.id] = compiled
            return bool(compiled.search(text))
        except re.error as error:
            logger.warning(
                f"OnEvent 任务正则表达式编译失败（或超过大小限制）；拒绝匹配: "
                f"mission_id={mission.id}, pattern={pattern_str}, error={error}"
            )
            return False

    async def evict_event_regex(self, mission_id: MissionId) -> None:
        """丢弃编译的正则表达式，强制下次匹配尝试重新编译"""
        self.event_regex_cache.pop(mission_id, None)

    async def check_user_rate(self, user_id: str) -> bool:
        """每用户全局速率限制器 — 只读检查"""
        now = datetime.now(timezone.utc)
        window = timedelta(seconds=self.rate_limit.window_secs)
        cutoff = now - window

        if user_id not in self.user_fire_log:
            self.user_fire_log[user_id] = deque()

        entries = self.user_fire_log[user_id]
        while entries and entries[0] < cutoff:
            entries.popleft()

        return len(entries) < self.rate_limit.max_fires

    async def record_user_rate(self, user_id: str) -> None:
        """记录成功触发"""
        now = datetime.now(timezone.utc)
        if user_id not in self.user_fire_log:
            self.user_fire_log[user_id] = deque()
        self.user_fire_log[user_id].append(now)

    async def budget_allows(self, user_id: str, mission_id: MissionId) -> bool:
        """咨询预算门控（如果附加）"""
        if self.budget_gate is None:
            return True
        return await self.budget_gate.allow_mission_fire(user_id, mission_id)

    def spawn_mission_outcome_watcher(
            self, mission_id: MissionId, thread_id: ThreadId, original_fire_at: datetime
    ) -> None:
        """生成任务结果观察者"""
        tm = self.thread_manager
        store = self.store
        effects = self.effects
        notification_tx = self.notification_tx

        async def watcher():
            try:
                outcome = await tm.join_thread(thread_id)
                await process_mission_outcome_and_notify(
                    store, effects, mission_id, thread_id, outcome, notification_tx, original_fire_at,
                )
            except Exception as e:
                logger.debug(f"任务结果处理失败: mission_id={mission_id}, error={e}")

        asyncio.create_task(watcher())


def payload_matches_filters(
        filters: Dict[str, Any],
        payload: Optional[dict] = None,
) -> bool:
    """检查每个 (key, value) 对是否精确匹配负载的顶级字段。
    空的过滤映射始终匹配。None 负载仅匹配空的过滤映射
    """
    if not filters:
        return True
    if payload is None:
        return False
    if not isinstance(payload, dict):
        return False
    return all(
        key in payload and payload[key] == value
        for key, value in filters.items()
    )


def payload_dedup_key(payload: Optional[dict] = None) -> str:
    """为事件负载计算稳定的去重键。将规范化的 JSON 序列化进行哈希处理。
    空/None 负载折叠为单个固定键，以便抑制相同空事件的洪流
    """
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False) if payload is not None else ""
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]


def build_meta_prompt(
        mission: Mission,
        project_docs: List[MemoryDoc],
        trigger_payload: Optional[dict] = None,
        context_blocks: Optional[List[Tuple[str, str]]] = None,
) -> str:
    """为任务线程构建元提示

    将任务的目标、当前焦点、方法历史和相关的项目文档组装成结构化提示，
    指导线程执行
    """
    if context_blocks is None:
        context_blocks = []

    parts = []

    # 任务标题和目标
    parts.append(
        f"# 任务: {mission.name}\n\n目标: {mission.goal}"
    )

    # 描述
    if mission.description:
        parts.append(f"\n{mission.description}")

    # 成功标准
    if mission.success_criteria:
        parts.append(f"成功标准: {mission.success_criteria}")

    # 预加载的工作区上下文
    if context_blocks:
        parts.append("\n## 已加载的上下文")
        for path, content in context_blocks:
            parts.append(f"### {path}\n\n{content}")

    # 当前焦点
    if mission.current_focus:
        parts.append(f"\n## 当前焦点\n{mission.current_focus}")
    elif not mission.history_thread_ids:
        parts.append(
            "\n## 当前焦点\n这是首次运行。从理解目标并确定第一步开始。"
        )

    # 方法历史
    if mission.approach_history:
        parts.append("\n## 先前的方法")
        for i, approach in enumerate(mission.approach_history):
            parts.append(f"{i + 1}. {approach}")

    # 项目知识（来自反思文档）
    if project_docs:
        parts.append("\n## 来自先前线程的知识")
        for doc in project_docs:
            label = str(doc.doc_type).upper()
            content = doc.content[:500]
            truncated = "..." if len(doc.content) > 500 else ""
            parts.append(f"[{label}] {doc.title}: {content}{truncated}")

    # 触发负载
    if trigger_payload is not None:
        payload_str = json.dumps(trigger_payload, indent=2, ensure_ascii=False)
        preview = payload_str[:1000]
        parts.append(f"\n## 触发负载\n```json\n{preview}\n```")

    # 线程计数
    parts.append(
        f"\n这是此任务的第 {len(mission.history_thread_ids) + 1} 个线程。"
    )

    # 指令
    parts.append(
        "\n## 指令\n基于以上上下文，朝着目标迈出下一步。"
        "使用工具收集信息、分析数据或执行动作。"
        "完成后，使用 FINAL() 返回你的响应。包括：\n"
        "1. 你在此步骤中完成了什么\n"
        "2. 下一个焦点应该是什么（用于下一个线程）\n"
        "3. 目标是否已实现（是/否）"
    )

    return "\n".join(parts)
