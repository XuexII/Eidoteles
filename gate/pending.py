# 待处理门控状态——统一类型，替代 `PendingApproval` 和 `PendingAuth`。
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from engine import CapabilityLease, ResumeKind, ThreadId, ConversationId
from ironclaw_common import ExternalThreadId


@dataclass(frozen=True)
class PendingGateKey:
    """
    待处理门控存储的复合键。

    按 `(user_id, thread_id)` 键控——每个线程恰好一个待处理门控。
    这消除了当批准仅按 `user_id` 键控时存在的 `Ambiguous` 解析路径。
    """
    user_id: str
    thread_id: ThreadId


@dataclass
class PendingGate:
    """
    暂停执行的任何门控的统一待处理状态。

    替换路由器的 `PendingApproval` 和 `PendingAuth`。
    存储在 [`PendingGateStore`] 中并通过 [`GatePersistence`] 持久化。
    """
    # 此待处理门控请求的唯一 ID
    request_id: str
    # 哪个门控创建了此待处理状态（例如 "approval"、"authentication"）
    gate_name: str
    # 触发门控的用户
    user_id: str
    # 被暂停的引擎线程
    thread_id: ThreadId
    # 引擎线程之上的对话标识符
    conversation_id: ConversationId
    # 发起请求的通道。解析必须来自相同通道（或可信通道）
    source_channel: str
    # 触发门控的工具
    action_name: str
    # 来自 LLM 的工具调用 ID
    call_id: str
    # 工具参数
    parameters: Dict[str, Any]
    # 工具将执行的操作的人类可读描述
    description: str
    # 期望的解析类型
    resume_kind: ResumeKind
    # 此待处理状态的创建时间
    created_at: datetime
    # 此待处理状态的过期时间（过期后安全关闭）
    expires_at: datetime
    # 外部/客户端可见的线程 ID，用于在引擎线程之上维护自己的对话标识符的通道。
    #
    # 这是通道提供的标识符（web UUID、Telegram chat id、Slack `thread_ts`）——
    # 不是内部引擎 [`ThreadId`]。参见 `crates/ironclaw_common/src/identity.rs`
    # 中的 `ExternalThreadId` 原理。
    scope_thread_id: Optional[ExternalThreadId] = None
    # 安全用于 UI 显示和历史记录的经过编辑的参数
    display_parameters: Optional[Dict[str, Any]] = None
    # 当门控来自完成而不是暂停线程的回退路径时，要重试的原始用户消息
    original_message: Optional[str] = None
    # 认证完成后在恢复时注入的已完成操作输出
    resume_output: Optional[Dict[str, Any]] = None
    # 在恢复暂停操作时重用的租约快照，该操作的原始租约在门控触发前已被消费
    paused_lease: Optional[CapabilityLease] = None
    # 此暂停操作的批准是否已被授予
    approval_already_granted: bool = False

    @property
    def is_expired(self) -> bool:
        """检查此待处理门控是否已过期。"""
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def effective_wire_thread_id(self) -> str:
        """
        通道事件的有效线线程标识符——当设置时为外部范围
        （保留通道使用的任何内容），否则为渲染为字符串的内部引擎 UUID。
        选择此方式是因为下游 `AppEvent` 字段目前携带纯字符串，
        且通道依赖单一真实来源来确定要路由回哪个标识符。
        """
        if self.scope_thread_id is not None:
            return str(self.scope_thread_id)
        return str(self.thread_id)

    @property
    def key(self) -> PendingGateKey:
        """构建此门控的复合键。"""
        return PendingGateKey(
            user_id=self.user_id,
            thread_id=self.thread_id,
        )


@dataclass
class PendingGateView:
    """API 响应的待处理门控只读视图。"""
    request_id: str
    thread_id: str
    gate_name: str
    tool_name: str
    description: str
    parameters: str
    resume_kind: ResumeKind

    @classmethod
    def from_gate(cls, gate: PendingGate) -> "PendingGateView":
        """从 PendingGate 创建视图。"""
        import json
        params = gate.display_parameters if gate.display_parameters is not None else gate.parameters
        return cls(
            request_id=str(gate.request_id),
            thread_id=gate.effective_wire_thread_id(),
            gate_name=gate.gate_name,
            tool_name=gate.action_name,
            description=gate.description,
            parameters=json.dumps(params, indent=2, ensure_ascii=False),
            resume_kind=gate.resume_kind,
        )
