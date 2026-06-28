from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, Set, List, Optional, Any

from context import JobContext
from tools.wasm import WebhookCapability

# 工具模式验证的最大嵌套深度，防止恶意构造的模式导致栈溢出。
MAX_SCHEMA_DEPTH = 16


class ApprovalRequirement(str, Enum):
    """
    特定工具调用需要多少审批。
    """
    # 无需审批
    Never = "Never"
    # 需要审批，但会话自动批准可以绕过
    UnlessAutoApproved = "UnlessAutoApproved"
    # 始终需要显式审批（即使已自动批准）
    Always = "Always"

    def is_required(self) -> bool:
        """
        在自动审批不相关的上下文中（例如自主工作器/调度器），此调用是否需要审批。
        """
        return self != ApprovalRequirement.Never


@dataclass
class ApprovalContext:
    """
    为后台作业和例程预计算的自主动工具范围。
    交互式会话不使用此类型 —— 它们仍然依赖 `requires_approval()` 和会话级别的批准状态。
    """
    allowed_tools: Set[str] = field(default_factory=set)

    @classmethod
    def autonomous(cls) -> "ApprovalContext":
        """
        创建一个没有允许工具的自主上下文。

        对应 Rust: pub fn autonomous() -> Self
        """
        return cls()

    @classmethod
    def autonomous_with_tools(cls, tools: Set[str]) -> "ApprovalContext":
        """
        创建一个带有特定允许工具的自主上下文。
        """
        return cls(allowed_tools=set(tools))

    def is_blocked(self, tool_name: str, requirement: ApprovalRequirement) -> bool:
        """
        检查在此上下文中工具调用是否被阻止。

        - Never 工具始终允许（无需审批）。
        - UnlessAutoApproved 工具在自主上下文中允许（自主执行意味着自动批准）。
        - Always 工具仅在明确列在 allowed_tools 中时才允许。
        pub fn is_blocked(&self, tool_name: &str, requirement: ApprovalRequirement) -> bool
        """
        if requirement == ApprovalRequirement.Never:
            return False
        elif requirement == ApprovalRequirement.UnlessAutoApproved:
            return False
        elif requirement == ApprovalRequirement.Always:
            return tool_name not in self.allowed_tools
        return False

    @staticmethod
    def is_blocked_or_default(
            context: Optional["ApprovalContext"],
            tool_name: str,
            requirement: ApprovalRequirement,
    ) -> bool:
        """
        检查给定可选上下文时工具是否被阻止。

        当为 None 时，回退到遗留行为：所有非 Never 工具都被阻止。
        """
        if context is not None:
            return context.is_blocked(tool_name, requirement)
        else:
            return requirement.is_required()


@dataclass
class ToolRateLimitConfig:
    """
    内置工具调用的按工具速率限制配置。

    控制每个用户在每个时间窗口内可以调用某个工具的次数。
    只读工具（echo、time、json、file_read 等）不应被速率限制。
    写入/外部工具（shell、http、file_write、memory_write、create_job）应该被限制。
    """
    # 每分钟最大调用次数
    requests_per_minute: int = 60

    # 每小时最大调用次数
    requests_per_hour: int = 1000


class RiskLevel(str, Enum):
    """
    工具调用的风险级别。

    由 shell 工具用于分类命令，由工作器用于驱动审批决策和可观察性日志记录。
    实现 Ord 以便调用者可以比较级别（例如 risk >= RiskLevel::High）。
    """
    # 只读、安全、可逆（例如 ls、cat、grep）
    Low = "low"

    # 创建或修改状态，但通常可逆（例如 mkdir、git commit、cargo build）
    Medium = "medium"

    # 破坏性、不可逆或安全敏感的（例如 rm -rf、git push --force、kill -9）
    High = "high"

    def __str__(self) -> str:
        return self.value


