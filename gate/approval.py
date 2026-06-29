# 批准门控——包装 `Tool::requires_approval()`。
#
# 用可组合的门控替代 `EffectBridgeAdapter::execute_action()` 中的内联批准检查（步骤 1），
# 该门控可处理交互式、自主和容器执行模式。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from engine.gate import ExecutionGate, ExecutionMode, GateContext, GateDecision, ResumeKind
from tools.rate_limiter import RateLimiter
from tools import ApprovalRequirement, ToolRegistry


@dataclass
class ApprovalGate:
    """
    检查 `Tool.requires_approval()` 并根据执行模式发出 `Pause(Approval)` 或 `Deny` 的门控。

    **数据库持久化的权限：** 此门控检查 `ctx.auto_approved` 以获取会话范围的自动批准。
    构造 [`GateContext`] 的调用者负责从数据库持久化的 `PermissionState::AlwaysAllow` 条目
    （通过 `effective_permission()`）预填充该集合。
    v1 调度器在 `dispatcher.rs` 回合开始时水合该集合；
    v2 通过 `EffectBridgeAdapter::auto_approved` 查询持久化权限。
    "始终批准"决策的**持久化**由 `bridge/router.rs` 中的 `persist_always_allow()` (v2)
    和 `agent/thread_ops.rs` 中的 `process_approval()` (v1) 处理。

    优先级：100（在速率限制之后，在中继通道检查之后）。
    """

    tools: ToolRegistry

    @property
    def name(self) -> str:
        return "approval"

    @property
    def priority(self) -> int:
        return 100

    async def evaluate(self, ctx: GateContext) -> GateDecision:
        result = await self.tools.get_resolved(ctx.action_name)
        if result is None:
            return GateDecision.Allow  # 未知工具——让执行处理

        _, tool = result
        is_auto_approved = ctx.action_name in ctx.auto_approved
        # 使用原始参数进行批准检查（适配器在执行前规范化参数，
        # 但批准检查应使用 LLM 提供的参数，以便破坏性检测正常工作）
        requirement = tool.requires_approval(ctx.parameters)

        if ctx.execution_mode == ExecutionMode.Interactive:
            if requirement == ApprovalRequirement.Never:
                return GateDecision.Allow
            elif requirement == ApprovalRequirement.UnlessAutoApproved:
                if is_auto_approved:
                    return GateDecision.Allow
                else:
                    # 检查凭证支持的 HTTP 自动批准
                    if ctx.action_name in ("http", "http_request"):
                        reg = self.tools.credential_registry()
                        if reg is not None:
                            host = extract_host_from_params(ctx.parameters)
                            if host is not None and reg.has_credentials_for_host(host):
                                return GateDecision.Allow
                    return GateDecision.Pause(
                        reason=f"工具 '{ctx.action_name}' 需要批准才能执行。",
                        resume_kind=ResumeKind.Approval(allow_always=True),
                    )
            elif requirement == ApprovalRequirement.Always:
                return GateDecision.Pause(
                    reason=f"工具 '{ctx.action_name}' 需要对此操作进行显式批准。",
                    resume_kind=ResumeKind.Approval(allow_always=False),
                )

        elif ctx.execution_mode == ExecutionMode.InteractiveAutoApprove:
            if requirement in (ApprovalRequirement.Never, ApprovalRequirement.UnlessAutoApproved):
                # 自动批准模式：shell、file_write、http 等无需提示即可继续。
                # 其他安全措施（租约、速率限制、钩子、认证门控）仍然适用。
                return GateDecision.Allow
            elif requirement == ApprovalRequirement.Always:
                return GateDecision.Pause(
                    reason=(
                        f"工具 '{ctx.action_name}' 需要显式批准"
                        "（自动批准不涵盖此操作）。"
                    ),
                    resume_kind=ResumeKind.Approval(allow_always=False),
                )

        elif ctx.execution_mode == ExecutionMode.Autonomous:
            if requirement in (ApprovalRequirement.Never, ApprovalRequirement.UnlessAutoApproved):
                # Never 和 UnlessAutoApproved 在自主模式下被允许
                # （回归修复：0e5f1b12 — is_blocked 正在拒绝 Never 工具）
                return GateDecision.Allow
            elif requirement == ApprovalRequirement.Always:
                return GateDecision.Deny(
                    reason=(
                        f"工具 '{ctx.action_name}' 需要显式批准，无法自主运行。"
                    ),
                )

        elif ctx.execution_mode == ExecutionMode.Container:
            return GateDecision.Allow

        return GateDecision.Allow


