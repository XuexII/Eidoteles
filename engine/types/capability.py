# 能力——效果的单位。
#
# 能力将动作（工具）、知识（技能）和策略（钩子）捆绑为一个可安装/可激活的单元。
# 能力通过租约授予线程。
#
# 面向模型的暴露是有意拆分的：
# - `ActionInventory` 包含当前步骤可调用的动作
# - `CapabilitySummary` 包含背景/上下文能力元数据，
#   包括应属于“可激活集成”而非普通可调用界面中的已屏蔽集成

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional



# ── 为action 授权 ────────────────────────────────────────

class GrantedType(str, Enum):
    # 租约覆盖能力中的所有操作（通配符）。
    All = "all"
    # 将租约限制为列出的操作名称。
    Specific = "Specific"


@dataclass
class GrantedActions:
    # 授权的类型
    type: GrantedType
    # 获得授权的操作
    actions: List[str] = field(default_factory=list)

    def covers(self, action_name: str) -> bool:
        """检查特定操作是否被覆盖。"""
        hyphenated = action_name.replace('_', '-')
        underscored = action_name.replace('-', '_')

        match self.type:
            case GrantedType.All:
                return True
            case GrantedType.Specific:
                return any([action in {action_name, hyphenated, underscored} for action in self.actions])

        return False

    @property
    def is_all(self) -> bool:
        """如果这是通配符授予，返回 true。"""
        return self.type == GrantedType.All


# ── 租约 ID ──────────────────────────────────────────
@dataclass(frozen=True)
class LeaseId:
    """强类型租约标识符。"""
    value: uuid.UUID = field(default_factory=uuid.uuid4)

    def __str__(self) -> str:
        return str(self.value)


# ── 效果类型 ────────────────────────────────────────────

class EffectType(str, Enum):
    """
    操作可能产生的副作用的分类。
    由策略引擎用于允许/拒绝决策。
    """
    # 从本地文件系统或工作区读取
    ReadLocal = "read_local"
    # 从外部 API 读取（无变更）
    ReadExternal = "read_external"
    # 写入本地文件系统或工作区
    WriteLocal = "write_local"
    # 写入外部服务（创建 PR、发送邮件）
    WriteExternal = "write_external"
    # 需要凭证的认证 API 调用
    CredentialedNetwork = "credentialed_network"
    # 代码执行或 shell 访问
    Compute = "compute"
    # 金融操作（支付、转账）
    Financial = "financial"


# ── 操作定义 ───────────────────────────────────────
class ModelToolSurface(Enum):
    """
    操作是否应作为提供者原生工具定义发出，还是仅通过紧凑的提示元数据
    按需 `tool_info` 显示。
    """
    # 将完整的可调用模式发出到提供者原生工具数组
    FullSchema = "full_schema"
    # 保持在步骤中可调用，但以紧凑方式在提示元数据中显示，
    # 并依赖 `tool_info(..., detail="schema")` 获取参数
    CompactToolInfo = "compact_tool_info"


