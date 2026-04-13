import logging
from llm import llm_signals_tool_intent, TOOL_INTENT_NUDGE

logger = logging.getLogger(__name__)


async def run_agentic_loop(
        delegate: LoopDelegate,
        reasoning: Reasoning,
        reason_ctx: ReasoningContext,
        config: AgenticLoopConfig
):
    """
    运行统一的智能体循环。

    这是所有三类消费者（聊天、作业、容器）使用的唯一实现。
    delegate 通过 LoopDelegate 特质提供消费者特定的行为
    """
    consecutive_tool_intent_nudges = 0

    for iteration in range(config.max_iterations):
        # 检查外部信号（停止、取消、用户消息）
        signal = await delegate.check_signals()
        match signal:
            case LoopSignal.Continue:
                pass
            case LoopSignal.Stop:
                return LoopOutcome.STOPPED
            case LoopSignal.InjectMessage(msg):
                reason_ctx.messages.push(ChatMessage.user(msg))

        # 大语言模型调用前钩子（成本守卫、工具刷新、迭代限制提示）
        outcome = await delegate.before_llm_call(reason_ctx, iteration)
        if outcome:
            return outcome

        # 请求大模型
        output = await delegate.call_llm(reasoning, reason_ctx, iteration)

        match output.result:
            case RespondResult.Text(text):
                logger.debug(f"LLM返回文本结果")
                # 工具意图提示：如果大语言模型只是说“让我搜索一下……”而没有实际调用工具，则注入一条提示消息。
                if config.enable_tool_intent_nudge and \
                        reason_ctx.available_tools and \
                        reason_ctx.force_text and \
                        consecutive_tool_intent_nudges < config.max_tool_intent_nudges and \
                        llm_signals_tool_intent(text):
                    consecutive_tool_intent_nudges += 1
                    logger.debug("大语言模型表达了工具使用意图但没有实际调用工具，正在发送提示")
                    await delegate.on_tool_intent_nudge(text, reason_ctx)
                    reason_ctx.messages.push(ChatMessage.assistant(text))
                    reason_ctx.messages.push(ChatMessage.user(TOOL_INTENT_NUDGE))
                    await delegate.after_iteration(iteration)

                # 由于我们收到了非意图文本响应，重置提示计数器
                if llm_signals_tool_intent(text):
                    consecutive_tool_intent_nudges = 0
                action = await delegate.handle_text_response(text, reason_ctx)
                match action:
                    case TextAction.Return(outcome):
                        return outcome
                    case TextAction.Continue:
                        continue

            case RespondResult.ToolCalls(tool_calls, content):
                logger.debug(f"LLM调用工具")
                consecutive_tool_intent_nudges = 0
                outcome = await delegate.execute_tool_calls(tool_calls, content, reason_ctx)
                if outcome:
                    return outcome

        await delegate.after_iteration(iteration)


    return LoopOutcome.MaxIterations



