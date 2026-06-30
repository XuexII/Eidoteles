# 数据流分析的来源追踪。
#
# 每个数据值都可以标记其来源。策略引擎在效果边界处使用来源信息来执行基于污点的安全规则。
# 第 1 阶段：仅定义类型；策略执行将在第 4 阶段实现。


from dataclasses import dataclass

from .memory import DocId


class Provenance:
    """数据的来源"""
    pass


@dataclass
class User(Provenance):
    """直接用户输入"""
    pass


@dataclass
class System(Provenance):
    """系统提示、配置"""
    pass


@dataclass
class ToolOutput(Provenance):
    """能力动作的结果"""
    action_name: str


@dataclass
class LlmGenerated(Provenance):
    """由 LLM 生成"""
    pass


@dataclass
class MemoryRetrieval(Provenance):
    """从项目记忆中检索"""
    doc_id: DocId
