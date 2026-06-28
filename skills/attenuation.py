#! 基于信任的工具过滤（权限衰减）。
# !
# ! **仅 V1** —— 当 v1 代理（`src/agent/`）被删除时移除此模块。
# !
# ! 在 v2 中，Python 编排器通过 `format_skills()` 处理技能信任，
# ! 策略引擎通过能力租约处理工具访问。此模块仅从 `src/agent/dispatcher.rs` 调用。
# !
# ! 核心防御机制：任何活跃技能的最低信任级别决定了*工具上限* ——
# ! 高于上限的工具会完全从 LLM 的工具列表中移除。
# ! LLM 无法被操纵去调用它不知道存在的工具。
# !
# ! | 信任状态              | 工具上限                                          |
# ! |-----------------------|---------------------------------------------------|
# ! | 无活跃技能            | 所有工具（正常行为）                              |
# ! | 仅受信任的            | 所有工具（用户放置的这些，完全信任）              |
# ! | 已安装存在            | 仅只读工具                                        |

# 假设以下类型已从对应模块导入
from ironclaw_llm import ToolDefinition
from ironclaw_skills import LoadedSkill, SkillTrust
from dataclasses import dataclass, field
from typing import List

# 始终安全的工具 —— 只读，无副作用。
#
# **维护说明**：此列表是有意硬编码且保守的。
# 向 IronClaw 添加新工具时，它们默认*排除*在只读列表之外
# （即，在 Installed 上限下被阻止）。只有被证明无副作用的工具才应添加到此列表中 ——
# 它不得写入文件、发起网络请求、执行命令或修改任何状态。
# 扩展此列表前需要安全团队审查。

READ_ONLY_TOOLS = [
    "memory_search",
    "memory_read",
    "memory_tree",
    "time",
    "echo",
    "json",
    "skill_list",
    "skill_search",
]


@dataclass
class AttenuationResult:
    """
    工具衰减的结果，包含透明度信息。
    """
    # 发送给 LLM 的过滤后的工具定义。
    tools: List[ToolDefinition]
    # 所有活跃技能中的最低信任级别。
    min_trust: str  # SkillTrust 值
    # 对移除内容及原因的人类可读解释。
    explanation: str
    # 被移除的工具名称列表。
    removed_tools: List[str] = field(default_factory=list)


def attenuate_tools(
        tools: List[ToolDefinition],
        active_skills: List[LoadedSkill],
) -> AttenuationResult:
    """
    根据活跃技能的信任级别过滤工具定义。

    这是硬安全门：高于信任上限的工具在到达 LLM 之前从工具列表中移除。
    LLM 无法调用它不知道存在的工具，无论技能提示如何指示。
    """
    # 无活跃技能 = 无衰减
    if not active_skills:
        return AttenuationResult(
            tools=tools,
            min_trust=SkillTrust.Trusted,
            explanation="无活跃技能，所有工具可用",
            removed_tools=[],
        )

    # 计算所有活跃技能中的最低信任级别
    min_trust = min(
        (skill.trust for skill in active_skills),
        default=SkillTrust.Trusted,
    )

    if min_trust == SkillTrust.Trusted:
        # 受信任的技能具有完全信任 —— 无需过滤
        return AttenuationResult(
            tools=list(tools),
            min_trust=min_trust,
            explanation="所有活跃技能都是受信任的（完全信任），所有工具可用",
            removed_tools=[],
        )

    elif min_trust == SkillTrust.Installed:
        # 已安装：仅限只读工具
        kept = []
        removed = []

        for tool in tools:
            if tool.name in READ_ONLY_TOOLS:
                kept.append(tool)  # 假设 tool 可直接使用，无需 clone
            else:
                removed.append(tool.name)

        explanation = (
            f"存在已安装技能：限制为只读工具，移除了 {len(removed)} 个工具: {', '.join(removed)}"
        )

        return AttenuationResult(
            tools=kept,
            min_trust=min_trust,
            explanation=explanation,
            removed_tools=removed,
        )
