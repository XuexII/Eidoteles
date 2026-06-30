# 工具层级分类。
#
# 根据每个动作声明的效果和批准要求，将其映射到特权层级。
# 由 [`LeasePlanner`] 用于限定线程类型感知的租约，并由 [`LeaseGate`] 用于授权检查。
#
# [`LeasePlanner`]: crate::capability::planner::LeasePlanner
# [`LeaseGate`]: （未来实现）


from enum import Enum

from ..types.capability import ActionDef, EffectType

# ── 自主工具拒绝列表 ─────────────────────────────────────────

# 自主工具拒绝列表中的工具动作 — 无论其声明的效果如何，
# 这些始终被分类为 [`ToolTier::Administrative`]
AUTONOMOUS_TOOL_DENYLIST = [
    "routine_create",
    "routine_update",
    "routine_delete",
    "routine_fire",
    "event_emit",
    "create_job",
    "job_prompt",
    "restart",
    "tool_install",
    "tool_auth",
    "tool_remove",
    "tool_upgrade",
    "skill_install",
    "skill_remove",
    "secret_list",
    "secret_delete",
]


def is_autonomous_denylisted(action_name: str) -> bool:
    """如果动作名称在自主工具拒绝列表中，则返回 True"""
    return action_name in AUTONOMOUS_TOOL_DENYLIST


# ── 工具层级 ─────────────────────────────────────────────────

class ToolTier(Enum):
    """工具动作的权限层级

    层级完全有序：`ReadOnly < Stateful < Privileged < Administrative`。
    [`LeasePlanner`] 使用此排序来决定为每个 [`ThreadType`] 授予哪些动作

    [`ThreadType`]: crate::types::thread::ThreadType
    """
    # 只读，无副作用（echo、time、json、memory_search、memory_read）
    ReadOnly = 1
    # 创建或读取本地状态（read_file、list_dir）
    Stateful = 2
    # 写操作或外部效果（shell、file_write、http、create_job）
    Privileged = 3
    # 永远不应自主运行的系统级操作
    # （routine_*、tool_install、skill_*、secret_*、restart）
    Administrative = 4

    def __lt__(self, other: "ToolTier") -> bool:
        return self.value < other.value

    def __le__(self, other: "ToolTier") -> bool:
        return self.value <= other.value

    def __gt__(self, other: "ToolTier") -> bool:
        return self.value > other.value

    def __ge__(self, other: "ToolTier") -> bool:
        return self.value >= other.value


def classify_tool_tier(action: ActionDef) -> ToolTier:
    """将工具动作分类为 [`ToolTier`]

    分类规则（按优先级顺序）：
    1. 动作名称在 [`AUTONOMOUS_TOOL_DENYLIST`] 中 → `Administrative`
    2. `requires_approval: true` → `Privileged`
    3. 仅 `ReadLocal` / `Compute` 效果 → `ReadOnly`
    4. 其他一切 → `Stateful`
    """
    # 1. 拒绝列表中的 → Administrative
    if is_autonomous_denylisted(action.name):
        return ToolTier.Administrative

    # 2. 需要批准 → Privileged
    if action.requires_approval:
        return ToolTier.Privileged

    # 3. 仅读取/计算效果 → ReadOnly
    if action.effects and all(
            e in (EffectType.ReadLocal, EffectType.Compute) for e in action.effects
    ):
        return ToolTier.ReadOnly

    # 4. 默认
    return ToolTier.Stateful