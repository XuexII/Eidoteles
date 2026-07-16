"""
Thread——工作单元

每个Thread代表一个完整的执行任务，具有自己的生命周期、状态机、能力租约和资源消耗跟踪
它将会话（交互式对话）、作业（后台工作）、例程（定时执行）和子智能体（委托推理）的概念统一为具有共享状态机的单一抽象

流程:
ThreadManager创建Thread实例，设置goal为用户输入，授予capability leases，并启动执行
"""

from __future__ import annotations
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, List

from .capability import LeaseId
from .event import (
    EventKind,
    EventKindStateChanged,
    EventKindMessageAdded,
    ThreadEvent
)
from .message import ThreadMessage
from ironclaw_common.common import DEFAULT_USER_ID, OwnerId

logger = logging.getLogger(__name__)


# 标识线程的状态
class ThreadState(str, Enum):
    """线程状态枚举"""
    # 线程已创建但尚未启动
    Created = "Created"
    # 线程正在主动执行步骤
    Running = "Running"
    # 等待外部输入（用户批准、子线程完成）
    Waiting = "Waiting"
    # 被系统暂停（资源压力、优先级抢占）
    Suspended = "Suspended"
    # 执行成功完成
    Completed = "Completed"
    # 完全完成（终止状态）
    Done = "Done"
    # 终止失败
    Failed = "Failed"

    def can_transition_to(self, target: "ThreadState") -> bool:
        """检查转换到目标状态是否有效"""
        valid_transitions = {
            (ThreadState.Created, ThreadState.Running),
            (ThreadState.Created, ThreadState.Failed),
            (ThreadState.Running, ThreadState.Waiting),
            (ThreadState.Running, ThreadState.Suspended),
            (ThreadState.Running, ThreadState.Completed),
            (ThreadState.Running, ThreadState.Failed),
            (ThreadState.Waiting, ThreadState.Running),
            (ThreadState.Waiting, ThreadState.Failed),
            (ThreadState.Suspended, ThreadState.Running),
            (ThreadState.Suspended, ThreadState.Failed),
            (ThreadState.Completed, ThreadState.Done),
        }
        return (self, target) in valid_transitions

    def is_terminal(self) -> bool:
        """该状态是否为终止状态（无法再进行转换）"""
        return self in (ThreadState.Done, ThreadState.Failed)

    def is_active(self) -> bool:
        """该状态是否表示正在进行中的工作"""
        return self in (ThreadState.Running, ThreadState.Waiting)


# 标识线程/任务的类型
class ThreadType(str, Enum):
    """线程执行的工作性质"""
    # 与用户的交互式对话
    Foreground = "Foreground"
    # 后台研究或子任务
    Research = "Research"
    # 长期运行的目标，随时间推移产生线程
    Mission = "Mission"


# LLM 调用的目的
class LlmCallPurpose(str, Enum):
    """LLM 调用的目的"""
    Chat = "chat"


# 线程配置
@dataclass
class ThreadConfig:
    """线程的执行参数"""
    # LLM 调用迭代的最大次数
    max_iterations: int = 50
    # 最大执行时长
    max_duration: Optional[timedelta] = None
    # 是否检测并提示没有动作调用的工具意图
    enable_tool_intent_nudge: bool = True
    # 每个线程的最大工具意图提示次数
    max_tool_intent_nudges: int = 2
    # 是否要求在接收文本响应之前至少尝试一次动作/代码。
    # 当在用户消息中检测到明确的执行意图时由路由设置（例如 "运行它"、"获取数据"）
    require_action_attempt: bool = False
    # 当 require_action_attempt 为 true 时的最大纠正提示次数
    max_action_requirement_nudges: int = 2
    # 终止前累计输入+输出 token 的最大数量
    max_tokens_total: Optional[int] = None
    # 终止前连续出错的最大步数。
    # 在任何成功步骤后重置为 0（匹配官方 RLM 行为）
    max_consecutive_errors: Optional[int] = 5
    # 模型上下文限制（以 token 为单位）（用于压缩阈值计算）。
    # 默认值：128,000。用于在 85% 使用率时触发压缩
    model_context_limit: int = 128_000
    # 上下文变大时是否启用自动压缩
    enable_compaction: bool = False
    # 压缩阈值，作为 model_context_limit 的分数（0.0-1.0）。
    # 默认值：0.85（匹配官方 RLM）
    compaction_threshold: float = 0.85
    # 终止前的最大累计美元成本。
    # 需要 LlmBackend 填充 `TokenUsage::cost_usd`
    max_budget_usd: Optional[float] = None
    # 此线程在递归调用树中的深度。
    # 根线程深度为 0。通过 rlm_query() 的子调用增加深度
    depth: int = 0
    # rlm_query() 子调用的最大递归深度
    max_depth: int = 1