class ToolDomain(str, Enum):
    """
    工具应执行的位置：编排器进程或容器内部。

    编排器工具在主代理进程中运行（内存访问、作业管理等）。
    容器工具在 Docker 容器内部运行（shell、文件操作、代码修改）。
    """
    # 在编排器中安全运行（纯函数、内存、作业管理）
    Orchestrator = "orchestrator"

    # 必须在沙箱容器内运行（文件系统、shell、代码）
    Container = "container"


class EngineVersion(str, Enum):
    """引擎版本枚举。"""
    V1 = "v1"
    V2 = "v2"


class EngineCompatibility(str, Enum):
    """
    工具在哪些引擎版本中可用。

    由每个工具通过 Tool::engine_compatibility() 声明。
    工具默认为 Both；版本特定的工具进行覆盖。
    """
    # 在 v1（遗留代理循环）和 v2（引擎线程）中均可用
    # 对应 Rust: Both
    Both = "both"

    # 仅在 v1（遗留代理循环）中可用
    V1Only = "v1_only"

    # 仅在 v2（引擎线程/能力）中可用
    V2Only = "v2_only"

    def is_visible_in(self, version: EngineVersion) -> bool:
        """
        具有此兼容性的工具在给定引擎版本中是否可见。
        """
        if self == EngineCompatibility.Both:
            return True
        elif self == EngineCompatibility.V1Only:
            return version == EngineVersion.V1
        elif self == EngineCompatibility.V2Only:
            return version == EngineVersion.V2
        return False


class ToolError(Exception):
    """
    工具执行的错误类型。

    使用 Exception 子类实现各变体，保留 Rust 枚举的模式匹配语义。
    """
    pass


class InvalidParameters(Exception):
    """无效参数。对应 Rust: ToolError::InvalidParameters(String)"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(f"无效参数: {message}")


class ExecutionFailed(ToolError):
    """执行失败。对应 Rust: ToolError::ExecutionFailed(String)"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(f"执行失败: {message}")


class Timeout(ToolError):
    """超时。对应 Rust: ToolError::Timeout(Duration)"""

    def __init__(self, duration: timedelta):
        self.duration = duration
        super().__init__(f"超时: {duration}")


class NotAuthorized(ToolError):
    """未授权。对应 Rust: ToolError::NotAuthorized(String)"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(f"未授权: {message}")


class RateLimited(ToolError):
    """速率限制。对应 Rust: ToolError::RateLimited(Option<Duration>)"""

    def __init__(self, retry_after: Optional[timedelta] = None):
        self.retry_after = retry_after
        if retry_after is not None:
            super().__init__(f"速率限制，请在 {retry_after} 后重试")
        else:
            super().__init__("速率限制")


class ExternalService(ToolError):
    """外部服务错误。对应 Rust: ToolError::ExternalService(String)"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(f"外部服务错误: {message}")


