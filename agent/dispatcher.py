from __future__ import annotations
import asyncio
import logging
from threading import Lock
from typing import Callable, TypeVar, Any
from dataclasses import dataclass, field
from typing import List, Optional
from agent import Agent
from agent.session import PendingApproval, PendingAuthPrompt, Session, ThreadState
from channels import (
    ChannelManager,
    IncomingMessage,
    StatusUpdate,
    ToolDecision
)
from context import JobContext
from error import Error
from agent.agent_loop import chat_tool_execution_metadata
from agent.agentic_loop import AgenticLoopConfig, LoopDelegate, LoopOutcome, LoopSignal, TextAction
from tools.permissions import AdminToolPolicyCache, effective_permission
from tools import redact_params
from skills.attenuation import attenuate_tools
from tenant import TenantCtx
from skills import LoadedSkill
from llm import (
    ChatMessage,
    Reasoning,
    ReasoningContext,
    TokenUsage,
    Role,
    ToolCall,
    ResponseMetadata,
    normalized_model_override
)
from decimal import Decimal

# 日志记录器
logger = logging.getLogger(__name__)


def selected_model_override(value: Any) -> Optional[str]:
    """
    从 JSON 值中提取并规范化模型覆盖。

    参数:
        value: 可能包含模型名称的 JSON 值。

    返回:
        Optional[str]: 规范化后的模型名称，如果无效或为默认值则返回 None。
    """
    # 仅当值是字符串时才尝试获取模型名称
    if isinstance(value, str):
        return normalized_model_override(value)
    return None


def resolve_settings_temperature(
        current: Optional[float],
        settings_value: Optional[Any],
) -> Optional[float]:
    """
    决定从设置中派生的温度是否应覆盖推理上下文中已有的按请求值。

    仅当尚无按请求值且设置值可以解析为数字时才返回 `Some(new_value)`。
    结果被限制在支持的 `[0.0, 2.0]` 范围内，以防止错误的数据库值。

    参数:
        current: 当前已设置的按请求温度值。
        settings_value: 从设置中读取的可选 JSON 值。

    返回:
        Optional[float]: 解析并限制后的温度值，如果不应覆盖则返回 None。
    """
    # 如果已有按请求值，则不覆盖
    if current is not None:
        return None

    # 尝试将设置值解析为数字并限制范围
    if settings_value is not None and isinstance(settings_value, (int, float)):
        t = float(settings_value)
        # 限制在 [0.0, 2.0] 范围内
        return max(0.0, min(2.0, t))

    return None

def chat_job_context(
    message: IncomingMessage,
    thread_id: str,
    user_tz: "ZoneInfo",  # chrono_tz::Tz，Python 中用 zoneinfo.ZoneInfo 或 pytz.timezone
    skill_scope_owner_id: Optional[str],
) -> JobContext:
    """
    为聊天会话创建作业上下文。
    参数:
        message: 传入的消息。
        thread_id: 当前线程的唯一标识符。
        user_tz: 用户的时区。
        skill_scope_owner_id: 可选的技能范围所有者 ID，仅在多租户模式下设置。

    返回:
        JobContext: 配置好的作业上下文。
    """
    # 创建基础作业上下文，设置为聊天类型
    job_ctx = JobContext.with_user(message.user_id, "chat", "Interactive chat session")
    job_ctx = job_ctx.with_requester_id(message.sender_id)

    # 设置对话 ID 为线程 ID
    job_ctx.conversation_id = thread_id

    # 设置用户时区为时区名称字符串
    # Python 中 ZoneInfo.key 或 pytz.timezone.zone 返回时区名称
    job_ctx.user_timezone = str(user_tz)  # 假设 user_tz 的字符串表示即为时区名称

    # 设置元数据
    job_ctx.metadata = chat_tool_execution_metadata(message)

    # 如果提供了技能范围所有者 ID，则添加到元数据中
    if skill_scope_owner_id is not None:
        job_ctx.metadata["skill_scope_owner_id"] = skill_scope_owner_id

    return job_ctx



@dataclass
class AgenticLoopResponseResult:
    """
    完成并返回响应。
    """
    # 响应文本内容。
    text: str
    # 当前回合的使用量摘要。
    turn_usage: TurnUsageSummary


@dataclass
class AgenticLoopNeedApprovalResult:
    """
    某个工具在继续之前需要审批。
    """
    # 要存储的待处理审批请求。
    pending: PendingApproval
    # 回合因审批暂停前累积的使用量。
    turn_usage: TurnUsageSummary


@dataclass
class AgenticLoopFailedResult:
    """
    循环在当前回合花费使用量后失败。
    """
    # 失败的错误信息。
    error: Exception
    # 当前回合的使用量摘要。
    turn_usage: TurnUsageSummary


@dataclass
class AgenticLoopAuthPendingResult:
    """
    认证流程已启动 —— 配置卡片已通过 AuthRequired 状态发送，
    且已在当前线程上调用了 enter_auth_mode。调用者使用
    TurnOutcome::CompletedSilently 结束回合（不持久化文本响应 ——
    认证卡片是唯一面向用户的信号）。
    """
    turn_usage: TurnUsageSummary