# ── 活跃技能溯源 ─────────────────────────────────────────────

@dataclass
class ActiveSkillProvenance:
    """线程执行期间处于激活状态的skill的来源信息"""
    doc_id: str
    name: str
    version: int
    snippet_names: List[str] = field(default_factory=list)
    force_activated: bool = False


ACTIVE_SKILLS_METADATA_KEY = "active_skills"
LLM_USAGE_METADATA_USER_ID_MAX_LEN = 255


# ── 线程 ─────────────────────────────────────────────────────
def generate_thread_id() -> str:
    return str(uuid.uuid4())


@dataclass(kw_only=True)
class Thread:
    """线程 — 工作单元"""
    _id: str = field(default_factory=generate_thread_id, init=False)
    # 执行目标/用户输入
    goal: str
    # 用户可见的简短标签，用于UI显示（如侧边栏）
    title: Optional[str] = None
    # thread类型
    thread_type: ThreadType
    # 当前状态
    state: ThreadState = ThreadState.Created
    # 所属项目，用于隔离上下文
    project_id: str
    # 租户隔离：拥有此线程的用户。
    user_id: str = DEFAULT_USER_ID
    # 父thread ID，支持层级嵌套
    parent_thread_id: Optional[str] = None
    # 执行配置（迭代限制、预算、超时等
    config: ThreadConfig
    # 用户可见的对话历史
    messages: List[ThreadMessage] = field(default_factory=list)
    # 内部执行记录，用于orchestrator推理和工具追踪
    internal_messages: List[ThreadMessage] = field(default_factory=list)
    # 事件日志，记录执行过程中的关键事件
    events: List[ThreadEvent] = field(default_factory=list)
    # 授予的能力租约，定义可执行的操作范围
    capability_leases: List[LeaseId] = field(default_factory=list)
    # 扩展元数据，存储自定义信息
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    # 执行步数统计
    step_count: int = 0
    # 累计token消耗
    total_tokens_used: int = 0
    # 所有步骤的累计美元成本
    total_cost_usd: float = 0.0

    @property
    def id(self):
        return self._id

    def with_parent(self, parent_thread_id: str):
        """设置父线程"""
        self.parent_thread_id = parent_thread_id

    @staticmethod
    def derive_title_from_message(message: str) -> Optional[str]:
        """从自由格式的用户消息中派生简短的侧边栏标题

        取第一个非空行，去除首尾空白，按字符数（不是字节数）截断，
        这样多字节输入不会出错。如果消息全是空白则返回 None
        """
        MAX_CHARS = 60
        # 找到第一个非空行
        trimmed = None
        for line in message.splitlines():
            stripped = line.strip()
            if stripped:
                trimmed = stripped
                break

        if trimmed is None:
            return None

        # 流式截断避免在长单行输入上进行 O(n) 的字符计数：
        # 取最多 MAX_CHARS-1 个字符，然后查看剩余部分。
        # 如果恰好还剩一个字符，追加它（结果是 MAX_CHARS 个字符，没有省略号）；
        # 如果还有更多，追加 '…'
        chars = list(trimmed)
        if len(chars) <= MAX_CHARS:
            return trimmed

        out = ''.join(chars[:MAX_CHARS - 1])
        if len(chars) == MAX_CHARS:
            out += chars[MAX_CHARS - 1]
        else:
            out += '…'
        return out

    @property
    def owner_id(self) -> OwnerId:
        """获取线程所有者的 ID"""
        return OwnerId.from_user_id(self.user_id)

    def is_owned_by(self, user_id: str) -> bool:
        """检查线程是否属于指定用户"""
        return self.owner_id.matches_user(user_id)

    def llm_usage_metadata(self, purpose: LlmCallPurpose) -> dict:
        """构建附加到引擎 LLM 使用记账记录的元数据

        Args:
            purpose: LLM 调用的目的

        Returns:
            包含 thread_id、purpose 以及可选的 user_id、conversation_scope、
            v1_conversation_id 的元数据字典
        """
        metadata = {
            "thread_id": str(self.id),
            "purpose": purpose,
        }

        # 仅在 user_id 非空且长度未超过限制时附加
        if self.user_id and len(self.user_id) <= LLM_USAGE_METADATA_USER_ID_MAX_LEN:
            metadata["user_id"] = self.user_id

        # 附加 conversation_scope（如果存在）
        if scope := self.metadata.get("conversation_scope"):
            if isinstance(scope, str):
                metadata["conversation_scope"] = scope

        # 附加 v1_conversation_id（如果存在）
        if conversation_id := self.metadata.get("v1_conversation_id"):
            if isinstance(conversation_id, str):
                metadata["v1_conversation_id"] = conversation_id

        return metadata

    def set_active_skills(
            self,
            active_skills: List[ActiveSkillProvenance],
    ):
        """在线程元数据中持久化活跃技能溯源"""
        if not isinstance(self.metadata, dict):
            raise RuntimeError("Store: 线程元数据不是 JSON 对象")

        try:
            self.metadata[ACTIVE_SKILLS_METADATA_KEY] = [
                asdict(skill) for skill in active_skills
            ]
        except Exception as e:
            raise RuntimeError(f"Store: 序列化活跃技能溯源失败: {e}")

        self.updated_at = datetime.now(timezone.utc)

    def active_skills(self) -> List[ActiveSkillProvenance]:
        """从线程元数据中加载活跃技能溯源"""
        skills_data = self.metadata.get(ACTIVE_SKILLS_METADATA_KEY, [])
        try:
            return [ActiveSkillProvenance(**skill) for skill in skills_data]
        except Exception as e:
            return []

    def transition_to(
            self,
            new_state: ThreadState,
            reason: Optional[str] = None,
    ):
        """转换到新状态，记录事件"""
        if not self.state.can_transition_to(new_state):
            raise RuntimeError(f"从{self.state} 到 {new_state} 的转换无效")

        event = ThreadEvent(
            thread_id=self.id,
            kind=EventKindStateChanged(
                from_state=self.state,
                to=new_state,
                reason=reason,
            ),
        )
        self.events.append(event)
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc)

        if new_state in (ThreadState.Completed, ThreadState.Done):
            self.completed_at = datetime.now(timezone.utc)

    def add_event(self, kind: EventKind):
        """向该线程的日志中添加事件"""
        self.events.append(ThreadEvent(thread_id=self.id, kind=kind))
        self.updated_at = datetime.now(timezone.utc)

    def add_message(self, message: ThreadMessage):
        """向该线程的对话中添加消息"""
        content = message.content
        if len(content) > 80:
            preview = content[:80] + "..."
        else:
            preview = content

        self.add_event(EventKindMessageAdded(
            role=message.role,
            content_preview=preview,
        ))
        self.messages.append(message)

    def add_internal_message(self, message: ThreadMessage):
        """向内部执行转录中添加消息，不将其作为用户可见的对话消息暴露"""
        self.internal_messages.append(message)
        self.updated_at = datetime.now(timezone.utc)