@dataclass
class ActionDef:
    """能力中单个操作的定义。"""
    # 操作名称（例如 "create_issue"、"web_fetch"）
    name: str
    # 人类可读描述
    description: str
    # 参数的 JSON Schema
    parameters_schema: Dict[str, Any]
    # 此操作可能产生的效果类型
    effects: List[EffectType] = field(default_factory=list)
    # 此操作在执行前是否需要用户批准
    requires_approval: bool = False
    # 此操作应如何向模型展示
    model_tool_surface: ModelToolSurface = ModelToolSurface.FullSchema
    # 可选的发现元数据，用于 `tool_info` 和提示指导
    discovery: Optional[ActionDiscoveryMetadata] = None

    def emits_full_schema_tool(self) -> bool:
        """此操作是否应作为提供者原生工具定义与其完整模式一起发出。"""
        return self.model_tool_surface == ModelToolSurface.FullSchema

    @property
    def discovery_name(self) -> str:
        """此操作的规范发现名称。"""
        if self.discovery is not None:
            return self.discovery.name
        return self.name

    @property
    def discovery_schema(self) -> Dict[str, Any]:
        """发现模式，默认可调用模式。"""
        if self.discovery is not None and self.discovery.schema_override is not None:
            return self.discovery.schema_override
        return self.parameters_schema

    @property
    def discovery_summary(self) -> Optional[ActionDiscoverySummary]:
        """精选的发现摘要（如果存在）。"""
        if self.discovery is not None:
            return self.discovery.summary
        return None

    def matches_name(self, name: str) -> bool:
        """检查给定名称是否解析为此操作。"""
        trimmed = name.strip()
        if not trimmed:
            return False
        disc_name = self.discovery_name
        if self.name == trimmed or disc_name == trimmed:
            return True
        if '-' in trimmed or '-' in self.name or '-' in disc_name:
            normalized = trimmed.replace('-', '_')
            return normalized == self.name.replace('-', '_') or normalized == disc_name.replace('-', '_')
        return False


@dataclass
class ActionInventory:
    """单个执行步骤的模型可见操作清单。"""
    # 现在可调用的内联操作
    inline: List[ActionDef] = field(default_factory=list)
    # 尚不可调用，但仍可通过 `tool_info` 用于步骤范围发现的操作
    # （例如在 `Activatable Integrations` 下被阻止的操作）
    discoverable: List[ActionDef] = field(default_factory=list)


@dataclass
class ActionDiscoverySummary:
    """可调用操作的精选发现指导。"""
    # 始终必需的参数
    always_required: List[str] = field(default_factory=list)
    # 条件要求或跨字段不变量
    conditional_requirements: List[str] = field(default_factory=list)
    # 正确选择/使用工具的附加说明
    notes: List[str] = field(default_factory=list)
    # 可选结构化示例
    examples: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ActionDiscoveryMetadata:
    """分层在可执行操作之上的可选发现元数据。"""
    # 向模型显示的规范发现名称
    name: str
    # 可选精选的发现指导
    summary: Optional[ActionDiscoverySummary] = None
    # 可选发现模式（当与可调用模式不同时）
    schema_override: Optional[Dict[str, Any]] = None


# ── 能力状态与摘要 ──────────────────────────────────

class CapabilityStatus(str, Enum):
    """
    能力背景呈现的规范化模型可见状态。
    这是主机运行时真实状态的标准化投影。它本身并非身份验证、激活或安装状态的真实来源。
    """
    # 能力现在可以直接使用
    Ready = "ready"
    # 能力现在可用，但仅通过范围或间接路由
    ReadyScoped = "ready_scoped"
    # 能力存在，但在使用前需要认证
    NeedsAuth = "needs_auth"
    # 能力存在，但在认证或执行可以继续之前需要设置
    NeedsSetup = "needs_setup"
    # 能力已安装或已知，但当前未激活
    Inactive = "inactive"
    # 运行时已知，但尚未激活为直接操作
    Latent = "latent"
    # 能力查找或就绪检查失败，并出现具体运行时错误
    Error = "error"
    # 能力在注册表中已知，但未安装
    AvailableNotInstalled = "available_not_installed"


class CapabilitySummaryKind(Enum):
    """能力后台摘要的高级类别。"""
    # 可通过桥接操作使用的消息或通知路由
    Channel = "channel"
    # 扩展支持的提供者或集成
    Provider = "provider"
    # 引擎原生运行时能力后台
    Runtime = "runtime"


