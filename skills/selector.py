# 用于两阶段技能筛选的确定性前置过滤器
#
# 技能筛选的第一阶段完全基于确定性规则，不调用大模型，
# 上下文也不会带入任何技能正文内容。该设计可避免循环操控问题：
# 防止已加载的技能反过来干扰待筛选技能的选取逻辑。
#
# 打分规则：
# - 关键词完全匹配：10 分（单项总分上限30分）
# - 关键词子串匹配：5 分（单项总分上限30分）
# - 标签匹配：3 分（单项总分上限15分）
# - 正则表达式匹配：20 分（单项总分上限40分）

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Set, Any

from .types import LoadedSkill

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────

# 分配给技能的默认最大上下文 token 数
MAX_SKILL_CONTEXT_TOKENS = 4000

# 每个技能的最大关键词分数上限，防止通过关键词堆砌进行游戏化。
# 即使一个技能有 20 个关键词，它最多也只能获得这么多关键词分数
MAX_KEYWORD_SCORE = 30

# 每个技能的最大标签分数上限（与关键词上限平行）
MAX_TAG_SCORE = 15

# 每个技能的最大正则表达式模式分数上限。没有上限时，5 个模式
# 每个 20 分可能产生 100 分，主导关键词+标签分数
MAX_REGEX_SCORE = 40

# 正则表达式模式运行的最大消息长度（以字节为单位）。
# 超过此长度的消息跳过正则表达式评分，以避免在热路径上对每个技能进行 O(n) 的工作
# （regex 库是线性的，但常数在大规模时很重要）
MAX_REGEX_MATCH_MESSAGE_BYTES = 64 * 1024


# ── 数据结构 ─────────────────────────────────────────────────

@dataclass
class ScoredSkill:
    """带有分数信息的预过滤结果"""
    skill: LoadedSkill
    score: int


@dataclass
class SelectionOutcome:
    """单次选择遍历的结果，带有关于非明显决策的人类可读注释

    `notes` 旨在向用户展示 — 例如解释为什么链式加载了伴随技能
    或为什么某个技能被预算丢弃。并非每个选择决策都被注释；
    我们追求的是信号而非噪音，因此"未评分"等常规结果不产生注释
    """
    selected: List[LoadedSkill] = field(default_factory=list)  # List[LoadedSkill]
    notes: List[str] = field(default_factory=list)


@dataclass
class SkillSelectionOptions:
    """确定性技能预过滤的选择策略"""
    regex_activation_enabled: bool = True


class TrySelectOutcome(Enum):
    """try_select 调用未添加技能的原因。调用者使用此来渲染不同的注释
    （预算 vs 标记 vs 重复），而不是将它们混为一个不透明的"已跳过"
    """
    Selected = "Selected"
    AlreadySelected = "AlreadySelected"
    CandidateLimit = "CandidateLimit"
    MarkerSatisfied = "MarkerSatisfied"
    BudgetFull = "BudgetFull"


# ── 技能 token 成本估算 ──────────────────────────────────────

def skill_token_cost(skill: Any) -> int:  # skill: LoadedSkill
    """估算将技能提示加载到 LLM 上下文的 token 成本

    优先使用声明的 `max_context_tokens`，但如果声明相对于提示内容
    低得不合理，则回退到基于实际长度的估算（并警告）。
    强制最小 1 token，以便 `max_context_tokens: 0` 声明不能绕过预算
    """
    declared_tokens = skill.manifest.activation.max_context_tokens
    # 粗略的 token 估算：每字节约 0.25 个 token（英语散文每 token 约 4 字节）
    approx_tokens = int(len(skill.prompt_content) * 0.25)

    if approx_tokens > declared_tokens * 2:
        logger.warning(
            f"技能 '{skill.manifest.name}' 声明 max_context_tokens={declared_tokens} "
            f"但提示约 {approx_tokens} tokens；使用实际估算"
        )
        raw_cost = approx_tokens
    else:
        raw_cost = declared_tokens

    return max(raw_cost, 1)


# ── 技能选择 ─────────────────────────────────────────────────

def try_select(
        skill: Any,  # LoadedSkill
        result: List[Any],
        selected_names: Set[str],
        budget_remaining: List[int],  # 使用单元素列表以允许修改
        max_candidates: int,
        satisfied_setup_markers: Set[str],
) -> TrySelectOutcome:
    """尝试将技能添加到选中集合，失败时返回具体原因

    在评分选择循环和链式加载循环之间共享
    """
    if len(result) >= max_candidates:
        return TrySelectOutcome.CandidateLimit

    name = skill.manifest.name
    if name in selected_names:
        return TrySelectOutcome.AlreadySelected

    # 即使对于链式加载的伴随技能也尊重标记排除：如果伴随技能
    # 的设置已经完成，它在当前轮次中没有任何贡献
    marker = getattr(skill.manifest.activation, 'setup_marker', None)
    if marker is not None and marker in satisfied_setup_markers:
        return TrySelectOutcome.MarkerSatisfied

    cost = skill_token_cost(skill)
    if cost > budget_remaining[0]:
        return TrySelectOutcome.BudgetFull

    budget_remaining[0] -= cost
    selected_names.add(name)
    result.append(skill)
    return TrySelectOutcome.Selected


