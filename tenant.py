from dataclasses import dataclass
from typing import Optional


# ---------- 假设的外部类型定义 ----------

class UserId:
    """用户标识符（对应 crate::ownership::UserId）。"""
    pass


class TenantScope:
    """租户范围的数据库访问（对应 TenantScope）。"""
    pass


class Workspace:
    """工作空间（对应 Workspace）。"""
    pass


class CostGuard:
    """成本守卫（对应 CostGuard）。"""
    pass


class TenantRateState:
    """租户速率限制状态（对应 TenantRateState）。"""
    pass


# ---------- TenantCtx 数据类 ----------

@dataclass
class TenantCtx:
    """
    按请求的租户执行上下文。

    捆绑了 [`TenantScope`]（范围内的数据库访问）、工作空间、成本守卫和
    按租户的速率限制。每个请求通过
    [`AgentDeps::tenant_ctx()`] 构造一次。

    `Clone + Send + Sync` —— 可以安全地存储在 `ChatDelegate` 上而不会产生生命周期问题。

    对应 Rust:
    #[derive(Clone)]
    pub struct TenantCtx {
        identity: crate::ownership::UserId,
        store: Option<TenantScope>,
        workspace: Option<Arc<Workspace>>,
        cost_guard: Arc<CostGuard>,
        rate: Arc<TenantRateState>,
    }

    Attributes:
        identity: 用户身份标识符。
        store: 可选的租户范围数据库访问。
        workspace: 可选的工作空间。
        cost_guard: 成本守卫，用于跟踪和限制 API 使用成本。
        rate: 租户速率限制状态。
    """
    identity: UserId
    store: Optional[TenantScope] = None
    workspace: Optional[Workspace] = None   # Python 中无需 Arc，直接持有引用
    cost_guard: Optional[CostGuard] = None   # Python 中无需 Arc
    rate: Optional[TenantRateState] = None   # Python 中无需 Arc


class SystemScope:
    pass