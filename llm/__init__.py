from .session import create_session_manager, SessionManager
from .provider import (
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    ContentPart,
    FinishReason,
    ImageUrl,
    LlmProvider,
    ModelMetadata,
    Role,
    ToolCall,
    ToolCompletionRequest,
    ToolCompletionResponse,
    ToolDefinition,
    ToolResult,
    generate_tool_call_id,
    normalized_model_override,
sanitize_tool_messages
)
from .reasoning import (
    ActionPlan,
    Reasoning,
    ReasoningContext,
    RespondOutput,
    RespondResult,
    ResponseAnomaly,
    ResponseMetadata,
    SILENT_REPLY_TOKEN,
    TOOL_INTENT_NUDGE,
    TRUNCATED_TOOL_CALL_NOTICE,
    TokenUsage,
    ToolSelection,
    is_silent_reply,
    llm_signals_tool_intent,
    user_signals_execution_intent,
    clean_response,
    contains_codex_text_tool_call_syntax,
    recover_codex_text_tool_calls_from_content,
    recover_codex_text_tool_calls_from_tool_names,
    recover_tool_calls_from_content

)
from .config import LlmConfig
from .recording import MemorySnapshotEntry, RecordingLlm
from .runtime import LlmReloadHandle, SwappableLlmProvider

from typing import Optional, Tuple, Any
import logging

logger = logging.getLogger(__name__)

import asyncio
import logging
from datetime import timedelta
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# ── 提供者链组件 ─────────────────────────────────────────────

class ProviderChainComponents:
    """从配置重建的原始主要 + 廉价提供者

    由 [`build_provider_chain`]（用于启动接线）和 [`LlmReloadHandle::reload`]
    （用于热交换）使用：后者需要*未包装*的主要提供者，以便将其馈送到现有的
    [`SwappableLlmProvider`] 中，而不堆叠另一个包装器
    """

    def __init__(
            self,
            primary: "LlmProvider",
            cheap: Optional["LlmProvider"] = None,
    ):
        self.primary: LlmProvider = primary
        self.cheap: Optional[LlmProvider] = cheap


# ── 构建提供者链组件 ─────────────────────────────────────────

async def build_provider_chain_components(
        config: LlmConfig,
        session: SessionManager,
) -> ProviderChainComponents:
    """构建完整的 LLM 提供者链及其所有配置的包装器

    按以下顺序应用装饰器：
    1. 原始提供者（来自配置）
    2. RetryProvider（每个提供者的指数退避重试）
    3. SmartRoutingProvider（配置廉价模型时的廉价/主要拆分）
    4. FailoverProvider（主要失败时的回退模型）
    5. CircuitBreakerProvider（后端降级时快速失败）
    6. CachedProvider（内存响应缓存）

    同时返回一个单独的廉价 LLM 提供者用于心跳/评估
    （不属于链的一部分 — 它是用于显式廉价任务的独立提供者）

    这是提供者链构建的唯一真实来源，由 `main.rs` 和 `app.rs` 调用
    """
    return await build_provider_chain_components_with_options(
        config, session, include_standalone_cheap=True,
    )


