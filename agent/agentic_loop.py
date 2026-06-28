from __future__ import annotations
from llm import (
    ChatMessage,
    FinishReason,
    Reasoning,
    ReasoningContext,
    RespondResult,
    RespondOutput,
    ResponseMetadata,
    ToolCall,
    llm_signals_tool_intent,
    TOOL_INTENT_NUDGE,
    TRUNCATED_TOOL_CALL_NOTICE
)
import abc
import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, List
from agent.session import PendingApproval
import hashlib
import json
from util import canonicalize_json_value

logger = logging.getLogger(__name__)


# 来自委托的信号，指示循环应如何继续执行。
@dataclass
class Continue:
    """正常继续"""
    pass


@dataclass
class Stop:
    """优雅地停止循环"""
    pass


@dataclass
class InjectMessage:
    """将用户消息注入上下文并继续执行"""
    message: str


LoopSignal = Continue | Stop | InjectMessage


# 大语言模型返回文本响应的结果。
@dataclass
class Return:
    """将此作为循环的最终结果返回。"""
    outcome: LoopOutcome


@dataclass
class Continue:
    """继续循环（文本已处理，但循环应继续执行）。"""
    pass


TextAction = Return | Continue


# 智能体循环的最终结果。
@dataclass
class Response:
    """
    完成并返回文本响应。
    """
    text: str


@dataclass
class Stopped:
    """
    循环被信号停止。
    """
    pass


@dataclass
class MaxIterations:
    """
    超出最大迭代次数。
    """
    pass


@dataclass
class Failure:
    """
    循环提前终止，并给出明确的失败原因。
    """
    reason: str


@dataclass
class NeedApproval:
    """
    某个工具在继续之前需要用户批准（仅限聊天委托）。
    """
    pending_approval: PendingApproval


@dataclass
class AuthPending:
    """
    认证流程已启动 —— 配置卡片已发送，抑制文本响应。
    """
    message: str


LoopOutcome = Response | Stopped | MaxIterations | Failure | NeedApproval | AuthPending


@dataclass
class AgenticLoopConfig:
    # 最大迭代次数
    max_iterations: int = 50
    # 是否启用工具意图提示
    enable_tool_intent_nudge: bool = True
    # 最大工具意图提示次数
    max_tool_intent_nudges: int = 2


