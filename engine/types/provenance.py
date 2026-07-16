"""
Provenance——数据来源追踪标记

每个数据值都可以标记其来源
策略引擎在效果边界使用provenance来执行基于污点的安全规则，例如对LLM生成的数据执行金融效果时需要额外审批
"""

from dataclasses import dataclass


class UserProvenance:
    """
    用户输入
    """
    pass


class SystemProvenance:
    """系统提示、配置"""
    pass


@dataclass
class ToolOutputProvenance:
    """能力动作的结果"""
    action_name: str


class LlmGeneratedProvenance:
    """由 LLM 生成"""
    pass


@dataclass
class MemoryRetrievalProvenance:
    """从项目记忆中检索"""
    doc_id: str


Provenance = UserProvenance | SystemProvenance | ToolOutputProvenance | LlmGeneratedProvenance | MemoryRetrievalProvenance
