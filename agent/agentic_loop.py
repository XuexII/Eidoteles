from agent.session import PendingApproval
from llm import ChatMessage, Reasoning, ReasoningContext, RespondResult
from enum import Enum, auto
from typing import TypedDict, ClassVar, Optional, Dict, List, Tuple, Union
from dataclasses import dataclass, field
import logging
import abc

logger = logging.getLogger(__name__)


# 来自委托的信号，指示循环应如何继续执行。
class LoopSignal(str, Enum):
    # 正常继续
    Continue = auto()
    # 优雅地停止循环。
    Stop = auto()
    # 将用户消息注入上下文并继续执行。
    InjectMessage = auto()


# 大语言模型返回文本响应的结果。
class TextAction(str, Enum):
    # 将此作为循环的最终结果返回。
    Return = auto()
    # 继续循环（文本已处理，但循环应继续执行）。
    Continue = auto()


# 智能体循环的最终结果。
class LoopOutcome(str, Enum):
    Response = auto()
    # 某个信号停止
    Stopped = auto()
    # 超过最大迭代次数。
    MaxIterations = auto()
    # 某个工具需要用户批准后才能继续执行（仅限聊天委托）。
    NeedApproval = auto()


@dataclass
class AgenticLoopConfig:
    # 最大迭代次数
    max_iterations: int = 50
    # 是否启用工具意图提示
    enable_tool_intent_nudge: bool = True
    # 最大工具意图提示次数
    max_tool_intent_nudges: int = 2


# ========== LoopDelegate 抽象基类 ==========
class LoopDelegate(abc.ABC):
    """
    策略 trait —— 每个消费者实现此 trait 以自定义 I/O 和生命周期。
    共享循环会在明确定义的点调用这些方法。消费者只实现不同上下文
    （聊天、作业、容器）之间差异化的行为。循环本身处理公共逻辑：
    工具意图提示、迭代计数、工具定义刷新以及响应→执行→处理循环。

    注意：Rust 要求 Send + Sync，在 Python 中无需显式标注，但应确保实现
    是线程安全的（例如避免共享可变状态而不加锁）。
    """

    @abc.abstractmethod
    async def check_signals(self) -> LoopSignal:
        """
        每次迭代开始时调用。检查外部信号（取消、用户消息、停止请求）。
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def before_llm_call(
            self,
            reason_ctx: ReasoningContext,
            iteration: int,
    ) -> Optional[LoopOutcome]:
        """
        在调用 LLM 之前调用。允许委托刷新工具定义、执行成本保护或注入消息。
        返回 Some(outcome) 可提前中断循环。
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def call_llm(
            self,
            reasoning: Reasoning,
            reason_ctx: ReasoningContext,
            iteration: int,
    ) -> object:  # 实际应为 Result[crate::llm::RespondOutput, Error]
        """
        调用 LLM 并返回结果。委托拥有 LLM 调用权，以处理消费者特定的关注点
        （速率限制、自动压缩、成本跟踪、强制文本模式）。
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def handle_text_response(
            self,
            text: str,
            reason_ctx: ReasoningContext,
    ) -> TextAction:
        """
        处理来自 LLM 的纯文本响应。
        返回 TextAction.RETURN 退出循环，返回 TextAction.CONTINUE 继续。
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def execute_tool_calls(
            self,
            tool_calls: List[object],  # 实际应为 Vec<crate::llm::ToolCall>
            content: Optional[str],
            reason_ctx: ReasoningContext,
    ) -> Optional[LoopOutcome]:
        """
        执行工具调用并将结果添加到上下文中。
        返回 Some(outcome) 可中断循环（例如需要批准）。
        """
        raise NotImplementedError

    async def on_tool_intent_nudge(self, text: str, reason_ctx: ReasoningContext) -> None:
        """
        当 LLM 表达了工具意图但实际没有调用工具时调用。
        委托可使用此方法发出事件或记录提示以便观察。
        默认实现为空。
        """
        pass

    async def after_iteration(self, iteration: int) -> None:
        """
        每次成功迭代（无错误、无提前返回）之后调用。
        默认实现为空。
        """
        pass


async def run_agentic_loop(
        delegate: LoopDelegate,
        reasoning: Reasoning,
        reason_ctx: ReasoningContext,
        config: AgenticLoopConfig
):
    """
    运行统一的智能体循环。y

    这是所有三类消费者（聊天、作业、容器）使用的唯一实现。
    delegate` 通过 `LoopDelegate` 特质提供消费者特定的行为。
    """
    consecutive_tool_intent_nudges = 0

    for iteration in range(1, config.max_iterations + 1):
        # 检查外部信号（停止、取消、用户消息）。
        signal = await delegate.check_signals()
        if signal == LoopSignal.Continue:
            pass
        elif signal == LoopSignal.Stop:
            return LoopOutcome.Stopped
        elif signal == LoopSignal.InjectMessage:
            reason_ctx.messages.append()

        # 大语言模型调用前钩子（成本守卫、工具刷新、迭代限制提示）。
        if outcome := await delegate.before_llm_call(reason_ctx, iteration):
            return outcome

        # 调用llm
        output = await delegate.call_llm(reasoning, reason_ctx, iteration)

        if output == RespondResult.Text:
            text = output.text
            logger.info(
                f"LLM text response: iteration={iteration}, len={len(text)}, "
                f"has_suggestions={'<suggestions>' in text}, response={text}"
            )
        elif output == RespondResult.ToolCalls:
            names = []
            content = output.content
            logger.info(f"LLM tool_calls response:iteration={iteration}, tools={names}, "
                        f"has_content={content}")

        # 判断输出结果
        await delegate.after_iteration(iteration)

    # 超过最大调用次数
    return LoopOutcome.MaxIterations
