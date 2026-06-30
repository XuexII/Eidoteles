# 上下文检索引擎。
#
# 通过从项目中检索相关记忆文档，为线程步骤构建上下文。
# 使用关键词匹配文档标题+内容，并根据文档类型进行优先级评分（在上下文注入中，经验教训和规范的优先级高于摘要）。

import logging
import re
from dataclasses import dataclass
from typing import List

from ..types.memory import DocType, MemoryDoc
from ..types.project import ProjectId

logger = logging.getLogger(__name__)

# ── 停用词列表 ───────────────────────────────────────────────

STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "about", "it",
    "its", "this", "that", "these", "those", "i", "you", "he", "she",
    "we", "they", "what", "which", "who", "how", "when", "where", "why",
    "and", "or", "but", "not", "no", "if", "then", "so", "up", "out", "just",
}


# ── 检索引擎 ─────────────────────────────────────────────────

@dataclass
class RetrievalEngine:
    """检索线程上下文的相关记忆文档"""
    store: Store

    async def retrieve_context(
            self,
            project_id: ProjectId,
            user_id: str,
            query: str,
            max_docs: int,
    ) -> List[MemoryDoc]:
        """检索项目中给定查询的相关记忆文档

        加载项目的所有文档，按关键词相关性和文档类型优先级评分，
        并返回前 `max_docs` 个结果
        """
        if max_docs == 0:
            return []

        # 包括用户拥有的和共享的系统文档用于上下文检索
        all_docs = await self.store.list_memory_docs_with_shared(project_id, user_id)
        if not all_docs:
            return []

        keywords = extract_keywords(query)
        if not keywords:
            # 没有有意义的关键词 — 仅按文档类型优先级返回
            scored = [(doc_type_weight(doc.doc_type), doc) for doc in all_docs]
            scored.sort(key=lambda x: x[0], reverse=True)
            scored = scored[:max_docs]
            return [doc for _, doc in scored]

        # 按关键词相关性 + 类型优先级评分
        scored = []
        for doc in all_docs:
            keyword_score = keyword_match_score(doc, keywords)
            type_weight = doc_type_weight(doc.doc_type)
            # 综合评分：关键词相关性（0.0-1.0）+ 类型优先级奖励
            score = keyword_score + type_weight
            if score > 0.0:
                scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        scored = scored[:max_docs]
        return [doc for _, doc in scored]


# ── 辅助函数 ─────────────────────────────────────────────────

def extract_keywords(query: str) -> List[str]:
    """从查询中提取小写关键词，过滤掉停用词"""
    # 分割非字母数字字符（保留 _ 和 -）
    words = re.findall(r'[a-zA-Z0-9_\-]+', query.lower())

    return [
        w for w in words
        if len(w) >= 2 and w not in STOP_WORDS
    ]


def keyword_match_score(doc: MemoryDoc, keywords: List[str]) -> float:
    """评分文档与给定关键词的匹配程度（0.0 到 1.0）"""
    if not keywords:
        return 0.0

    title_lower = doc.title.lower()
    content_lower = doc.content.lower()

    matched = 0
    for kw in keywords:
        # 标题匹配权重更高
        if kw in title_lower:
            matched += 2
        elif kw in content_lower:
            matched += 1

    # 标准化：最大可能分数是 keywords.len() * 2（全部在标题中）
    max_score = len(keywords) * 2
    return matched / max_score if max_score > 0 else 0.0


def doc_type_weight(doc_type: DocType) -> float:
    """按文档类型的优先级权重。越高 = 对上下文注入越有用"""
    weights = {
        DocType.Spec: 0.5,  # 缺失的能力信息优先级最高
        DocType.Skill: 0.45,  # 带有激活元数据和代码片段的技能
        DocType.Lesson: 0.4,  # 经验教训防止重复错误
        DocType.Issue: 0.2,  # 已知问题
        DocType.Summary: 0.1,  # 背景上下文
        DocType.Note: 0.05,  # 草稿笔记，最低优先级
        DocType.Plan: 0.3,  # 带有结构化步骤的执行计划
    }
    return weights.get(doc_type, 0.0)
