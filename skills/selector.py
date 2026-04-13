from typing import List, Optional, Any
import logging
import regex as re

logger = logging.getLogger(__name__)


def score_skill(skill: LoadedSkill, message_lower: str, message_original: str):
    """
    根据用户消息对技能进行评分。
    """
    # 排除性否决：如果消息中包含任何排除关键词，则得分为 0
    if any([excl in message_lower for excl in skill.lowercased_exclude_keywords]):
        return 0

    score = 0
    # 带上限的关键词评分，防止通过堆砌关键词来操纵评分
    keyword_score = 0
    # 去除单词收尾的特殊符合
    msg_words = [re.sub(r'^\W+|\W+$', '', word) for word in message_lower.split()]

    for kw_lower in skill.lowercased_keywords:
        # 精确单词匹配（由单词边界界定）
        if any([word == kw_lower for word in msg_words]):
            keyword_score += 10
        elif kw_lower in message_lower:
            keyword_score += 5

    score += min(keyword_score, MAX_KEYWORD_SCORE)
    # 根据 activation.tags 进行标签评分
    tag_score = 0
    for tag_lower in skill.lowercased_tags:
        if tag_lower in message_lower:
            tag_score += 3

    score += min(tag_score, MAX_TAG_SCORE)
    # 使用预编译的正则表达式模式进行评分（在加载时缓存），并带有上限。
    regex_score = 0
    for p in skill.compiled_patterns:
        if p.match(message_original):
            regex_score += 20
    score += min(regex_score, MAX_REGEX_SCORE)

    return score




def prefilter_skills(
        message: str,
        available_skills: LoadedSkill,
        max_candidates: int,
        max_context_tokens: int
):
    """
    使用确定性评分机制，为给定的消息选择候选技能。

    返回按得分排序的技能列表（得分最高的在前），数量受 `max_candidates` 和总上下文预算的限制。
    此选择过程不涉及大语言模型。
    """
    if not available_skills or not message:
        return []

    message_lower = message.lower()

    scored: List[ScoredSkill] = []
    for skill in available_skills:
        score = score_skill(skill, message_lower, message)
        if score > 0:
            scored.append(ScoredSkill(skill, score))

    # 按得分降序排序
    scored.sort(key=lambda x: x.score, reverse=True)

    # 应用候选数量限制和上下文预算
    result = []
    for entry in scored:
        if len(result) >= max_candidates:
            break

        declared_tokens = entry.skill.manifest.activation.max_context_tokens
        # 粗略的令牌估算：每字节约 0.25 个令牌（英文散文每个令牌约对应 4 个字节）
        approx_tokens = approx_tokens = int(len(entry.skill.prompt_content) * 0.25)

        raw_cost = declared_tokens
        if approx_tokens > declared_tokens * 2:
            logger.warning(
                f"技能 '{entry.skill.name}' 声明的 max_context_tokens={declared_tokens}，但提示词大约为 {approx_tokens} 个令牌；将使用实际估算值")
            raw_cost = approx_tokens

        # 强制执行最低令牌成本，防止 max_context_tokens=0 绕过预算限制
        token_cost = token_cost = max(raw_cost, 1)
        if token_cost <= max_context_tokens:
            max_context_tokens -= token_cost
            result.append(entry.skill)

    return result