# 代理循环执行的结果。
AgenticLoopResult = AgenticLoopResponseResult | AgenticLoopNeedApprovalResult | AgenticLoopFailedResult | AgenticLoopAuthPendingResult

@dataclass
class TurnUsageSummary:
    """
    回合使用量摘要。
    """
    # 累计的令牌使用量。
    usage: TokenUsage = field(default_factory=lambda: TokenUsage())
    # 累计的美元成本。
    cost_usd: Decimal = field(default_factory=lambda: Decimal('0'))

    def record_llm_call(self, usage: TokenUsage, cost_usd: Decimal) -> None:
        """
        记录一次 LLM 调用，累加令牌使用量和成本。
        参数:
            usage: 本次 LLM 调用的令牌使用量。
            cost_usd: 本次 LLM 调用的美元成本。
        """
        # saturating_add 对应 Python 中的普通加法（Python 整数不会溢出）
        self.usage.input_tokens += usage.input_tokens
        self.usage.output_tokens += usage.output_tokens
        self.usage.cache_read_input_tokens += usage.cache_read_input_tokens
        self.usage.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self.cost_usd += cost_usd

    def clone(self) -> "TurnUsageSummary":
        """
        创建当前摘要的深拷贝。
        返回:
            TurnUsageSummary: 当前摘要的副本。
        """
        return TurnUsageSummary(
            usage=TokenUsage(
                input_tokens=self.usage.input_tokens,
                output_tokens=self.usage.output_tokens,
                cache_read_input_tokens=self.usage.cache_read_input_tokens,
                cache_creation_input_tokens=self.usage.cache_creation_input_tokens,
            ),
            cost_usd=self.cost_usd,  # Decimal 是不可变类型，直接赋值即可
        )