# ========== LoopDelegate 抽象基类 ==========
@dataclass
class LoopDelegate(abc.ABC):
    """
    策略 trait —— 每个消费者实现此 trait 以自定义 I/O 和生命周期。
    共享循环会在明确定义的点调用这些方法。消费者只实现不同上下文
    （聊天、作业、容器）之间差异化的行为。循环本身处理公共逻辑：
    工具意图提示、迭代计数、工具定义刷新以及响应→执行→处理循环。

    聊天场景策略: 为聊天会话定制共享的 agentic loop 行为。
        管理会话和线程状态（持有 Session 和 thread_id）
        处理工具批准流程（requires_approval 工具会暂停循环）
        跟踪 token 使用和成本
        注入技能上下文
        实现完整的 3 阶段工具执行（preflight → 并行执行 → post-flight）
        处理用户中断信号
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
    ) -> RespondOutput:
        """
        调用 LLM 并返回结果。委托拥有 LLM 调用权，以处理消费者特定的关注点
        （速率限制、自动压缩、成本跟踪、强制文本模式）。
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def handle_text_response(
            self,
            text: str,
            metadata: ResponseMetadata,
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
            tool_calls: List[ToolCall],
            content: Optional[str],
            reason_ctx: ReasoningContext,
            reasoning: Optional[str]
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


@dataclass
class DuplicateToolCallTracker:
    """
    追踪连续失败且完全相同的工具调用，并在累积后升级处理。

    通过对批次中每次调用的 `(tool_name, canonicalized_args)` 进行哈希计算，为每批工具调用生成指纹。
    如果该指纹与上一批匹配，且批次中所有工具均返回错误结果，则连续失败计数器递增。
    当大语言模型调用不同的工具、任一工具成功或返回文本响应时，计数器重置。
    """
    # 指纹
    last_fingerprint: Optional[int] = None
    # 连续计数
    consecutive_count: int = 0

    @staticmethod
    def fingerprint(tool_calls: List[ToolCall]) -> int:
        """
        计算一批工具调用的指纹。

        使用工具名称和规范化的参数 JSON 来生成一致的哈希值，
        使得相同工具名称和参数的调用产生相同的指纹。

        参数:
            tool_calls: 要计算指纹的工具调用列表。

        返回:
            int: 该批工具调用的 64 位哈希指纹。
        """
        hasher = hashlib.sha256()

        for tc in tool_calls:
            # 哈希工具名称
            # 对应 Rust: tc.name.hash(&mut hasher);
            hasher.update(tc.name.encode('utf-8'))

            # 规范化参数 JSON 并哈希
            canonical = canonicalize_json_value(tc.arguments)
            hasher.update(json.dumps(canonical, sort_keys=True).encode('utf-8'))

        # 返回 64 位整数指纹（取 SHA-256 的前 8 字节）
        return int.from_bytes(hasher.digest()[:8], byteorder='big')

    def record_with_fingerprint(self, fp: int, all_failed: bool) -> int:
        """
        记录预计算的指纹以及是否所有工具都失败了。
        返回此次更新后当前的连续重复失败计数。

        参数:
            fp: 当前批次的指纹。
            all_failed: 是否批次中的所有工具都失败了。

        返回:
            int: 更新后的连续重复失败计数。
        """
        if all_failed and self.last_fingerprint == fp:
            # 相同指纹且全部失败 —— 增加连续计数
            self.consecutive_count += 1
        elif all_failed:
            # 不同的工具调用但全部失败 —— 开始新的连续计数
            self.last_fingerprint = fp
            self.consecutive_count = 1
        else:
            # 至少有一个工具成功 —— 重置
            self.reset()
            self.last_fingerprint = fp

        return self.consecutive_count

    def reset(self) -> None:
        """
        当 LLM 产生文本响应或成功调用不同工具时重置。
        """
        self.last_fingerprint = None
        self.consecutive_count = 0


async def run_agentic_loop(
        delegate: LoopDelegate,
        reasoning: Reasoning,
        reason_ctx: ReasoningContext,
        config: AgenticLoopConfig
):
    """
    运行统一的智能体循环。

    这是所有三类消费者（聊天、作业、容器）使用的唯一实现。
    delegate` 通过 `LoopDelegate` 特质提供消费者特定的行为。
    """
    consecutive_tool_intent_nudges = 0
    # 跨所有迭代累积计数（不会被文本响应重置），
    # 因此非连续的截断仍会逐步升级为强制文本输出。
    truncation_count = 0

    # 创建重复工具调用跟踪器
    dup_tracker = DuplicateToolCallTracker(0)

    for iteration in range(1, config.max_iterations + 1):
        # 检查外部信号（停止、取消、用户消息）。
        match await delegate.check_signals():
            case Continue():
                pass
            case Stop():
                return Stopped()
            case InjectMessage(msg):
                reason_ctx.messages.append(ChatMessage.user(msg))

        # 大语言模型调用前钩子（成本守卫、工具刷新、迭代限制提示）。
        if outcome := await delegate.before_llm_call(reason_ctx, iteration):
            return outcome

        # 调用llm
        output = await delegate.call_llm(reasoning, reason_ctx, iteration)

        match output.result:
            case RespondResult.Text(text):
                logger.debug(
                    f"LLM text response: iteration={iteration}, len={len(text)}, "
                    f"has_suggestions={'<suggestions>' in text}, response={text}"
                )
                # 工具意图提示：如果 LLM 说 "let me search..." 但实际上没有调用工具，
                # 则注入一条提示消息。
                if (
                        config.enable_tool_intent_nudge
                        and reason_ctx.available_tools  # 列表非空
                        and not reason_ctx.force_text
                        and consecutive_tool_intent_nudges < config.max_tool_intent_nudges
                        and llm_signals_tool_intent(text)
                ):
                    consecutive_tool_intent_nudges += 1
                    logger.info(
                        "迭代 %d: LLM 表达了工具意图但未调用工具，正在提示",
                        iteration,
                    )

                    # 通知委托进行工具意图提示
                    await delegate.on_tool_intent_nudge(text, reason_ctx)

                    # 将助手消息和提示消息添加到上下文中
                    reason_ctx.messages.append(ChatMessage.assistant(text))
                    reason_ctx.messages.append(ChatMessage.user(TOOL_INTENT_NUDGE))

                    # 执行迭代后回调
                    # 对应 Rust: delegate.after_iteration(iteration).await;
                    await delegate.after_iteration(iteration)

                    # 继续下一次迭代
                    continue

                # 收到非工具意图的文本响应，重置提示计数器
                if not llm_signals_tool_intent(text):
                    consecutive_tool_intent_nudges = 0

                # 文本响应会打破任何重复工具调用的连续性。
                dup_tracker.reset()

                # 处理文本响应
                action = await delegate.handle_text_response(text, output.metadata, reason_ctx)
                match action:
                    case Return(outcome):
                        # 返回指定的结果
                        return outcome
                    case Continue():
                        # 继续循环
                        pass

            case RespondResult.ToolCalls(tool_calls, content, reasoning):
                names = [tc.name for tc in tool_calls]
                logger.debug(f"LLM tool_calls response:iteration={iteration}, tools={names}, "
                             f"has_content={content}")
                # 如果响应被截断，工具调用参数很可能不完整。
                # 丢弃它们并告诉 LLM 尝试不同的方法，而不是执行格式错误的工具调用。
                if output.finish_reason == FinishReason.Length:
                    truncation_count += 1
                    names = [tc.name for tc in tool_calls]
                    logger.warning(
                        "迭代 %d: 丢弃被截断的工具调用（finish_reason=Length）: tools=%s, truncation_count=%d",
                        iteration,
                        names,
                        truncation_count,
                    )

                    # 如果有文本内容，添加助手消息
                    if content is not None:
                        reason_ctx.messages.append(ChatMessage.assistant(content))

                    # 添加截断通知消息
                    reason_ctx.messages.append(ChatMessage.user(TRUNCATED_TOOL_CALL_NOTICE))

                    # 在重复截断后，强制进入纯文本模式，使 LLM 停止尝试无法放入输出预算的工具调用。
                    if truncation_count >= 3:
                        reason_ctx.force_text = True

                    # 执行迭代后回调
                    await delegate.after_iteration(iteration)
                    # 继续下一次迭代
                    continue

                # 重置计数器
                consecutive_tool_intent_nudges = 0
                truncation_count = 0

                # 在执行前计算指纹（避免克隆整个 Vec）
                batch_fingerprint = DuplicateToolCallTracker.fingerprint(tool_calls)

                # 在执行前重置标志；委托在 execute_tool_calls 中设置它
                reason_ctx.last_tool_batch_all_failed = False

                # 执行工具调用
                outcome = await delegate.execute_tool_calls(tool_calls, content, reason_ctx, reasoning)
                if outcome is not None:
                    return outcome

                # 跟踪重复的失败工具调用并逐步升级
                dup_count = dup_tracker.record_with_fingerprint(
                    batch_fingerprint,
                    reason_ctx.last_tool_batch_all_failed,
                )

                if dup_count >= DUPLICATE_FORCE_TEXT_THRESHOLD:
                    # 重复的失败工具调用 —— 强制进入文本模式
                    logger.debug(
                        "迭代 %d: 重复的失败工具调用（次数=%d）—— 强制进入文本模式",
                        iteration,
                        dup_count,
                    )
                    reason_ctx.force_text = True
                    reason_ctx.messages.append(ChatMessage.user(DUPLICATE_TOOL_CALL_WARNING))

                elif dup_count >= DUPLICATE_WARNING_THRESHOLD:
                    # 重复的失败工具调用 —— 注入警告
                    logger.debug(
                        "迭代 %d: 重复的失败工具调用（次数=%d）—— 注入警告",
                        iteration,
                        dup_count,
                    )
                    reason_ctx.messages.append(ChatMessage.user(DUPLICATE_TOOL_CALL_WARNING))

            case _:
                pass

        # 判断输出结果
        await delegate.after_iteration(iteration)

    # 超过最大调用次数
    return LoopOutcome.MaxIterations