async def build_provider_chain_components_with_options(
        config: LlmConfig,
        session: SessionManager,
        include_standalone_cheap: bool,
) -> ProviderChainComponents:
    """构建提供者链组件，可选择是否包含独立的廉价提供者"""

    # 1. 原始提供者
    if config.backend == "openai_codex":
        llm: LlmProvider = await create_openai_codex_provider(config)
    else:
        llm: LlmProvider = await create_llm_provider(config, session)

    logger.debug(f"LLM 提供者已初始化: {llm.model_name()}")

    # 2. 重试包装器 — 使用顶层 LlmConfig 字段
    retry_config = RetryConfig(max_retries=config.max_retries)
    if retry_config.max_retries > 0:
        logger.debug(f"LLM 重试包装器已启用 (max_retries={retry_config.max_retries})")
        llm: LlmProvider = RetryProvider(llm, retry_config)

    # 3. 智能路由（廉价/主要拆分）
    cheap_model = config.cheap_model_name()
    if cheap_model is not None:
        cheap: LlmProvider = create_cheap_provider_for_backend(config, session, cheap_model)
        if cheap is None:
            raise LlmError(
                f"请求失败: 无法为后端 '{config.backend}' 上的模型 '{cheap_model}' 创建廉价提供者"
            )

        if retry_config.max_retries > 0:
            cheap: LlmProvider = RetryProvider(cheap, retry_config)

        logger.debug(
            f"智能路由已启用 (primary={llm.model_name()}, cheap={cheap.model_name()})"
        )
        smart_routing_config = SmartRoutingConfig(
            cascade_enabled=config.smart_routing_cascade,
        )
        llm: LlmProvider = SmartRoutingProvider(llm, cheap, smart_routing_config)

    # 4. 故障转移
    if config.nearai.fallback_model is not None:
        if config.nearai.fallback_model == config.nearai.model:
            logger.warning("fallback_model 与主要模型相同，故障转移可能无效")

        fallback_config = NearAiConfig(
            model=config.nearai.fallback_model,
            cheap_model=config.nearai.cheap_model,
            base_url=config.nearai.base_url,
            api_key=config.nearai.api_key,
            fallback_model=None,
            max_retries=config.nearai.max_retries,
            circuit_breaker_threshold=config.nearai.circuit_breaker_threshold,
            circuit_breaker_recovery_secs=config.nearai.circuit_breaker_recovery_secs,
            response_cache_enabled=config.nearai.response_cache_enabled,
            response_cache_ttl_secs=config.nearai.response_cache_ttl_secs,
            response_cache_max_entries=config.nearai.response_cache_max_entries,
            failover_cooldown_secs=config.nearai.failover_cooldown_secs,
            failover_cooldown_threshold=config.nearai.failover_cooldown_threshold,
            smart_routing_cascade=config.nearai.smart_routing_cascade,
        )
        fallback: LlmProvider = create_llm_provider_with_config(
            fallback_config, session, config.request_timeout_secs,
        )

        logger.debug(
            f"LLM 故障转移已启用 (primary={llm.model_name()}, fallback={fallback.model_name()})"
        )

        if retry_config.max_retries > 0:
            fallback: LlmProvider = RetryProvider(fallback, retry_config)

        cooldown_config = CooldownConfig(
            cooldown_duration=timedelta(seconds=config.nearai.failover_cooldown_secs),
            failure_threshold=config.nearai.failover_cooldown_threshold,
        )
        llm: LlmProvider = FailoverProvider([llm, fallback], cooldown_config)

    # 5. 断路器
    if config.circuit_breaker_threshold is not None:
        cb_config = CircuitBreakerConfig(
            failure_threshold=config.circuit_breaker_threshold,
            recovery_timeout=timedelta(seconds=config.circuit_breaker_recovery_secs),
        )
        logger.debug(
            f"LLM 断路器已启用 (threshold={config.circuit_breaker_threshold}, "
            f"recovery_secs={config.circuit_breaker_recovery_secs})"
        )
        llm: LlmProvider = CircuitBreakerProvider(llm, cb_config)

    # 6. 响应缓存
    if config.response_cache_enabled:
        rc_config = ResponseCacheConfig(
            ttl=timedelta(seconds=config.response_cache_ttl_secs),
            max_entries=config.response_cache_max_entries,
        )
        logger.debug(
            f"LLM 响应缓存已启用 (ttl_secs={config.response_cache_ttl_secs}, "
            f"max_entries={config.response_cache_max_entries})"
        )
        llm: LlmProvider = CachedProvider(llm, rc_config)

    # 独立的廉价 LLM 用于心跳/评估（不属于链的一部分）
    cheap_llm: Optional[LlmProvider] = None
    if include_standalone_cheap:
        cheap_llm = create_cheap_llm_provider(config, session)

    if cheap_llm is not None:
        logger.debug(f"廉价 LLM 提供者已初始化: {cheap_llm.model_name()}")

    return ProviderChainComponents(primary=llm, cheap=cheap_llm)

async def build_provider_chain(
        config: LlmConfig,
        session: SessionManager,
) -> Tuple[LlmProvider, Optional[LlmProvider], Optional[RecordingLlm], LlmReloadHandle]:
    """构建完整的提供者链并将主要（以及廉价的，如果有）包装在支持热交换的
    [`SwappableLlmProvider`] 句柄中。返回的 [`LlmReloadHandle`] 可以稍后从
    新配置重新构建链

    这是提供者链构建的唯一真实来源，由 `main.rs` 和 `app.rs` 调用

    Args:
        config: LLM 配置
        session: 会话管理器

    Returns:
        包含 (主要提供者, 可选的廉价提供者, 可选的 RecordingLlm, LlmReloadHandle) 的元组

    Raises:
        LlmError: 当构建提供者链失败时
    """
    # 构建提供者链组件
    components = await build_provider_chain_components(config, session)

    # 将主要提供者包装在可热交换的包装器中
    primary_swappable = SwappableLlmProvider(components.primary)

    # 将廉价提供者（如果存在）也包装在可热交换的包装器中
    cheap_swappable = None
    if components.cheap is not None:
        cheap_swappable = SwappableLlmProvider(components.cheap)

    # 构建重载句柄
    reload_handle = LlmReloadHandle(primary_swappable, cheap_swappable)

    # 录制（用于重放测试的追踪捕获）包装可交换的包装器，
    # 以便追踪在交换时跟随活跃的内部提供者
    primary = primary_swappable
    recording_handle = RecordingLlm.from_env(primary)
    if recording_handle is not None:
        primary = recording_handle

    # 廉价提供者
    cheap = cheap_swappable if cheap_swappable is not None else None

    return (primary, cheap, recording_handle, reload_handle)