@dataclass
class ChatDelegate(LoopDelegate):
    """
    聊天（分发器）上下文的委托实现。

    实现 `LoopDelegate` 特质，为交互式聊天会话定制共享的智能体循环，
    包含完整的三个阶段工具执行（预检 → 并行执行 → 后置处理）、审批流程、
    钩子、认证拦截和成本跟踪。
    """
    agent: Agent
    # 按请求的租户执行上下文。
    tenant: TenantCtx
    # 包含一个或多个线程的会话。
    session: Session
    thread_id: str
    # 从外部渠道收到的消息
    message: IncomingMessage
    job_ctx: JobContext
    # 激活的技能
    active_skills: List[LoadedSkill]
    cached_prompt: str
    cached_prompt_no_tools: str
    nudge_at: int
    force_text_at: int
    user_tz: Any  # chrono_tz::Tz，Python 中可用 pytz.timezone 或 zoneinfo.ZoneInfo
    turn_usage: Lock  # std::sync::Mutex<TurnUsageSummary>，Python 使用 threading.Lock
    cached_admin_tool_policy: AdminToolPolicyCache

    def turn_usage_summary(self) -> TurnUsageSummary:
        """
        获取回合使用量摘要的克隆。
        """
        return self.with_turn_usage(lambda turn_usage: turn_usage.clone())

    def record_turn_usage(self, usage: TokenUsage, cost_usd: Decimal) -> None:
        """
        记录一次 LLM 调用的使用量。
        """
        self.with_turn_usage(
            lambda turn_usage: turn_usage.record_llm_call(usage, cost_usd)
        )

    def with_turn_usage(self, f: Callable[[TurnUsageSummary], R]) -> R:
        """
        在锁保护下访问回合使用量数据。
        如果互斥锁被毒化，记录警告并恢复累积的使用量。

        Python 的 threading.Lock 不会像 Rust 的 std::sync::Mutex 那样
        在持有线程 panic 时被"毒化"，因此直接使用 with 语句获取锁即可。
        此处保留 try/except 结构以对应 Rust 的 match Ok/Err 语义，
        实际中 Python 锁不会抛出异常。

        参数:
            f: 接收 TurnUsageSummary 的可变引用并返回 R 的回调函数。

        返回:
            R: 回调函数的返回值。
        """
        try:
            # 获取锁（对应 Rust 的 self.turn_usage.lock() Ok 分支）
            # 对应 Rust: Ok(mut turn_usage) => f(&mut turn_usage),
            with self._turn_usage_lock:
                return f(self._turn_usage_data)
        except Exception:
            # Python 锁不会毒化，但为保持与 Rust 代码结构的一致性，
            # 保留异常处理分支
            # 对应 Rust: Err(poisoned) => { tracing::warn!(...); let mut turn_usage = poisoned.into_inner(); f(&mut turn_usage) }
            logger.warning("回合使用量互斥锁毒化；恢复累积的使用量")
            # Python 中没有 poisoned.into_inner() 的概念，
            # 直接使用现有的数据引用
            return f(self._turn_usage_data)

    async def check_signals(self) -> LoopSignal:
        if (thread := self.session.get(self.thread_id)) and (thread.state == ThreadState.Interrupted):
            return LoopSignal.Stop

        return LoopSignal.Continue

    async def before_llm_call(self, reason_ctx: ReasoningContext, iteration: int):
        # 当接近迭代次数限制时注入一条提示消息，以便大语言模型意识到应在下一轮生成最终答案。
        if iteration == self.nudge_at:
            msg = ChatMessage(
                role=Role.System,
                content="你正接近工具调用次数上限。"
                        "请在下一轮回复中，使用你已经收集到的信息给出最佳的最终答案。"
                        "不要再调用任何工具。")
            reason_ctx.messages.append(msg)

        force_text = iteration >= self.force_text_at
        # 每次迭代时刷新工具定义，以便新构建的工具能够被识别。
        # 当配置了运行时策略时，使用经过策略过滤的变体**，这样可见性过滤适用于*每一次*迭代，
        # 而不仅仅是第 1 次迭代（zmanian #3243 高优先级第 2 次迭代缺口）。
        # 如果不这样做，托管多租户部署会在第一轮对话之后将提供商主机类工具（例如一旦有工具构建器注册了 `shell`）暴露给模型。
        # 根据运行时策略获取工具定义
        if self.agent.deps.runtime_policy is not None:
            # 有策略时，获取策略下可见的工具定义
            tool_defs = await self.agent.tools().tool_definitions_visible_under(self.agent.deps.runtime_policy)
        else:
            # 无策略时，获取所有工具定义
            tool_defs = await self.agent.tools().tool_definitions()

        # 如果存在活跃技能，则应用基于信任的工具衰减
        if self.active_skills:
            # 执行工具衰减
            result = attenuate_tools(tool_defs, self.active_skills)  # 假设 attenuate_tools 已导入

            # 记录衰减详情
            logger.debug(
                "应用工具衰减: min_trust=%s, tools_available=%d, tools_removed=%d, removed=%s, explanation=%s",
                result.min_trust,
                len(result.tools),
                len(result.removed_tools),
                result.removed_tools,
                result.explanation,
            )

            # 使用衰减后的工具列表
            tool_defs = result.tools

        # 首先应用管理员工具策略，这样在按用户权限过滤和会话自动批准之前，
        # 管理员禁用的工具就已经被移除。
        is_admin = self.tenant.identity().is_admin()

        # 加载缓存的管理员工具策略
        admin_policy = await load_cached_admin_tool_policy(
            self.agent.store(),
            self.cached_admin_tool_policy,
        )

        # 过滤管理员禁用的工具
        tool_defs = filter_admin_disabled_tools(
            tool_defs,
            self.agent.config.multi_tenant,
            is_admin,
            self.tenant.user_id(),
            admin_policy,
        )

        # 应用按用户工具权限过滤。
        #
        # 从按用户数据库设置存储中加载 tool_permissions（与 selected_model 同一数据源）。
        # 当没有可用存储时（没有租户的测试环境），回退到空映射 —— 缺失的条目通过
        # effective_permission() 中的种子基线来解析。
        # 禁用的工具会完全从 LLM 的工具列表中排除。
        # AlwaysAllow 工具在会话中预先批准，因此审批流程会被跳过，
        # 除非工具声明了 ApprovalRequirement::Always，它仍然是不可绕过的硬性底线。
        #
        # SettingsStore 被包装在 CachedSettingsStore 中，因此同一会话内的重复调用成本很低（内存查找）。
        if self.tenant.store() is not None:
            # 有存储可用，尝试获取所有设置
            try:
                db_map = await self.tenant.store().get_all_settings()
                # 从数据库映射构建设置对象并提取工具权限
                tool_permissions = Settings.from_db_map(db_map).tool_permissions
            except Exception as e:
                # 加载失败时记录警告，保持现有会话状态
                logger.warning(
                    "加载工具权限失败，保持现有会话状态: %s", e
                )
                # 故障关闭：保留之前过滤后的 available_tools，
                # 而不是发布未过滤的工具列表，后者可能会重新暴露被明确标记为 Disabled 的工具。
                # 这里返回 None 表示调用方应保持原有的工具列表不变
                tool_permissions = None  # 特殊标记，由调用方判断
        else:
            # 无存储可用，使用空映射
            tool_permissions = {}

        # 过滤工具定义并为会话预批准收集 AlwaysAllow 名称。
        # 有效权限在此具有权威性，但解析为 AlwaysAllow 的工具在执行时仍然遵守
        # ApprovalRequirement::Always。AskEachTime/Disabled 行会在下一回合清除预批准。
        to_auto_approve = []

        # 过滤工具定义列表
        filtered_tool_defs = []
        for tool_def in tool_defs:
            # 根据有效权限决定工具的处理方式
            permission = effective_permission(tool_def.name, tool_permissions)

            if permission == PermissionState.Disabled:
                # 从 LLM 上下文中排除禁用的工具
                logger.debug("从LLM上下文中排除禁用的工具: tool=%s", tool_def.name)
                # None 表示过滤掉此工具（不添加到结果列表）
                continue
            elif permission == PermissionState.AlwaysAllow:
                # 收集要自动批准的工具名称
                to_auto_approve.append(tool_def.name)
                filtered_tool_defs.append(tool_def)
            elif permission == PermissionState.AskEachTime:
                # 每次询问的工具直接保留
                filtered_tool_defs.append(tool_def)

        # 用过滤后的列表替换原工具定义列表
        tool_defs = filtered_tool_defs

        # 清除并重新填充自动批准列表，使用当前数据库状态，
        # 这样权限降级（AlwaysAllow → AskEachTime）可以在同一会话中立竿见影。
        # "始终批准" 的点击通过 process_approval 持久化到数据库，因此会在此处重新添加。
        # 对应 Rust: { let mut sess = self.session.lock().await; ... }
        async with self.session.lock:
            # 清除现有自动批准工具列表
            # 对应 Rust: sess.auto_approved_tools.clear();
            self.session.auto_approved_tools.clear()

            # 重新填充要自动批准的工具
            # 对应 Rust: for name in &to_auto_approve { sess.auto_approve_tool(name); }
            for name in to_auto_approve:
                self.session.auto_approve_tool(name)

        # 更新本次迭代的上下文
        # 对应 Rust: reason_ctx.available_tools = tool_defs;
        reason_ctx.available_tools = tool_defs

        # 如果已经设置了 force_text（例如由截断升级触发），则保留该状态
        # 对应 Rust: let force_text = force_text || reason_ctx.force_text;
        force_text = force_text or reason_ctx.force_text

        # 根据 force_text 选择对应的系统提示
        # 对应 Rust: reason_ctx.system_prompt = Some(if force_text { ... } else { ... });
        if force_text:
            reason_ctx.system_prompt = self.cached_prompt_no_tools
        else:
            reason_ctx.system_prompt = self.cached_prompt

        # 更新上下文中的 force_text 标志
        # 对应 Rust: reason_ctx.force_text = force_text;
        reason_ctx.force_text = force_text

        # 如果强制文本模式，记录信息日志
        # 对应 Rust: if force_text { tracing::info!(...); }
        if force_text:
            logger.info(
                "迭代 %d: 强制使用纯文本响应（已达到迭代限制）",
                iteration,
            )

        # 向用户发送"思考中"状态更新
        # 对应 Rust: let _ = self.agent.channels.send_status(...).await;
        # 使用 try/except 忽略发送失败（模拟 let _ = 的语义）
        try:
            await self.agent.channels.send_status(
                self.message.channel,
                StatusUpdate.Thinking(f"思考中（第 {iteration} 步）..."),
                self.message.metadata,
            )
        except Exception:
            pass

    async def call_llm(self, reasoning: Reasoning, reason_ctx: ReasoningContext, iteration: int):
        # 在调用大语言模型之前执行成本防护。

        # 调用大模型
        try:
            output = await reasoning.respond_with_tools(reason_ctx)
            return output
        except ContextLengthExceeded as e:
            # 捕获特定错误：上下文长度超限
            pass
        except Exception as other_err:
            return other_err

        # 记录成本并跟踪令牌使用情况。

    async def handle_text_response(
            self,
            text: str,
            metadata: ResponseMetadata,
            reason_ctx: ReasoningContext,
    ) -> TextAction:
        """
        处理文本响应。

        剥离内部 "[Called tool ...]" 文本，这些文本可能在遗留提供商兼容性扁平化
        将 tool_calls 转换为纯文本且 LLM 将其回显时泄漏出来。

        参数:
            text: LLM 响应的文本内容。
            metadata: 响应元数据（未使用，用 _metadata 前缀表示）。
            reason_ctx: 推理上下文的可变引用（未使用，用 _reason_ctx 前缀表示）。

        返回:
            TextAction: 包含 LoopOutcome::Response 的 Return 动作。
        """
        # 剥离内部 "[Called tool ...]" 文本，防止遗留提供商兼容性扁平化导致的泄漏
        sanitized = strip_internal_tool_call_text(text)

        # 返回带有清理后文本的响应结果
        return Return(LoopOutcome.Response(sanitized))

    async def execute_tool_calls(
            self,
            tool_calls: List[ToolCall],
            content: Optional[str],
            reason_ctx: ReasoningContext,
            reasoning: Optional[str],
    ) -> Optional[LoopOutcome]:
        """
        执行工具调用。
        参数:
            tool_calls: 要执行的工具调用列表。
            content: 助手消息的可选文本内容。
            reason_ctx: 推理上下文的可变引用。
            reasoning: 大模型think内容。

        返回:
            可选的 LoopOutcome，如果工具执行需要暂停（审批/认证）则返回相应结果。
        """
        # ---------- 提取并清理叙述文本 ----------
        narrative = None
        if content is not None and content.strip():
            sanitized = self.agent.safety.sanitize_tool_output("agent_narrative", content)
            clean = sanitized.content
            if clean.strip():
                narrative = clean

        # ---------- 将助手消息（带 tool_calls）添加到上下文 ----------
        # OpenAI 协议要求这在工具结果消息之前。
        # 携带 reasoning 以便下一次请求可以将其回传 —— DeepSeek 思考模式和 Gemini 2.5+
        # 需要此字段来验证链（#3201, #3225）。
        assistant_msg = ChatMessage.assistant_with_tool_calls(content, tool_calls)
        assistant_msg = assistant_msg.with_reasoning(reasoning)
        reason_ctx.messages.append(assistant_msg)

        # 执行工具并将结果添加到上下文中
        try:
            await self.agent.channels.send_status(
                self.message.channel,
                StatusUpdate.Thinking(contextual_tool_message(tool_calls)),
                self.message.metadata,
            )
        except Exception as e:
            logger.warning(f"send_status时报错:{e}")

        # ---------- 构建每个工具的决策（用于推理更新）----------
        # 为推理更新构建每个工具的决策。
        # 通过 SafetyLayer 对每项理由进行清理（与 JobDelegate 保持一致）。
        decisions = []
        for tc in tool_calls:
            if tc.reasoning:
                sanitized = self.agent.safety.sanitize_tool_output("tool_rationale", tc.reasoning)
                decisions.append(ToolDecision(
                    tool_name=tc.name,
                    rationale=sanitized.content,
                ))

        # ---------- 向频道发送推理更新 ----------
        if narrative is not None or decisions:
            try:
                await self.agent.channels.send_status(
                    self.message.channel,
                    StatusUpdate.ReasoningUpdate(
                        narrative=narrative or "",
                        decisions=list(decisions),
                    ),
                    self.message.metadata,
                )
            except Exception:
                pass

        # ---------- 记录工具调用（敏感参数已脱敏）----------
        redacted_args = []
        for tc in tool_calls:
            tool = await self.agent.tools.get(tc.name)
            if tool is not None:
                safe = redact_params(tc.arguments, tool.sensitive_params())
            else:
                safe = dict(tc.arguments)  # 浅拷贝
            redacted_args.append(safe)

        # 获取会话锁并记录到线程中
        async with self.session.lock:
            thread = self.session.threads.get(self.thread_id)
            if thread is not None:
                turn = thread.last_turn()
                if turn is not None:
                    # 设置回合级别的叙述
                    if turn.narrative is None:
                        turn.narrative = narrative

                    for tc, safe_args in zip(tool_calls, redacted_args):
                        sanitized_rationale = None
                        if tc.reasoning is not None:
                            sanitized = self.agent.safety().sanitize_tool_output("tool_rationale", tc.reasoning)
                            sanitized_rationale = sanitized.content

                        turn.record_tool_call_with_reasoning(
                            tc.name,
                            safe_args,
                            sanitized_rationale,
                            tc.id,
                        )

        # === 阶段 1：预检（顺序执行）===
        # 遍历 tool_calls 检查审批和钩子。将每个工具分类为 Rejected（由钩子拒绝）或 Runnable。
        # 在第一个需要审批的工具处停止。
        preflight = []
        runnable = []
        approval_needed = None

        for idx, original_tc in enumerate(tool_calls):
            tc = ToolCall(
                id=original_tc.id,
                name=original_tc.name,
                arguments=dict(original_tc.arguments),  # 克隆
                reasoning=original_tc.reasoning,
                signature=original_tc.signature,
                arguments_parse_error=original_tc.arguments_parse_error,
            )

            tool_opt = await self.agent.tools().get(tc.name)
            sensitive = tool_opt.sensitive_params() if tool_opt is not None else []

            # 钩子：BeforeToolCall
            hook_params = redact_params(tc.arguments, sensitive)
            event = HookEvent.ToolCall(
                tool_name=tc.name,
                parameters=hook_params,
                user_id=self.message.user_id,
                context="chat",
            )

            hook_result = await self.agent.hooks().run(event)

            if isinstance(hook_result, HookError.Rejected):
                preflight.append((tc, PreflightOutcome.Rejected(
                    f"工具调用被钩子拒绝: {hook_result.reason}"
                )))
                continue

            if isinstance(hook_result, HookError):
                preflight.append((tc, PreflightOutcome.Rejected(
                    f"工具调用被钩子策略阻止: {hook_result}"
                )))
                continue

            # 处理钩子修改的参数
            # 对应 Rust: Ok(HookOutcome::Continue { modified: Some(new_params) }) => ...
            if isinstance(hook_result, HookOutcome.Continue) and hook_result.modified is not None:
                try:
                    parsed = json.loads(hook_result.modified)
                    if isinstance(parsed, dict):
                        for key in sensitive:
                            if key in original_tc.arguments:
                                parsed[key] = original_tc.arguments[key]
                        tc.arguments = parsed
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(
                        "钩子为 ToolCall 返回了非 JSON 修改，忽略: tool=%s, error=%s",
                        tc.name, e,
                    )

            # 检查工具是否需要审批
            # 对应 Rust: if !self.agent.config.auto_approve_tools && let Some(tool) = tool_opt { ... }
            if not self.agent.config.auto_approve_tools and tool_opt is not None:
                requirement = tool_opt.requires_approval(tc.arguments)

                if requirement == ApprovalRequirement.NEVER:
                    needs_approval = False
                elif requirement == ApprovalRequirement.UNLESS_AUTO_APPROVED:
                    async with self.session.lock:
                        needs_approval = not self.session.is_tool_auto_approved(tc.name)
                else:  # ApprovalRequirement.ALWAYS
                    needs_approval = True

                if needs_approval:
                    # 在非 DM 中继频道中，自动拒绝需要审批的工具，
                    # 以防止卡在 AwaitingApproval 状态以及其他用户的提示注入。
                    # 对应 Rust: let is_relay = self.message.channel.ends_with("-relay"); ...
                    is_relay = self.message.channel.endswith("-relay")
                    is_dm = self.message.metadata.get("event_type") == "direct_message"

                    if is_relay and not is_dm:
                        logger.info(
                            "在非 DM 中继频道中自动拒绝需要审批的工具: tool=%s, channel=%s",
                            tc.name, self.message.channel,
                        )
                        reject_msg = (
                            f"工具 '{tc.name}' 需要审批，无法在共享频道中运行。"
                            f"请用户直接私信我（DM）以使用此工具。"
                        )
                        preflight.append((tc, PreflightOutcome.Rejected(reject_msg)))
                        continue

                    allow_always = requirement != ApprovalRequirement.ALWAYS
                    approval_needed = (idx, tc, tool_opt, allow_always)
                    break

            preflight_idx = len(preflight)
            preflight.append((tc, PreflightOutcome.Runnable))
            runnable.append((preflight_idx, tc))

        # === 阶段 2：并行执行 ===
        exec_results = [None] * len(preflight)

        if len(runnable) <= 1:
            # 顺序执行单个工具
            for pf_idx, tc in runnable:
                try:
                    await self.agent.channels.send_status(
                        self.message.channel,
                        StatusUpdate.tool_started_with_id(tc.name, tc.arguments, tc.id),
                        self.message.metadata,
                    )
                except Exception:
                    pass

                started_at = time.monotonic()
                result = await self.agent.execute_chat_tool(tc.name, tc.arguments, self.job_ctx)
                duration_ms = int((time.monotonic() - started_at) * 1000)

                disp_tool = await self.agent.tools().get(tc.name)
                try:
                    await self.agent.channels.send_status(
                        self.message.channel,
                        StatusUpdate.tool_completed(
                            tc.name, tc.id, result, tc.arguments,
                            disp_tool, duration_ms,
                        ),
                        self.message.metadata,
                    )
                except Exception:
                    pass

                exec_results[pf_idx] = result

        else:
            # 并行执行多个工具
            tasks = []
            task_indices = []

            for pf_idx, tc in runnable:
                async def run_tool(pf_idx=pf_idx, tc=tc):
                    try:
                        await self.agent.channels.send_status(
                            self.message.channel,
                            StatusUpdate.tool_started_with_id(tc.name, tc.arguments, tc.id),
                            self.message.metadata,
                        )
                    except Exception:
                        pass

                    started_at = time.monotonic()
                    result = await execute_chat_tool_standalone(
                        self.agent.tools(),
                        self.agent.safety(),
                        tc.name,
                        tc.arguments,
                        self.job_ctx,
                    )
                    duration_ms = int((time.monotonic() - started_at) * 1000)

                    par_tool = await self.agent.tools().get(tc.name)
                    try:
                        await self.agent.channels.send_status(
                            self.message.channel,
                            StatusUpdate.tool_completed(
                                tc.name, tc.id, result, tc.arguments,
                                par_tool, duration_ms,
                            ),
                            self.message.metadata,
                        )
                    except Exception:
                        pass

                    return pf_idx, result

                task = asyncio.create_task(run_tool())
                tasks.append(task)
                task_indices.append(pf_idx)

            # 等待所有任务完成
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                pf_idx = task_indices[i]
                if isinstance(result, Exception):
                    logger.error("聊天工具执行任务异常: %s", result)
                    exec_results[pf_idx] = ToolError.ExecutionFailed(
                        name=runnable[i][1].name,
                        reason="任务执行期间失败",
                    )
                else:
                    exec_results[pf_idx] = result[1]

        # === 阶段 3：后处理（按原始顺序顺序执行）===
        selected_auth_prompt = None
        tool_failure_count = 0
        total_tools = len(preflight)

        for pf_idx, (tc, outcome) in enumerate(preflight):
            if isinstance(outcome, PreflightOutcome.Rejected):
                # 对应 Rust: PreflightOutcome::Rejected(error_msg) => { ... }
                tool_failure_count += 1
                error_msg = outcome.reason
                result_content, tool_message = preflight_rejection_tool_message(
                    self.agent.safety(), tc.name, tc.id, error_msg,
                )

                async with self.session.lock:
                    thread = self.session.threads.get(self.thread_id)
                    if thread is not None:
                        turn = thread.last_turn()
                        if turn is not None:
                            turn.record_tool_error_for(tc.id, result_content)

                reason_ctx.messages.append(tool_message)

            else:
                # 对应 Rust: PreflightOutcome::Runnable => { ... }
                tool_result = exec_results[pf_idx]

                # 检测图像生成哨兵
                # 对应 Rust: let image_sentinel = if let Ok(ref output) = tool_result && matches!(tc.name.as_str(), "image_generate" | "image_edit") { ... };
                image_sentinel = None
                if tool_result is not None and not isinstance(tool_result, Exception) and tc.name in ("image_generate",
                                                                                                      "image_edit"):
                    sentinel = GeneratedImageSentinel.from_output(tool_result)
                    if sentinel is not None:
                        data_url = sentinel.data_url() or ""
                        path = sentinel.path()
                        if data_url:
                            try:
                                await self.agent.channels.send_status(
                                    self.message.channel,
                                    StatusUpdate.ImageGenerated(
                                        event_id=tc.id,
                                        data_url=data_url,
                                        path=path,
                                    ),
                                    self.message.metadata,
                                )
                            except Exception:
                                pass
                        else:
                            logger.warning("图像生成哨兵的数据 URL 为空，跳过广播")
                        image_sentinel = sentinel

                # 发送 ToolResult 预览
                # 对应 Rust: if image_sentinel.is_none() && let Ok(ref output) = tool_result && !output.is_empty() { ... }
                if image_sentinel is None and tool_result is not None and not isinstance(tool_result,
                                                                                         Exception) and tool_result:
                    try:
                        await self.agent.channels.send_status(
                            self.message.channel,
                            StatusUpdate.ToolResult(
                                name=tc.name,
                                preview=tool_result,
                                call_id=tc.id,
                            ),
                            self.message.metadata,
                        )
                    except Exception:
                        pass

                    # 为调试订阅者发送完整（非截断）输出
                    MAX_TOOL_OUTPUT_BYTES = 50_000
                    output_bytes = tool_result.encode('utf-8')
                    if len(output_bytes) > MAX_TOOL_OUTPUT_BYTES:
                        boundary = floor_char_boundary(tool_result, MAX_TOOL_OUTPUT_BYTES)
                        capped = tool_result[:boundary]
                        truncated = True
                    else:
                        capped = tool_result
                        truncated = False

                    try:
                        await self.agent.channels.send_status(
                            self.message.channel,
                            StatusUpdate.ToolResultFull(
                                name=tc.name,
                                output=capped,
                                truncated=truncated,
                                call_id=tc.id,
                            ),
                            self.message.metadata,
                        )
                    except Exception:
                        pass

                # 捕获认证提示
                # 对应 Rust: capture_auth_prompt(&mut selected_auth_prompt, &tc.name, &tool_result);
                if selected_auth_prompt is None:
                    selected_auth_prompt = capture_auth_prompt(tc.name, tool_result)

                # 暂存完整输出以便后续工具可以引用
                # 对应 Rust: if let Ok(ref output) = tool_result { self.job_ctx.tool_output_stash.write().await.insert(tc.id.clone(), output.clone()); }
                if tool_result is not None and not isinstance(tool_result, Exception):
                    async with self.job_ctx.tool_output_stash.lock:
                        self.job_ctx.tool_output_stash[tc.id] = tool_result

                is_tool_error = tool_result is None or isinstance(tool_result, Exception)
                if is_tool_error:
                    tool_failure_count += 1

                # 处理工具结果（区分图像生成和普通工具）
                # 对应 Rust: let (record_content, tool_message) = if let (Ok(_), Some(sentinel)) = (&tool_result, image_sentinel.as_ref()) { ... } else { ... };
                if tool_result is not None and not isinstance(tool_result, Exception) and image_sentinel is not None:
                    record_content = image_generation_record_content(image_sentinel)
                    tool_message = image_generation_summary_tool_message(
                        self.agent.safety(), tc.name, tc.id, image_sentinel,
                    )
                else:
                    tool_result_for_process = tool_result if tool_result is not None else Exception("No result")
                    record_content, tool_message = process_tool_result(
                        self.agent.safety(), tc.name, tc.id, tool_result_for_process,
                    )

                # 将清理后的结果记录到线程中
                # 对应 Rust: { let mut sess = self.session.lock().await; ... }
                async with self.session.lock:
                    thread = self.session.threads.get(self.thread_id)
                    if thread is not None:
                        turn = thread.last_turn()
                        if turn is not None:
                            if is_tool_error:
                                turn.record_tool_error_for(tc.id, record_content)
                            else:
                                turn.record_tool_result_for(tc.id, record_content)

                reason_ctx.messages.append(tool_message)

        # ---------- 报告批次中是否所有工具都失败 ----------
        # 对应 Rust: reason_ctx.last_tool_batch_all_failed = total_tools > 0 && tool_failure_count == total_tools;
        reason_ctx.last_tool_batch_all_failed = total_tools > 0 and tool_failure_count == total_tools

        # ---------- 审批处理 ----------
        # 审批暂停优先于显示认证提示。持久化提示以便在审批后可以重放。
        # 对应 Rust: if let Some((approval_idx, tc, tool, allow_always)) = approval_needed { ... }
        if approval_needed is not None:
            approval_idx, tc, tool, allow_always = approval_needed

            if selected_auth_prompt is not None:
                ext_name, auth_data = selected_auth_prompt
                await emit_auth_required_status(
                    self.agent.channels,
                    self.message,
                    ext_name,
                    auth_data.instructions,
                    auth_data.auth_url,
                    auth_data.setup_url,
                    None,
                )

            display_params = redact_params(tc.arguments, tool.sensitive_params())
            pending = PendingApproval(
                request_id=uuid.uuid4(),
                tool_name=tc.name,
                parameters=tc.arguments,
                display_parameters=display_params,
                description=tool.description(),
                tool_call_id=tc.id,
                context_messages=list(reason_ctx.messages),
                deferred_tool_calls=list(tool_calls[approval_idx + 1:]),
                selected_auth_prompt=persist_selected_auth_prompt(selected_auth_prompt),
                user_timezone=self.user_tz.zone if self.user_tz else None,
                allow_always=allow_always,
            )

            return LoopOutcome.NeedApproval(pending)

        # ---------- 认证处理 ----------
        # 对应 Rust: if let Some((ext_name, auth_data)) = selected_auth_prompt { ... }
        if selected_auth_prompt is not None:
            ext_name, auth_data = selected_auth_prompt

            if auth_data.awaiting_token:
                instructions = auth_instructions_or_default(auth_data.instructions)

                async with self.session.lock:
                    thread = self.session.threads.get(self.thread_id)
                    if thread is not None:
                        thread.enter_auth_mode(ext_name)

                await emit_auth_required_status(
                    self.agent.channels,
                    self.message,
                    ext_name,
                    instructions,
                    auth_data.auth_url,
                    auth_data.setup_url,
                    str(self.thread_id),
                )

                return LoopOutcome.AuthPending(instructions)

            await emit_auth_required_status(
                self.agent.channels,
                self.message,
                ext_name,
                auth_data.instructions,
                auth_data.auth_url,
                auth_data.setup_url,
                None,
            )

        # 无暂停 —— 继续循环
        # 对应 Rust: Ok(None)
        return None


