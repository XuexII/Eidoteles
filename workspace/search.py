"""
结合全文搜索和语义搜索的混合搜索。

支持两种融合策略：
1. **RRF**（倒数排名融合）—— 默认的基于排名的方法。
   `score = sum(1 / (k + rank))` 对每种检索方法计算。
2. **WeightedScore** —— 通过 `1/rank` 将排名转换为分数，结合
   可配置的权重（`fts_weight * fts_score + vector_weight * vector_score`），
   然后通过除以最大组合分数来归一化到 \[0,1\]。

两种策略都结合来自以下途径的结果：
- PostgreSQL / libSQL 全文搜索
- pgvector / libsql_vector 余弦相似度搜索
"""

from dataclasses import dataclass
from enum import Enum


class FusionStrategy(Enum):
    """用于融合 FTS 和向量搜索结果的策略"""
    # 倒数排名融合（默认）。忽略 `fts_weight`/`vector_weight`
    Rrf = "Rrf"
    # 使用归一化排名衍生分数的加权分数融合
    WeightedScore = "WeightedScore"


@dataclass
class SearchConfig:
    """混合搜索的配置"""
    # 返回的最大结果数
    limit: int = 10
    # RRF 常数（通常为 60）。较高的值更倾向于顶部结果
    rrf_k: int = 60
    # 是否包含 FTS 结果
    use_fts: bool = True
    # 是否包含向量结果
    use_vector: bool = True
    # 最低分数阈值（0.0-1.0）
    min_score: float = 0.0
    # 融合之前从每种方法获取的最大结果数
    pre_fusion_limit: int = 50
    # 组合结果时使用的融合策略
    fusion_strategy: FusionStrategy = FusionStrategy.Rrf
    # `WeightedScore` 融合中 FTS 结果的权重（默认 0.5）。
    # `Rrf` 融合忽略此值。对于通过 `WorkspaceSearchConfig::resolve` 的基于环境变量的配置，
    # 默认值按策略不同
    fts_weight: float = 0.5
    # `WeightedScore` 融合中向量结果的权重（默认 0.5）。
    # `Rrf` 融合忽略此值。对于通过 `WorkspaceSearchConfig::resolve` 的基于环境变量的配置，
    # 默认值按策略不同
    vector_weight: float = 0.5

    def with_fusion_strategy(self, strategy: FusionStrategy) -> "SearchConfig":
        """设置融合策略"""
        self.fusion_strategy = strategy
        return self

    def with_rrf_k(self, k: int) -> "SearchConfig":
        """设置 RRF 常数"""
        self.rrf_k = k
        return self

    def with_fts_weight(self, weight: float) -> "SearchConfig":
        """设置 FTS 权重"""
        self.fts_weight = weight
        return self

    def with_vector_weight(self, weight: float) -> "SearchConfig":
        """设置向量权重"""
        self.vector_weight = weight
        return self