class AuthenticationGate:
    """
    检查 `AuthManager.check_action_auth()` 是否缺少凭证的门控。

    优先级：200（在批准之后——对被拒绝的工具检查凭证没有意义）。

    目前是透传——实际的认证检查在 `effect_adapter.rs` 步骤 1.7 中保持内联，
    直到第 4 阶段迁移完成。
    """

    @property
    def name(self) -> str:
        return "authentication"

    @property
    def priority(self) -> int:
        return 200

    async def evaluate(self, ctx: GateContext) -> GateDecision:
        # 实际的认证检查通过 EffectBridgeAdapter 的 auth_manager 执行——
        # 在第 4 阶段迁移期间，此门控委托到那里。
        # 目前，effect_adapter.rs 步骤 1.7 中的内联检查保持不变。
        return GateDecision.Allow

@dataclass
class HookGate:
    """
    包装 `HookRegistry::run(BeforeToolCall)` 的门控。

    优先级：300（在批准和认证之后——钩子可以自定义行为，
    但不应抢占面向用户的批准/认证流程）。
    """

    hooks: HookRegistry
    tools: ToolRegistry

    @property
    def name(self) -> str:
        return "hook"

    @property
    def priority(self) -> int:
        return 300

    async def evaluate(self, ctx: GateContext) -> GateDecision:
        tool = await self.tools.get(ctx.action_name)
        redacted_params = (
            redact_params(ctx.parameters, tool.sensitive_params())
            if tool is not None
            else ctx.parameters
        )

        hook_event = HookEvent.ToolCall(
            tool_name=ctx.action_name,
            parameters=redacted_params,
            user_id=ctx.user_id,
            context=f"gate:{ctx.thread_id}",
        )

        try:
            outcome = await self.hooks.run(hook_event)
        except HookError.Rejected as e:
            return GateDecision.Deny(
                reason=f"工具 '{ctx.action_name}' 被钩子阻止: {e.reason}"
            )
        except Exception as e:
            logger.debug(
                "钩子错误（故障开放）, tool=%s, error=%s",
                ctx.action_name,
                e,
            )
            return GateDecision.Allow

        if isinstance(outcome, HookOutcome.Reject):
            return GateDecision.Deny(
                reason=f"工具 '{ctx.action_name}' 被钩子阻止: {outcome.reason}"
            )
        else:
            return GateDecision.Allow


class RateLimitGate:
    """
    包装每用户每工具 `RateLimiter` 的门控。

    优先级：50（在批准之前运行——对速率限制的工具快速拒绝）。
    """

    tools: ToolRegistry
    rate_limiter: RateLimiter

    @property
    def name(self) -> str:
        return "rate_limit"

    @property
    def priority(self) -> int:
        return 50

    async def evaluate(self, ctx: GateContext) -> GateDecision:
        tool = await self.tools.get(ctx.action_name)
        if tool is None:
            return GateDecision.Allow

        rl_config = tool.rate_limit_config()
        if rl_config is None:
            return GateDecision.Allow

        result = await self.rate_limiter.check_and_record(
            ctx.user_id, ctx.action_name, rl_config
        )

        if isinstance(result, RateLimitResult.Limited):
            return GateDecision.Deny(
                reason=(
                    f"工具 '{ctx.action_name}' 受到速率限制。"
                    f"请在 {result.retry_after:.0f} 秒后重试。"
                )
            )
        else:
            return GateDecision.Allow


class RelayChannelGate:
    """
    在中继通道上自动拒绝需要批准的工具的门控。

    修复 v1/v2 不一致问题，其中中继通道自动拒绝仅在 v1 调度器中，
    而不在 v2 路由器中。

    优先级：80（在批准之前——在无法交互响应的通道上显示批准 UI 没有意义）。
    """

    @property
    def name(self) -> str:
        return "relay_channel"

    @property
    def priority(self) -> int:
        return 80

    async def evaluate(self, ctx: GateContext) -> GateDecision:
        is_relay = ctx.source_channel.endswith("-relay")
        if not is_relay:
            return GateDecision.Allow

        if ctx.action_def.requires_approval:
            return GateDecision.Deny(
                reason=(
                    f"工具 '{ctx.action_name}' 需要批准，"
                    f"但中继通道 '{ctx.source_channel}' 无法提供交互式响应。"
                )
            )
        else:
            return GateDecision.Allow