async def execute_chat_tool_standalone(
        tools: "ToolRegistry",
        safety: "SafetyLayer",
        tool_name: str,
        params: dict,
        job_ctx: "JobContext",
) -> str:
    """
    在不借用 `&self` 的情况下执行聊天工具。

    这个独立函数使得从生成的并行任务中可以并行调用工具，
    而这些任务无法借用 `&self`。它委托给共享的
    `execute_tool_with_safety` 管道。
    参数:
        tools: 工具注册表的引用。
        safety: 安全层的引用。
        tool_name: 要执行的工具名称。
        params: 工具参数的字典。
        job_ctx: 作业上下文的引用。

    返回:
        str: 工具执行的结果字符串。
    """
    # 委托给 execute_tool_with_safety 管道
    # 对应 Rust: crate::tools::execute::execute_tool_with_safety(tools, safety, tool_name, params.clone(), job_ctx).await
    return await execute_tool_with_safety(
        tools,
        safety,
        tool_name,
        dict(params),  # params.clone() —— 克隆参数字典
        job_ctx,
    )


@dataclass
class ParsedAuthData:
    """
    用于发送 StatusUpdate::AuthRequired 的已解析认证结果字段。
    """
    # 可选的扩展名称。
    extension_name: Optional[ExtensionName] = None
    # 可选的认证说明。
    instructions: Optional[str] = None
    # 可选的认证 URL。
    auth_url: Optional[str] = None
    # 可选的设置 URL。
    setup_url: Optional[str] = None
    # 是否正在等待令牌输入。
    awaiting_token: bool = False