def prefilter_skills_with_options(
        message: str,
        available_skills: List[Any],  # List[LoadedSkill]
        max_candidates: int,
        max_context_tokens: int,
        satisfied_setup_markers: Set[str],
        options: SkillSelectionOptions,
) -> SelectionOutcome:
    """使用确定性评分选择给定消息的候选技能

    返回按分数排序的技能（最高优先），受 `max_candidates` 和
    总上下文预算限制。此选择不涉及 LLM

    ## 通过 `requires.skills` 的链式加载

    当技能按分数被选中时，其 `requires.skills` 伴随技能也会被拉入
    （如果可用），**绕过评分过滤器** — 它们依赖于父级的选择。
    这使得像 `developer-setup` 这样的人物/捆绑技能按设计工作：编排器
    声明它委托给哪些操作技能，选择编排器会自动加载它们。
    链式加载是非传递的（深度 1）；链式加载的伴随技能不会加载
    自己的伴随技能，以保持行为可预测

    链式加载的伴随技能仍然从同一预算中消耗并尊重 `max_candidates`。
    如果剩余预算无法容纳伴随技能，它会被静默跳过并记录调试日志 —
    父技能仍然被选中。具有已满足 `setup_marker` 的伴随技能也会被跳过
    （它们的工作已经完成）

    ## 设置标记排除

    `satisfied_setup_markers` 是一次性设置技能的工作区路径集合。
    任何 `activation.setup_marker` 在此集合中的技能无论分数如何都被排除 —
    它的设置已经完成，没有事情可做。调用者（`agent_loop::select_active_skills`）
    负责通过检查工作区中加载技能引用的每个不同标记来计算此集合

    传递空集合以禁用标记过滤（旧行为：每个技能无论工作区状态如何都参与竞争）
    """
    if not available_skills or not message:
        return SelectionOutcome()

    message_lower = message.lower()

    # 构建名称 → 技能查找表以用于链式加载伴随技能解析
    by_name = {s.manifest.name: s for s in available_skills}

    # 评分所有技能
    scored = []
    for skill in available_skills:
        # 设置标记排除：一次性设置技能如果其标记文件已存在于工作区中，
        # 其工作已完成。完全跳过评分，使其无法消耗预算
        marker = getattr(skill.manifest.activation, 'setup_marker', None)
        if marker is not None and marker in satisfied_setup_markers:
            continue

        score = score_skill(skill, message_lower, message, options)
        if score > 0:
            scored.append(ScoredSkill(skill=skill, score=score))

    # 按分数降序排序
    scored.sort(key=lambda x: x.score, reverse=True)

    # 应用候选限制和上下文预算
    result = []
    selected_names = set()
    budget_remaining = [max_context_tokens]  # 使用列表以允许在 _try_select 中修改
    notes = []

    for entry in scored:
        # 首先尝试选择父技能
        parent_outcome = try_select(
            entry.skill, result, selected_names, budget_remaining,
            max_candidates, satisfied_setup_markers,
        )

        if parent_outcome == TrySelectOutcome.BudgetFull:
            notes.append(
                f"{entry.skill.manifest.name}: 已跳过（技能上下文预算耗尽）"
            )
            # 父技能放不下 — 不链式加载伴随技能
            continue
        elif parent_outcome == TrySelectOutcome.CandidateLimit:
            # 预算/槽位耗尽；完全停止考虑更多候选者（它们也放不下）
            break
        elif parent_outcome in (TrySelectOutcome.AlreadySelected, TrySelectOutcome.MarkerSatisfied):
            # 已选择/标记已满足在此处静默：
            # 评分循环不应看到重复名称，标记过滤已在评分时发生。无注释
            continue

        # 链式加载 requires.skills 中声明的伴随技能。
        # 非传递：伴随技能不加载自己的伴随技能
        requires = getattr(entry.skill.manifest, 'requires', None)
        companion_names = getattr(requires, 'skills', []) if requires else []

        for companion_name in companion_names:
            companion = by_name.get(companion_name)
            if companion is None:
                # 已列出但未加载 — 静默忽略。人物捆绑声明可选的伴随技能
                continue

            outcome = try_select(
                companion, result, selected_names, budget_remaining,
                max_candidates, satisfied_setup_markers,
            )

            if outcome == TrySelectOutcome.Selected:
                notes.append(
                    f"{companion_name}: 从 {entry.skill.manifest.name} 链式加载"
                )
            elif outcome == TrySelectOutcome.BudgetFull:
                notes.append(
                    f"{companion_name}: 链式加载已跳过（预算已满）"
                )
            elif outcome == TrySelectOutcome.CandidateLimit:
                notes.append(
                    f"{companion_name}: 链式加载已跳过（达到最大活跃技能数）"
                )
            elif outcome == TrySelectOutcome.MarkerSatisfied:
                notes.append(
                    f"{companion_name}: 链式加载已跳过（设置已完成）"
                )
            # 跨父级的重复伴随技能是可以的 — 无注释
            # TrySelectOutcome.AlreadySelected → 静默

    return SelectionOutcome(selected=result, notes=notes)