@dataclass
class CapabilitySummary:
    """
    上下文或可激活能力的能力背景摘要。

    已就绪的可调用动作保留在 `ActionInventory` 中。`CapabilitySummary` 涵盖：
        - 应保留在后台提示/UI 中的运行时/上下文信息
        - 在变为可调用之前需要用户设置的集成
        `NeedsSetup`、`Inactive`、`Latent`、`AvailableNotInstalled`）；
        这些在“可激活集成”下向模型呈现，以便它能告知用户有哪些可用选项，
        但模型本身无法启用它们
    """
    # 稳定能力标识符（例如 `telegram` 或 `slack`）
    name: str
    # 高级类别，供提示/UI 渲染器使用
    kind: CapabilitySummaryKind
    # 规范归一化状态
    status: CapabilityStatus
    # 人类可读显示名称（如果可用）
    display_name: Optional[str] = None
    # 可选人类可读描述
    description: Optional[str] = None
    # 启用此能力后解锁的操作预览
    action_preview: List[str] = field(default_factory=list)
    # 可选路由指导，例如 `Usable through message`
    routing_hint: Optional[str] = None


# ── 能力 ──────────────────────────────────────────────

@dataclass
class Capability:
    """一个能力——捆绑操作、知识和策略。"""
    # 能力名称（例如 "github"、"deployment"）
    name: str
    # 人类可读描述
    description: str
    # 可执行操作（替代工具）
    actions: List[ActionDef] = field(default_factory=list)
    # 领域知识块（替代技能）
    knowledge: List[str] = field(default_factory=list)
    # 策略规则（替代钩子）
    policies: List[PolicyRule] = field(default_factory=list)


# ── 策略 ──────────────────────────────────────────────────

class PolicyCondition:
    """策略规则何时适用。"""
    pass


@dataclass
class PolicyConditionAlways(PolicyCondition):
    """始终适用。"""
    pass


@dataclass
class PolicyConditionActionMatches(PolicyCondition):
    """当操作名称完全匹配模式时适用。"""
    pattern: str


@dataclass
class PolicyConditionEffectTypeIs(PolicyCondition):
    """当操作具有特定效果类型时适用。"""
    effect_type: EffectType


class PolicyEffect(Enum):
    """策略引擎的决定。"""
    Allow = "allow"
    Deny = "deny"
    RequireApproval = "require_approval"


@dataclass
class PolicyRule:
    """能力中的命名策略规则。"""
    name: str
    # 策略规则何时适用
    condition: PolicyCondition
    # 策略引擎的决定
    effect: PolicyEffect


# ── 能力租约 ────────────────────────────────────────

@dataclass
class CapabilityLease:
    """对线程的能力访问的时间/使用限制授予。"""
    id: LeaseId
    # 此租约授予的线程
    thread_id: ThreadId
    # 此租约覆盖的能力
    capability_name: str
    # 从能力中授予的操作
    granted_actions: GrantedActions
    # 租约授予时间
    granted_at: datetime
    # 租约过期时间（None = 无过期）
    expires_at: Optional[datetime] = None
    # 最大操作调用次数（None = 无限制）
    max_uses: Optional[int] = None
    # 剩余调用次数（None = 无限制）
    uses_remaining: Optional[int] = None
    # 租约是否已被显式撤销
    revoked: bool = False
    # 租约被撤销的原因（用于审计跟踪）
    revoked_reason: Optional[str] = None

    def is_valid(self) -> bool:
        """检查此租约当前是否有效。"""
        if self.revoked:
            return False
        if self.expires_at is not None and datetime.now(timezone.utc) >= self.expires_at:
            return False
        if self.uses_remaining is not None and self.uses_remaining == 0:
            return False
        return True

    def covers_action(self, action_name: str) -> bool:
        """检查特定操作是否被此租约覆盖。"""
        return self.granted_actions.covers(action_name)

    def consume_use(self) -> bool:
        """消费租约的一次使用。如果没有剩余使用次数，返回 false。"""
        if self.uses_remaining is not None:
            if self.uses_remaining == 0:
                return False
            self.uses_remaining -= 1
        return True

    def refund_use(self) -> None:
        """
        当执行在操作实际完成之前被中断时，
        退还一次先前已消费的使用。
        """
        if (
                self.max_uses is not None
                and self.uses_remaining is not None
                and self.uses_remaining < self.max_uses
        ):
            self.uses_remaining += 1