DEFAULT_AUTH_TOKEN_INSTRUCTIONS = "Please provide your API token/key."

def normalize_extension_name(value: Optional[str]) -> Optional[ExtensionName]:
    """
    将可选的字符串值规范化为 ExtensionName。
    参数:
        value: 可选的原始扩展名称字符串。

    返回:
        Optional[ExtensionName]: 解析成功时返回 ExtensionName，否则返回 None。
    """
    if value is None:
        return None
    try:
        return ExtensionName(value)
    except (ValueError, TypeError):
        return None

def contextual_tool_message(tool_calls: List[ToolCall]) -> str:
    """
    根据工具名称构建上下文相关的思考消息。

    对于单个工具调用，返回类似 "Running command..." 或 "Fetching page..." 的消息，
    而不是通用的 "Executing 2 tool(s)..."；
    对于多个工具调用，回退到 "Executing N tool(s)..."。

    参数:
        tool_calls: 要生成消息的工具调用列表。

    返回:
        str: 上下文相关的思考状态消息。
    """
    # 单个工具调用：根据工具名称返回特定消息
    if len(tool_calls) == 1:
        name = tool_calls[0].name
        messages = {
            "shell": "Running command...",
            "web_fetch": "Fetching page...",
            "memory_search": "Searching memory...",
            "memory_write": "Writing to memory...",
            "memory_read": "Reading memory...",
            "http_request": "Making HTTP request...",
            "file_read": "Reading file...",
            "file_write": "Writing file...",
            "json_transform": "Transforming data...",
        }
        # 如果工具名称在映射中，返回对应消息；否则返回通用格式
        # 对应 Rust: name => format!("Running {name}..."),
        return messages.get(name, f"Running {name}...")

    # 多个工具调用：返回通用数量消息
    return f"Executing {len(tool_calls)} tool(s)..."


