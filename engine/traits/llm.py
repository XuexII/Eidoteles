# 大语言模型后端特质。
#
# 引擎对语言模型提供商的抽象。刻意比主 crate 的 `LlmProvider` 更简单——引擎只需要发起补全调用。
# 成本跟踪、缓存、重试和熔断是宿主关注的事项，由桥接适配器处理。

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from ..types.capability import ActionDef
from ..types.error import EngineError
from ..types.message import ThreadMessage
from ..types.step import LlmResponse, TokenUsage


@dataclass
class LlmCallConfig:
    """单次 LLM 调用的配置"""
    # 要生成的最大 token 数
    max_tokens: Optional[int] = None
    # 采样温度
    temperature: Optional[float] = None
    # 为 true 时，LLM 不应返回动作调用
    force_text: bool = False
    # 递归调用树中的深度（0 = 根，1+ = 子调用）。
    # 实现可以使用此来为子调用路由到更便宜的模型
    depth: int = 0
    # 可选的每次调用模型覆盖。设置时，桥接适配器通过
    # `CompletionRequest::model` 将此转发到底层 `LlmProvider`。
    # 不支持每次请求覆盖的提供者将回退到其配置的模型并记录警告
    model: Optional[str] = None
    # 转发到 LLM 提供者的不透明元数据
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class LlmOutput:
    """单次 LLM 调用的输出"""
    response: LlmResponse
    usage: TokenUsage


class LlmBackend(ABC):
    """语言模型提供者的抽象

    主 crate 通过包装其 `LlmProvider` trait 来实现此接口，
    在 `ThreadMessage` 和 `ChatMessage` 之间进行转换
    """

    @abstractmethod
    async def complete(
            self,
            messages: List[ThreadMessage],
            actions: List[ActionDef],
            config: LlmCallConfig,
    ) -> LlmOutput:
        """使用对话消息和可用的动作定义调用 LLM

        返回文本响应或一组动作调用
        """
        ...

    @abstractmethod
    def model_name(self) -> str:
        """模型标识符（例如 "gpt-4"、"claude-opus-4-20250514"）"""
        ...