class Sandbox(ToolError):
    """沙箱错误。对应 Rust: ToolError::Sandbox(String)"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(f"沙箱错误: {message}")


class ToolErrorType:
    InvalidParameters = InvalidParameters
    ExecutionFailed = ExecutionFailed
    Timeout = Timeout
    NotAuthorized = NotAuthorized
    RateLimited = RateLimited
    ExternalService = ExternalService
    Sandbox = Sandbox


@dataclass
class ToolOutput:
    """
    工具执行的输出。
    """
    # 结果数据
    result: Any
    # 所花费的时间
    duration: timedelta
    # 产生的成本（如果有）
    cost: Optional[Decimal] = None
    # 清理前的原始输出（用于调试）
    raw: Optional[str] = None

    @classmethod
    def success(cls, result: Any, duration: timedelta) -> "ToolOutput":
        """
        创建一个带有 JSON 结果的成功输出。

        对应 Rust:
        pub fn success(result: Value, duration: Duration) -> Self
        """
        return cls(result=result, duration=duration)

    @classmethod
    def text(cls, text: str, duration: timedelta) -> "ToolOutput":
        """
        创建一个文本输出。

        对应 Rust:
        pub fn text(text: impl Into<String>, duration: Duration) -> Self
        """
        return cls(result=text, duration=duration)

    def with_cost(self, cost: Decimal) -> "ToolOutput":
        """
        设置成本。

        对应 Rust:
        pub fn with_cost(mut self, cost: Decimal) -> Self
        """
        self.cost = cost
        return self

    def with_raw(self, raw: str) -> "ToolOutput":
        """
        设置原始输出。

        对应 Rust:
        pub fn with_raw(mut self, raw: impl Into<String>) -> Self
        """
        self.raw = raw
        return self


@dataclass
class ToolSchema:
    """
    工具的模式定义。
    """
    # 工具名称
    name: str
    # 工具描述
    description: str

    # 参数的 JSON Schema
    parameters: Any = field(default_factory=lambda: {
        "type": "object",
        "properties": {},
        "required": [],
    })

    def with_parameters(self, parameters: Any) -> "ToolSchema":
        """
        设置参数模式。
        """
        self.parameters = parameters
        return self


@dataclass
class ToolDiscoverySummary:
    """
    由 tool_info(detail: "summary") 展示的精选发现指导。
    """
    # 始终需要的条件列表
    always_required: List[str] = field(default_factory=list)

    # 有条件的需求列表
    conditional_requirements: List[str] = field(default_factory=list)

    # 备注列表
    notes: List[str] = field(default_factory=list)

    # 示例列表
    examples: List[Any] = field(default_factory=list)


class ToolRuntimeAffordance(str, Enum):
    """
    工具在给定的运行时策略下暴露所需的运行时权限。

    这是一个**可见性**过滤器 —— 它仅控制工具是否出现在面向模型的工具列表中。
    动作时授权（能力授予、审批、资源检查）仍然在每次调用时运行，无论可见性如何。

    默认为 ToolRuntimeAffordance::None —— 不依赖任何特定运行时权限的工具在所有策略下都显示。
    需要提供商托管 shell、主机工作空间文件系统或直接（非代理）网络出口的工具必须声明这一点，
    以便在已解析的策略无法授予底层权限时将其隐藏。

    参见 EffectiveRuntimePolicy 和 is_visible_under 以了解匹配的过滤逻辑。
    """
    # 在任何运行时策略下都可见。默认值。
    NONE = "None"

    # 需要除 `None` 以外的进程后端——`Docker`、
    # `Srt`、`SmolVm`、`LocalHost`、`TenantSandbox`、`OrgDedicatedRunner`中的任何一个均可满足。
    # 纯虚拟的 `process_backend == None` 配置会向模型隐藏 shell 类工具。
    AnyProcess = "AnyProcess"

    # 明确要求使用提供商主机 Shell。
    # 仅在 `process_backend == LocalHost` 时可见。
    # 托管多租户和企业专有配置永远不会选择 `LocalHost`，因此此能力实际上仅限本地单用户使用。
    LocalShell = "LocalShell"

    # 要求使用主机工作区文件系统。
    # 仅在 `filesystem_backend == HostWorkspace` 时可见。
    # 与 `LocalShell` 具有相同的本地化语义。
    HostFilesystem = "HostFilesystem"

    # 要求直接（非代理）网络出站。
    # 仅在 `network_mode in {Direct, DirectLogged}` 时可见。
    DirectNetwork = "DirectNetwork"


class Tool(ABC):
    """
    代理可以使用的工具的 trait。
    """

    # ---------- 抽象方法（子类必须实现）----------

    @property
    @abstractmethod
    def name(self) -> str:
        """
        获取工具名称。
        """
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """
        获取工具功能的描述。
        """
        pass

    @property
    @abstractmethod
    def parameters_schema(self) -> Any:
        """
        获取工具参数的 JSON Schema。
        """
        pass

    @abstractmethod
    async def execute(self, params: Any, ctx: JobContext) -> "ToolOutput":
        """
        使用给定参数执行工具。
        """
        pass

    # ---------- 可选方法（提供默认实现）----------

    def estimated_cost(self, params: Any) -> Optional[Decimal]:
        """
        使用给定参数运行此工具的估计成本。
        """
        return None

    def estimated_duration(self, params: Any) -> Optional[timedelta]:
        """
        使用给定参数运行此工具的估计耗时。
        """
        return None

    @property
    def requires_sanitization(self) -> bool:
        """
        此工具的输出是否需要清理。

        对于与外部服务交互的工具返回 true，
        因为这些工具的输出可能包含恶意内容。
        """
        return True

    def risk_level_for(self, params: Any) -> RiskLevel:
        """
        此工具特定调用的风险级别。

        默认为 Low（只读、安全）。对于风险取决于参数的工具进行覆盖 ——
        shell 工具根据命令字符串将命令分类为 Low / Medium / High。

        工作器在每次工具调用时记录此值，以便操作员可以审计每次执行的分类风险级别。
        """
        return RiskLevel.Low

    def requires_approval(self, params: Any) -> ApprovalRequirement:
        """
        此工具调用是否需要用户审批。

        默认返回 Never（大多数工具在沙箱环境中运行）。
        对于需要审批但可以会话自动批准的工具，覆盖返回 UnlessAutoApproved；
        对于必须始终提示的调用返回 Always（例如破坏性 shell 命令、带认证的 HTTP）。
        """
        return ApprovalRequirement.Never

    @property
    def execution_timeout(self) -> timedelta:
        """
        此工具在调用者终止之前允许运行的最长时间。
        对于长时间运行的工具（如沙箱执行）进行覆盖。
        默认：60 秒。
        """
        return timedelta(seconds=60)

    @property
    def domain(self) -> ToolDomain:
        """
        此工具应在何处执行。

        Orchestrator 工具在主代理进程中运行（安全，无 FS 访问）。
        Container 工具在 Docker 容器内部运行（shell、文件操作）。
        """
        return ToolDomain.Orchestrator

    @property
    def engine_compatibility(self) -> EngineCompatibility:
        """
        此工具在哪些引擎版本中可用。

        默认：Both。对于在 v2 中被引擎原生能力替代的工具覆盖为 V1Only
        （例如 routine_create → mission_create），或对于无法在 v2 中
        由 LLM 调用的工具（例如没有交互式审批路径的 ApprovalRequirement::Always 工具）。

        """
        return EngineCompatibility.Both

    @property
    def runtime_affordance(self) -> ToolRuntimeAffordance:
        """
        此工具在给定运行时策略下可见所需的运行时权限。

        默认为 ToolRuntimeAffordance::None —— 在每个策略下都可见。
        依赖提供商托管 shell、主机工作空间文件系统或直接网络出口的工具应覆盖此方法，
        以便在已解析策略无法授予底层权限时对模型隐藏。
        动作时授权仍然在每次调用时运行；这是一个 UX/可见性过滤器，而非授权门控。
        """
        return ToolRuntimeAffordance.NONE

    @property
    def sensitive_params(self) -> List[str]:
        """
        在记录、钩子和审批之前必须对其值进行脱敏的参数名称。

        代理框架在以下操作之前将这些参数值替换为 "[REDACTED]"：
        - 写入调试日志
        - 存储到 ActionRecord（内存中的作业历史）
        - 记录到 TurnToolCall（会话状态）
        - 发送到 BeforeToolCall 钩子
        - 在审批 UI 中显示

        execute() 方法仍然接收原始的、未脱敏的参数。
        脱敏仅适用于可观察性和审计路径，不适用于执行。

        用于接受明文密钥作为参数的工具（例如 secret_save）。
        """
        return []

    @property
    def rate_limit_config(self) -> Optional[ToolRateLimitConfig]:
        """
        此工具每次调用的速率限制。

        返回 Some(config) 以限制每个用户调用此工具的频率。
        只读工具（echo、time、json、file_read、memory_search 等）应返回 None。
        写入/外部工具（shell、http、file_write、memory_write、create_job）应返回合理的限制以防止代理失控。

        速率限制是按用户、按工具的，并且是内存中的（重启时重置）。
        这与 requires_approval() 正交 —— 工具可以同时有审批门控和速率限制。速率限制先检查（更廉价）。

        默认：None（无速率限制）。
        """
        return None

    @property
    def webhook_capability(self) -> Optional[WebhookCapability]:
        """
        此工具的可选主机端 webhook 验证配置。

        当存在时，/webhook/tools/{tool} 在调用工具之前验证共享密钥/签名。
        然后工具应仅处理负载规范化。
        """
        return None

    @property
    def discovery_schema(self) -> Any:
        """
        用于发现和强制转换的完整参数模式。

        与 parameters_schema()（可能放宽以保持工具数组紧凑）不同，这返回完整的类型化模式。
        由 tool_info 内置工具和 WASM 参数强制转换使用。

        默认：委托给 parameters_schema()。
        """
        return self.parameters_schema

    @property
    def discovery_summary(self) -> Optional[ToolDiscoverySummary]:
        """
        tool_info(detail: "summary") 使用的精选发现指导。

        默认：无自定义摘要；调用者可以从 discovery_schema() 派生最小回退。
        """
        return None

    @property
    def provider_extension(self) -> Optional[str]:
        """
        拥有此操作的规范提供商扩展（如果存在）。

        这允许运行时解析 操作 -> 提供商扩展 而无需从操作名称推断所有权。
        MCP 子工具应报告服务器扩展名称，扩展支持的 WASM 工具应报告其扩展 ID。
        """
        return None

    @property
    def required_credentials(self) -> List[str]:
        """
        此工具运行所需的密钥存储凭据名称。

        返回工具声明的每个非可选凭据的 secret_name
        （例如 WASM 工具的 capabilities.http.credentials）。
        引擎的认证预检（AuthManager::check_action_auth）查询此列表，
        如果任何声明的凭据在密钥存储中缺失，则引发认证门控，
        以便模型可以直接调用工具 —— 无需单独的启用步骤。

        默认返回空 —— 不需要凭据或内部处理缺失凭据的内置工具仅在相关时覆盖。
        """
        return []

    @property
    def schema(self) -> ToolSchema:
        """
        获取用于 LLM 函数调用的工具模式。
        """
        parameters = self.parameters_schema
        has_discovery_hint = (
                self.discovery_summary is not None
                or self.discovery_schema != parameters
        )
        if has_discovery_hint:
            description = (
                f"{self.description} "
                f"（调用 tool_info(name=\"{self.name}\", detail=\"summary\") 获取规则/示例，"
                f"或使用 detail=\"schema\" 获取完整的发现模式）"
            )
        else:
            description = self.description

        return ToolSchema(
            name=self.name,
            description=description,
            parameters=parameters,
        )


# ---------- 辅助函数 ----------

def require_str(params: Dict[str, Any], name: str) -> str:
    """
    从 JSON 对象中提取必需的字符串参数。

    如果键缺失或不是字符串，返回 ToolError::InvalidParameters。
    """
    value = params.get(name)
    if value is None or not isinstance(value, str):
        raise ToolErrorType.InvalidParameters(f"缺少 '{name}' 参数")
    return value


def require_param(params: Dict[str, Any], name: str) -> Any:
    """
    从 JSON 对象中提取任意类型的必需参数。

    如果键缺失，返回 ToolError::InvalidParameters。
    """
    value = params.get(name)
    if value is None:
        raise ToolErrorType.InvalidParameters(f"缺少 '{name}' 参数")
    return value


def check_approval_in_context(
        ctx: JobContext,
        tool_name: str,
        requirement: ApprovalRequirement,
) -> None:
    """
    根据作业的审批上下文检查工具调用是否被允许。

    此辅助函数应由执行子工具的工具（如构建器）调用，
    以确保即使在绕过工作器的正常审批流程时也能进行适当的审批检查。

    如果工具被允许，返回 None；如果被阻止，抛出 ToolError::NotAuthorized。

    # 安全语义

    当 approval_context 为 None 时，此函数使用**遗留阻止行为**：
    - Never 工具：允许
    - UnlessAutoApproved 工具：阻止（需要交互式审批）
    - Always 工具：阻止（需要显式审批）

    这与工作器级别的 ApprovalContext::is_blocked_or_default() 语义匹配，
    以防止权限提升。

    参数:
        ctx: 作业上下文。
        tool_name: 工具名称。
        requirement: 审批要求。

    抛出:
        NotAuthorized: 如果工具在此上下文中被阻止。
    """
    # 与工作器级别审批语义精确匹配，防止不一致
    if ApprovalContext.is_blocked_or_default(ctx.approval_context, tool_name, requirement):
        raise ToolErrorType.NotAuthorized(
            f"工具 '{tool_name}' 在此上下文中需要审批"
        )


def redact_params(params: Dict[str, Any], sensitive: List[str]) -> Dict[str, Any]:
    """
    将敏感参数值替换为 "[REDACTED]"。

    返回一个新的字典，其中指定的键被替换。非对象参数和未知键原样传递。
    仅当有敏感参数需要脱敏时才进行深拷贝。

    代理框架在记录日志、钩子分发、审批显示和 ActionRecord 存储之前使用此函数，
    以便明文密钥永远不会到达这些路径。

    对应 Rust:
    pub fn redact_params(params: &Value, sensitive: &[&str]) -> Value

    参数:
        params: 工具参数的字典。
        sensitive: 需要脱敏的参数名称列表。

    返回:
        脱敏后的参数字典。
    """
    # 如果没有敏感参数，直接返回克隆
    # 对应 Rust: if sensitive.is_empty() { return params.clone(); }
    if not sensitive:
        return dict(params)

    # 深拷贝参数并进行脱敏
    # 对应 Rust: let mut redacted = params.clone(); if let Some(obj) = redacted.as_object_mut() { ... }
    redacted = dict(params)
    for key in sensitive:
        if key in redacted:
            redacted[key] = "[REDACTED]"

    return redacted


def has_object_combinator_variants(schema: Dict[str, Any]) -> bool:
    """
    检查模式是否使用了 oneOf、anyOf 或 allOf 组合器，
    其中至少有一个变体是对象类型（具有 type: "object" 或 properties）。

    对应 Rust:
    fn has_object_combinator_variants(schema: &Value) -> bool
    """
    for key in ["oneOf", "anyOf", "allOf"]:
        variants = schema.get(key)
        if isinstance(variants, list):
            for v in variants:
                if isinstance(v, dict):
                    if v.get("type") == "object" or "properties" in v:
                        return True
    return False


def validate_tool_schema(schema: Dict[str, Any], path: str = "") -> List[str]:
    """
    对工具的 parameters_schema() 进行宽松的运行时验证。

    在工具注册时使用此函数以捕获结构性错误（缺少 "type": "object"、
    孤立的 "required" 键、缺少 "items" 的数组），而不拒绝有意的自由形式属性。

    返回验证错误列表。空列表表示模式有效。

    # 强制执行的规则

    1. 顶层必须有 "type": "object"
    2. 顶层必须有 "properties" 作为对象
    3. "required" 中的每个键必须存在于 "properties" 中
    4. 嵌套对象递归遵循相同规则
    5. 数组属性应定义 "items"

    没有 "type" 字段的属性是允许的（自由形式/任意类型）。

    对应 Rust:
    pub fn validate_tool_schema(schema: &Value, path: &str) -> Vec<String>
    """
    return validate_tool_schema_inner(schema, path, 0)


def validate_tool_schema_inner(schema: Dict[str, Any], path: str, depth: int) -> List[str]:
    """
    递归验证工具模式的内部函数。

    对应 Rust:
    fn validate_tool_schema_inner(schema: &Value, path: &str, depth: usize) -> Vec<String>
    """
    errors = []

    # 检查嵌套深度是否超过最大值
    # 对应 Rust: if depth > MAX_SCHEMA_DEPTH { errors.push(...); return errors; }
    if depth > MAX_SCHEMA_DEPTH:
        errors.append(f"{path}: 模式嵌套超过最大深度 {MAX_SCHEMA_DEPTH}")
        return errors

    # 将非数组组合器值报告为错误
    # 对应 Rust: for key in ["oneOf", "anyOf", "allOf"] { if let Some(val) = ... }
    for key in ["oneOf", "anyOf", "allOf"]:
        val = schema.get(key)
        if val is not None and not isinstance(val, list):
            errors.append(f"{path}: \"{key}\" 必须是数组")

    has_combinators = has_object_combinator_variants(schema)

    # 规则 1：此级别必须有 "type": "object"（除非组合器定义了结构）
    # 对应 Rust: match schema.get("type").and_then(|t| t.as_str()) { ... }
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        if schema_type == "object":
            pass
        else:
            errors.append(f"{path}: 期望类型 \"object\"，实际为 \"{schema_type}\"")
            return errors  # 无法继续检查
    else:
        if not has_combinators:
            errors.append(f"{path}: 缺少 \"type\": \"object\"")
            return errors

    # 递归验证组合器变体
    # 对应 Rust: for key in ["allOf", "oneOf", "anyOf"] { ... }
    for key in ["allOf", "oneOf", "anyOf"]:
        variants = schema.get(key)
        if isinstance(variants, list):
            for i, variant in enumerate(variants):
                if isinstance(variant, dict):
                    if variant.get("type") == "object" or "properties" in variant:
                        variant_path = f"{path}.{key}[{i}]"
                        errors.extend(validate_tool_schema_inner(variant, variant_path, depth + 1))

    # 规则 2：必须有 "properties" 作为对象（除非组合器定义了它们）
    # 对应 Rust: let properties = match schema.get("properties").and_then(|p| p.as_object()) { ... }
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        if not has_combinators:
            errors.append(f"{path}: 缺少或非对象的 \"properties\"")
            return errors

        # 组合器定义了结构 —— 根据所有组合器变体的合并属性验证顶层 required 键
        # 对应 Rust: if let Some(required) = schema.get("required").and_then(|r| r.as_array()) { ... }
        required = schema.get("required")
        if isinstance(required, list):
            merged_keys = set()

            all_of = schema.get("allOf")
            if isinstance(all_of, list):
                for variant in all_of:
                    if isinstance(variant, dict):
                        props = variant.get("properties")
                        if isinstance(props, dict):
                            merged_keys.update(props.keys())

            for key_name in ["oneOf", "anyOf"]:
                variants_list = schema.get(key_name)
                if isinstance(variants_list, list):
                    for variant in variants_list:
                        if isinstance(variant, dict):
                            props = variant.get("properties")
                            if isinstance(props, dict):
                                merged_keys.update(props.keys())

            for req in required:
                if isinstance(req, str) and req not in merged_keys:
                    errors.append(
                        f"{path}: 必需键 \"{req}\" 未在任何组合器变体的 properties 中找到"
                    )

        return errors

    # 规则 3："required" 中的每个键必须存在于 "properties" 中
    # 对应 Rust: if let Some(required) = schema.get("required").and_then(|r| r.as_array()) { ... }
    required = schema.get("required")
    if isinstance(required, list):
        for req in required:
            if isinstance(req, str) and req not in properties:
                errors.append(f"{path}: 必需键 \"{req}\" 未在 properties 中找到")

    # 规则 4 和 5：递归进入嵌套对象并检查数组
    # 对应 Rust: for (key, prop) in properties { ... }
    for key, prop in properties.items():
        prop_path = f"{path}.{key}"
        if isinstance(prop, dict):
            prop_type = prop.get("type")
            if isinstance(prop_type, str):
                if prop_type == "object":
                    # 递归验证嵌套对象
                    errors.extend(validate_tool_schema_inner(prop, prop_path, depth + 1))
                elif prop_type == "array":
                    items = prop.get("items")
                    if isinstance(items, dict):
                        # 如果 items 是对象类型，递归验证
                        if items.get("type") == "object":
                            errors.extend(
                                validate_tool_schema_inner(items, f"{prop_path}.items", depth + 1)
                            )
                    elif items is None:
                        errors.append(f"{prop_path}: 数组属性缺少 \"items\"")
        # 没有 "type" 字段是故意允许的（自由形式属性）

    return errors