def score_skill(
        skill: Any,  # LoadedSkill
        message_lower: str,
        message_original: str,
        options: SkillSelectionOptions,
) -> int:
    """根据用户消息对技能评分"""
    # 排除否决：如果消息中存在任何 exclude_keyword，得分为 0
    exclude_keywords = getattr(skill, 'lowercased_exclude_keywords', [])
    if any(excl in message_lower for excl in exclude_keywords):
        return 0

    score = 0

    # 关键词评分，带有上限以防止通过关键词堆砌进行游戏化
    keyword_score = 0
    lowercased_keywords = getattr(skill, 'lowercased_keywords', [])
    # 将消息分割为单词（去除标点符号）
    message_words = [
        word.strip(''.join(c for c in word if not c.isalnum()))
        for word in message_lower.split()
    ]

    for kw_lower in lowercased_keywords:
        # 精确单词匹配（由单词边界包围）
        if kw_lower in message_words:
            keyword_score += 10
        elif kw_lower in message_lower:
            # 子字符串匹配
            keyword_score += 5

    score += min(keyword_score, MAX_KEYWORD_SCORE)

    # 标签评分（来自 activation.tags）
    tag_score = 0
    lowercased_tags = getattr(skill, 'lowercased_tags', [])
    for tag_lower in lowercased_tags:
        if tag_lower in message_lower:
            tag_score += 3
    score += min(tag_score, MAX_TAG_SCORE)

    # 正则表达式模式评分（使用加载时缓存的预编译模式），带有上限
    if options.regex_activation_enabled and len(message_original.encode('utf-8')) <= MAX_REGEX_MATCH_MESSAGE_BYTES:
        regex_score = 0
        compiled_patterns = getattr(skill, 'compiled_patterns', [])
        for re_obj in compiled_patterns:
            if re_obj.search(message_original):
                regex_score += 20
        score += min(regex_score, MAX_REGEX_SCORE)

    return score


# ── 技能提及提取 ─────────────────────────────────────────────

def extract_skill_mentions(
        message: str,
        available_skills: List[Any],  # List[LoadedSkill]
) -> tuple[list[LoadedSkill], str]:
    """从消息中提取显式的 `/skill-name` 提及

    用户可以在消息中任何位置写入 `/github` 或 `/file-issues` 来
    强制激活技能。返回匹配的技能和一个重写的消息，
    其中每个 `/skill-name` 被替换为技能描述（以便对 LLM 来说句子仍然读起来自然）

    示例：`"从 /github 获取问题"` 对于名为 `github` 的技能
    （描述 "GitHub API"）→ 重写为 `"从 GitHub API 获取问题"`，
    并且 github 技能被强制包含
    """
    matched = []
    rewritten = message

    # 构建名称→技能查找表（不区分大小写）
    skill_map = {s.manifest.name.lower(): s for s in available_skills}

    # 找到匹配技能名称的 /word 模式。从末尾扫描以避免替换时索引偏移
    replacements = []
    # 匹配 /skill-name 模式，skill-name 只能包含 [a-zA-Z0-9._-]
    pattern = re.compile(r'(?:^|(?<=[\s"(\[\n]))\/([a-zA-Z0-9._-]+)')

    for match in pattern.finditer(message):
        name = match.group(1)
        lookup = name.lower()
        if lookup in skill_map:
            skill = skill_map[lookup]
            replacement = (
                name.replace('-', ' ') if not skill.manifest.description
                else skill.manifest.description
            )
            replacements.append((match.start(), match.end(), replacement))
            if not any(s.manifest.name == skill.manifest.name for s in matched):
                matched.append(skill)

    # 以相反顺序应用替换以保留索引
    for start, end, replacement in reversed(replacements):
        rewritten = rewritten[:start] + replacement + rewritten[end:]

    return matched, rewritten


def is_skill_mention_boundary(previous: str) -> bool:
    """检查前一个字符是否是技能提及的边界"""
    return previous in (' ', '\n', '\t', '"', '(', '[') or not previous.isascii()


# ── 置信度因子 ───────────────────────────────────────────────

def apply_confidence_factor(base_score: int, confidence: float, is_authored: bool) -> int:
    """将置信度因子应用于基础分数

    编写的技能始终获得因子 1.0（无调整）。
    提取的技能获得 `0.5 + 0.5 * confidence`，因此 0% 置信度的技能
    其分数减半（不是归零 — 在强关键词匹配时仍可被选中）
    """
    if is_authored:
        return base_score

    factor = 0.5 + 0.5 * max(0.0, min(1.0, confidence))
    return int(base_score * factor)
