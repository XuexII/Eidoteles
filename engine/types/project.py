"""
project——上下文隔离单元

用于限定memory、threads和missions的作用域。
每个Project提供独立的上下文边界，确保不同任务或用户的数据和执行逻辑相互隔离
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from ironclaw_common.common import OwnerId, slugify_simple


# ── 项目指标 ─────────────────────────────────────────────────

@dataclass
class ProjectMetric:
    """项目内的跟踪指标

    指标将项目目标与可测量的数字联系起来。`evaluation` 字段告诉
    代理*如何*获取当前值（例如，API 调用、shell 命令、要读取的文件）
    """
    # 人类可读的指标名称（例如 "月收入"）
    name: str
    # 测量单位（例如 "USD"、"users"、"%"）
    unit: str = ""
    # 要达到的目标值
    target: Optional[float] = None
    # 当前测量值
    current: Optional[float] = None
    # 如何测量此指标 — 代理获取当前值所遵循的指令
    # （例如 "查询 Stripe API /v1/balance"、"在用户数据库上运行 `wc -l`"、
    # "读取 projects/acme/kpis.json"）
    evaluation: str = ""
    # `current` 值的最后更新时间
    updated_at: Optional[datetime] = None


# ── 项目标识符 ───────────────────────────────────────────────

# 从 (user_id, slug) 派生的项目 ID 的稳定 v5 命名空间。
# 修改此值意味着每个用户的项目 ID 都会变化，因此一旦发布就绝不能更改
_PROJECT_ID_NAMESPACE = uuid.UUID("6f1f3c5a-4f2e-4ba4-9f3a-1c7e3c4f5a10")

# ── 项目 ─────────────────────────────────────────────────────
def generate_project_id(user_id: str, slug: str) -> str:
    if not user_id or not slug:
        return str(uuid.uuid4())
    seed = f"{user_id}:{slug}"
    return str(uuid.uuid5(_PROJECT_ID_NAMESPACE, seed))


@dataclass(kw_only=True)
class Project:
    """项目 — 上下文作用域单元"""
    _id: str = field(init=False)
    # 项目所有者，用于租户隔
    user_id: str
    # 项目名称，用于生成slug和目录路径
    name: str
    # 项目描述，说明项目用途
    description: str
    # 项目目标，定义项目的长期目标
    goals: List[str] = field(default_factory=list)
    # 项目指标，跟踪项目进展和性能
    metrics: List[ProjectMetric] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    # 可选的覆盖值，用于绑定到此项目沙箱中 `/project/` 的主机文件系统目录。
    # 当为 None 时，主机计算默认路径（参见 bridge 的 `project_workspace_path` 辅助函数）。
    # engine crate 有意只存储覆盖值而不存储已解析的默认值，
    # 因为解析默认值依赖于主机的基础目录（`~/.ironclaw`），而该目录位于此 crate 之外
    workspace_path: Optional[Path] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        slug = slugify_simple(self.name)
        object._id = generate_project_id(self.user_id, slug)

    @property
    def id(self):
        return self._id

    def with_workspace_path(self, path: Path):
        """为此项目的 `/project/` 挂载点设置显式的主机文件系统路径，
        """
        self.workspace_path = path

    @property
    def owner_id(self) -> OwnerId:
        """获取项目所有者的 ID"""
        return OwnerId.from_user_id(self.user_id)

    def is_owned_by(self, user_id: str) -> bool:
        """检查项目是否属于指定用户"""
        return self.owner_id.matches_user(user_id)
