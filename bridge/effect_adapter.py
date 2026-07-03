"""
效果桥接适配器——将 `ToolRegistry` + `SafetyLayer` 包装为 `ironclaw_engine::EffectExecutor`。

这是引擎与现有 IronClaw 基础设施之间的安全边界。所有 v1 安全控制都在此处强制执行：
    - 工具批准（requires_approval，自动批准跟踪）
    - 输出清理（sanitize_tool_output + wrap_for_llm）
    - Hook 拦截（BeforeToolCall）
    - 敏感参数脱敏
    - 速率限制（按用户、按工具）

主要作用:
    - 工具执行（execute_action）
    - 可用动作列表（available_actions）
    - 可用能力列表（available_capabilities）
    - 通过 AuthManager 实现预飞行认证检查，在工具执行前验证凭证是否存在
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Set, Any

from auth.extension import (AuthCheckResult, AuthManager, LatentActionExecution, ToolReadiness)
from auth.oauth import sanitize_auth_url
from bridge.action_discovery import ActionDiscovery
from bridge.action_projector import ActionProjector
from bridge.capability_projector import CapabilityProjector
from bridge.sandbox import (InterceptOutcome, maybe_intercept)
from bridge.tool_permissions import (ToolPermissionResolution, ToolPermissionSnapshot)
from context.JobContext
from extensions import InstalledExtension
from ironclaw_safety import SafetyLayer
from tools.rate_limiter import RateLimiter

from engine import (
    ActionDef,
    ActionInventory,
    ActionResult,
    CapabilityLease,
    CapabilityRegistry,
    CapabilitySummary,
    EffectExecutor,
    EngineError,
    MountError,
    Store,
    ThreadExecutionContext,
    WorkspaceMounts,
MissionManager
)
from hooks import (HookEvent, HookOutcome, HookRegistry)
from skills import SkillRegistry
from tools import (ApprovalRequirement, Tool)
from tools import ToolRegistry
from tools.permissions import PermissionState
from llm.recording import HttpInterceptor
from bridge import ExternalToolCatalog

logger = logging.getLogger(__name__)


# ── 工具批准上下文 ───────────────────────────────────────────

@dataclass
class ToolApprovalContext:
    """工具批准上下文"""
    action_name: str
    lookup_name: str
    parameters: dict
    lease: CapabilityLease
    context: ThreadExecutionContext
    approval_already_granted: bool = False


@dataclass
class ToolInfoSnapshotContext:
    """工具信息快照上下文"""
    action_name: str
    canonical_action_name: str
    lookup_name: str
    parameters: dict
    lease: CapabilityLease
    context: ThreadExecutionContext
    approval_already_granted: bool = False
    started_at: float = 0.0


# ── 效果桥接适配器 ───────────────────────────────────────────
@dataclass
class EffectBridgeAdapter(EffectExecutor):
    """
    包装现有工具管道以实现引擎的 `EffectExecutor`

    在适配器边界强制执行所有 v1 安全控制：
    工具批准、输出清理、钩子、速率限制和调用限制
    """

    tools: ToolRegistry
    safety: SafetyLayer
    hooks: HookRegistry
    # 来自代理配置/环境变量的全局自动批准模式
    auto_approve_tools: bool = field(default=False, init=False)
    # 用户已"始终"批准的工具（在会话内持久化）
    auto_approved: Set[str] = field(default_factory=set, init=False)
    # 每步工具调用计数器（在步骤之间外部重置）
    call_count: int = field(default=0, init=False)
    # 每用户每工具滑动窗口速率限制器
    rate_limiter: RateLimiter = field(default_factory=RateLimiter, init=False)
    # 用于处理 mission_* 函数调用的任务管理器
    mission_manager: Optional[MissionManager] = field(default=None, init=False)
    # 用于预检凭证检查的集中式认证管理器
    auth_manager: Optional[AuthManager] = field(default=None, init=False)
    # 可选的 HTTP 拦截器用于追踪记录/重放
    http_interceptor: Optional[HttpInterceptor] = field(default=None, init=False)
    # 引擎 v2 存储，用于将实时安装的 v1 技能镜像到 `DocType::Skill`
    engine_store: Optional[Store] = field(default=None, init=False)
    # v1 技能注册表，用于加载刚安装的技能以进行 v2 同步
    skill_registry: Optional[SkillRegistry] = field(default=None, init=False)
    # 可选的每项目工作区挂载表
    workspace_mounts: Optional[WorkspaceMounts] = field(default=None, init=False)
    # 引擎能力注册表
    capability_registry: Optional[CapabilityRegistry] = field(default=None, init=False)
    # 每线程外部工具目录
    external_tool_catalog: Optional[ExternalToolCatalog] = field(default=None, init=False)

    def with_global_auto_approve(self, enabled: bool) -> "EffectBridgeAdapter":
        """镜像 v1 调度器行为，用于全局自动批准的工具"""
        self.auto_approve_tools = enabled
        return self

    async def set_external_tool_catalog(self, catalog: Any) -> None:
        """安装每线程外部工具目录。在桥接初始化时设置一次；
        Responses API 处理程序通过其自己的引用克隆将工具注册到同一目录实例上
        """
        async with self._lock:
            self.external_tool_catalog = catalog

    async def external_tool_catalog_get(self) -> Optional[Any]:
        """查找目录（如果已安装）用于只读使用"""
        async with self._lock:
            return self.external_tool_catalog

    def external_tool_catalog_keys(self, context: Any) -> List[Any]:
        """解析此 `ThreadExecutionContext` 可能具有调用者提供的工具注册的所有目录键。
        引擎 `thread_id` 是桥接的后生成 `transfer` 之后的规范键，
        但执行器任务可以在该传输完成之前运行 —
        `conversation_scope`（由桥接标记到线程元数据中）是目录注册的原始调用者侧键，
        用作竞态窗口回退
        """
        keys = [context.thread_id]
        if context.conversation_scope is not None and context.conversation_scope != context.thread_id:
            keys.append(context.conversation_scope)
        return keys

    async def set_workspace_mounts(self, mounts: Optional[Any]) -> None:
        """在此适配器上安装每项目工作区挂载表。设置后，
        沙箱合格的工具调用（`file_read`、`file_write`、`list_dir`、
        `apply_patch`、`shell`）其路径参数解析到挂载中的将通过挂载后端调度，
        而不是主机工具

        传递 `None` 以移除挂载表并恢复所有工具的直接主机执行
        """
        async with self._lock:
            self.workspace_mounts = mounts

    async def set_capability_registry(self, registry: Any) -> None:
        """安装引擎能力注册表，以便 `available_actions()` 可以将引擎原生能力
        （任务等）的动作显示给 LLM。在桥接设置时调用一次，
        在 `router.rs` 完成注册所有能力之后
        """
        async with self._lock:
            self.capability_registry = registry

    async def set_http_interceptor(self, interceptor: Any) -> None:
        """在此适配器上安装追踪 HTTP 拦截器。适配器为工具调度构造的每个
        JobContext 将携带此拦截器的克隆，因此 http 感知工具将通过它记录/重放
        """
        async with self._lock:
            self.http_interceptor = interceptor

    async def set_engine_store(self, store: Any) -> None:
        """提供实时引擎存储，以便 `skill_install` 可以立即将已安装的技能同步到 v2 文档空间"""
        async with self._lock:
            self.engine_store = store

    async def set_skill_registry(self, registry: Any) -> None:
        """提供 v1 技能注册表，以便 `skill_install` 可以在工具返回其名称后解析规范安装的技能"""
        async with self._lock:
            self.skill_registry = registry

    async def auto_approve_tool(self, tool_name: str) -> None:
        """标记工具为自动批准（用户说"始终"）"""
        async with self._lock:
            self.auto_approved.add(tool_name)

    async def revoke_auto_approve(self, tool_name: str) -> None:
        """撤销工具的自动批准（恢复失败时回滚）"""
        async with self._lock:
            self.auto_approved.discard(tool_name)

    def tools_ref(self) -> Any:
        """访问底层工具注册表（用于参数编辑等）"""
        return self.tools

    def safety_ref(self) -> Any:
        """访问底层安全层

        桥接路由器使用此在广播到 SSE 之前通过泄露检测器编辑仅详细的可观察性事件
        （特别是 `CodeExecuted`）。引擎 crate 原始发出这些事件，
        因为它没有对 `ironclaw_safety` 的依赖；因此清理发生在此适配器边界
        """
        return self.safety

    async def set_auth_manager(self, mgr: Any) -> None:
        """设置认证管理器用于预检凭证检查"""
        async with self._lock:
            self.auth_manager = mgr

    async def set_mission_manager(self, mgr: Any) -> None:
        """设置任务管理器（在引擎初始化后调用）"""
        async with self._lock:
            self.mission_manager = mgr

    async def mission_manager_get(self) -> Optional[Any]:
        """获取任务管理器（如果可用）"""
        async with self._lock:
            return self.mission_manager

    async def fetch_extension_list(
            self, auth_manager: Optional[Any], context: Any
    ) -> Optional[List[Any]]:
        """获取扩展列表，使用短生命周期缓存（如果可用）。
        当 auth_manager 存在时返回列表，否则返回 None
        """
        if auth_manager is None:
            return None
        try:
            return await auth_manager.list_capability_extensions(context.user_id)
        except Exception as error:
            logger.debug(
                f"加载扩展库存失败；返回空列表: user_id={context.user_id}, error={error}"
            )
            return []

    async def fetch_extension_map(
            self, auth_manager: Optional[Any], context: Any
    ) -> Optional[Dict[str, Any]]:
        """获取扩展映射，使用短生命周期缓存（如果可用）。
        当 auth_manager 存在时返回映射，否则返回 None
        """
        extensions = await self.fetch_extension_list(auth_manager, context)
        if extensions is None:
            return None
        return {ext.name: ext for ext in extensions}

    async def resolved_user_permission_for_tool(
            self, lookup_name: str, context: Any
    ) -> ToolPermissionResolution:
        """解析用户对工具的权限"""
        snapshot = await ToolPermissionSnapshot.load(self.tools, context.user_id)
        return snapshot.resolve_permission(lookup_name)

    @staticmethod
    def ensure_tool_not_disabled(
            action_name: str, user_permission: ToolPermissionResolution
    ) -> None:
        """确保工具未被禁用"""
        if user_permission.effective == PermissionState.Disabled:
            raise EngineError.LeaseDenied(
                reason=f"工具 '{action_name}' 对此用户已禁用。"
            )

    async def enforce_tool_permission(
            self,
            approval: ToolApprovalContext,
            tool: Any,
            user_permission: ToolPermissionResolution,
    ) -> None:
        """强制执行工具权限"""
        if user_permission.effective == PermissionState.Disabled:
            self.ensure_tool_not_disabled(approval.action_name, user_permission)

        if approval.approval_already_granted:
            return

        approval_requirement = tool.requires_approval(approval.parameters)
        # `skill_install` 对参数敏感：重复安装是保证的无操作，
        # 并故意返回 `ApprovalRequirement::Never`。
        # 保留该 v1 契约，即使工具的默认权限对于真实安装是 ask-each-time
        if approval.lookup_name in ("skill_install", "skill-install") and \
                approval_requirement == ApprovalRequirement.Never:
            return

        if approval_requirement == ApprovalRequirement.Always:
            raise self.gate_paused(
                "approval",
                approval.action_name,
                approval.context.current_call_id,
                approval.parameters,
                ResumeKind.Approval(allow_always=False),
                None,
                approval.lease,
            )

        if user_permission.effective == PermissionState.AlwaysAllow:
            return

        if user_permission.effective == PermissionState.AskEachTime:
            is_explicit_ask = user_permission.explicit == PermissionState.AskEachTime
            is_approved = not is_explicit_ask and (
                    self.auto_approve_tools or approval.lookup_name in self.auto_approved
            )
            if is_approved:
                return
            raise self.gate_paused(
                "approval",
                approval.action_name,
                approval.context.current_call_id,
                approval.parameters,
                ResumeKind.Approval(allow_always=True),
                None,
                approval.lease,
            )

    @staticmethod
    def snapshot_action_result(
            context: Any, action_name: str, output: Any, is_error: bool, started_at: float
    ) -> ActionResult:
        """快照动作结果"""
        call_id = context.current_call_id or synthetic_action_call_id(action_name)
        duration_ms = int((time.monotonic() - started_at) * 1000)
        return ActionResult(
            call_id=call_id,
            action_name=action_name,
            output=output,
            is_error=is_error,
            duration_ms=duration_ms,
        )

    async def execute_tool_info_from_snapshot(
            self, tool_info: ToolInfoSnapshotContext
    ) -> ActionResult:
        """从快照执行 tool_info"""
        resolved_tool = await self.tools.get_resolved(tool_info.lookup_name)
        user_permission = await self.resolved_user_permission_for_tool(
            tool_info.lookup_name, tool_info.context
        )
        self.ensure_tool_not_disabled(tool_info.action_name, user_permission)

        if resolved_tool is not None:
            _, tool = resolved_tool
            await self.enforce_tool_permission(
                ToolApprovalContext(
                    action_name=tool_info.action_name,
                    lookup_name=tool_info.lookup_name,
                    parameters=tool_info.parameters,
                    lease=tool_info.lease,
                    context=tool_info.context,
                    approval_already_granted=tool_info.approval_already_granted,
                ),
                tool,
                user_permission,
            )

        inventory = tool_info.context.available_action_inventory_snapshot
        actions = tool_info.context.available_actions_snapshot

        if inventory is not None:
            snapshot_result = ActionDiscovery.tool_info(tool_info.parameters, inventory)
        elif actions is not None:
            snapshot_result = ActionDiscovery.tool_info_from_actions(tool_info.parameters, actions)
        else:
            error_msg = "tool_info: 此执行上下文中动作清单不可用"
            sanitized = self.safety.sanitize_tool_output("tool_info", error_msg)
            return self.snapshot_action_result(
                tool_info.context,
                tool_info.canonical_action_name,
                {"error": sanitized.content},
                True,
                tool_info.started_at,
            )

        if snapshot_result.is_error:
            error_msg = f"工具 tool_info 失败: {snapshot_result.error}"
            sanitized = self.safety.sanitize_tool_output("tool_info", error_msg)
            return self.snapshot_action_result(
                tool_info.context,
                tool_info.canonical_action_name,
                {"error": sanitized.content},
                True,
                tool_info.started_at,
            )

        if snapshot_result.result is None:
            requested = tool_info.parameters.get("name", "<missing>")
            error_msg = (
                f"tool_info: 在此执行上下文中没有名为 '{requested}' 的可调用或可发现动作"
            )
            sanitized = self.safety.sanitize_tool_output("tool_info", error_msg)
            return self.snapshot_action_result(
                tool_info.context,
                tool_info.canonical_action_name,
                {"error": sanitized.content},
                True,
                tool_info.started_at,
            )

        return self.snapshot_action_result(
            tool_info.context,
            tool_info.canonical_action_name,
            snapshot_result.result,
            False,
            tool_info.started_at,
        )

    async def sync_skill_install_result(
            self, output_value: dict, project_id: Any
    ) -> None:
        """同步技能安装结果"""
        skill_name = output_value.get("name") if isinstance(output_value, dict) else None
        if not skill_name:
            return

        store = self.engine_store
        if store is None:
            return

        registry = self.skill_registry
        if registry is None:
            return

        guard = registry.read() if hasattr(registry, 'read') else registry
        skill = guard.find_by_name(skill_name)
        if skill is None:
            raise EngineError.Skill(
                reason=f"skill_install 报告了 '{skill_name}'，但在注册表中未找到已安装的技能"
            )

        await sync_v1_skill_to_store(skill, store, project_id)

    async def ensure_project_for_memory_write(
            self, target: str, user_id: str
    ) -> Optional[Any]:
        """确保 `projects/<slug>/...` 写入存在 Project 实体

        引擎将工作区目录视为项目的真实来源：在 `projects/<slug>/` 下写入任何文件
        即声明项目存在。此钩子在成功的 `memory_write` 之后运行，
        在存储中查找或创建匹配的 Project，并返回其 ID，
        以便调用者可以将 `project_id` 拼接到工具输出中

        如果目标不在 `projects/<slug>/...` 下（常规工作区写入）或无法派生可用的 slug
        则返回 None — 非致命，调用者只是跳过丰富化
        """
        slug = extract_project_slug_from_target(target)
        if slug is None:
            return None

        mgr = self.mission_manager
        if mgr is None:
            return None

        store = mgr.store()
        existing = await store.list_projects(user_id)
        slug_lower = slug.lower()
        matched = None
        for p in existing:
            if p.user_id == user_id and (
                    slugify_simple(p.name) == slug_lower or p.name.lower() == slug_lower
            ):
                matched = p
                break

        if matched is not None:
            return matched.id

        project = Project.new(user_id, slug, "")
        pid = project.id
        await store.save_project(project)
        return pid

    @staticmethod
    def gate_paused(
            gate_name: str,
            action_name: str,
            call_id: Optional[str],
            parameters: dict,
            resume_kind: Any,
            resume_output: Optional[dict],
            paused_lease: Optional[Any],
    ) -> EngineError:
        """创建门控暂停错误"""
        return EngineError.GatePaused(
            gate_name=gate_name,
            action_name=action_name,
            call_id=call_id or "",
            parameters=parameters,
            resume_kind=resume_kind,
            resume_output=resume_output,
            paused_lease=paused_lease,
        )

    @staticmethod
    def auth_gate_from_extension_result(
            action_name: str,
            parameters: dict,
            context: Any,
            output_value: dict,
            lease: Any,
    ) -> Optional[EngineError]:
        """从扩展结果构建认证门控"""
        if not isinstance(output_value, dict):
            return None

        status = output_value.get("status")
        name = output_value.get("name")
        if not status or not name:
            return None

        if status in ("awaiting_authorization", "awaiting_token"):
            credential_name_str = output_value.get("credential_name") or name
            instructions = output_value.get("instructions", "完成认证以继续。")
            auth_url = sanitize_auth_url(output_value.get("auth_url"))

            return EffectBridgeAdapter.gate_paused(
                "authentication",
                action_name,
                context.current_call_id,
                parameters,
                ResumeKind.Authentication(
                    credential_name=credential_name_str,
                    instructions=instructions,
                    auth_url=auth_url,
                ),
                output_value,
                lease,
            )
        return None

    async def handle_mission_call(
            self, action_name: str, params: dict, context: Any
    ) -> Optional[ActionResult]:
        """处理 mission_* 和 routine_* 函数调用。routine_* 是别名：
        例程模式被转换为 mission_* 参数并通过相同的任务管理器调度。
        如果动作名称既不是 mission 也不是 routine 调用，则返回 None
        """
        # 在调度之前将 routine_* 别名转换为 mission_*
        routine_alias = routine_to_mission_alias(action_name, params)
        if routine_alias is not None:
            effective_action = routine_alias.mission_action
            effective_params = routine_alias.mission_params
            post_create_update = routine_alias.post_create_update
        else:
            effective_action = action_name
            effective_params = params
            post_create_update = None

        mgr = self.mission_manager
        if mgr is None:
            return None

        if effective_action == "mission_create":
            if should_reject_immediate_mission_create(context):
                return ActionResult(
                    call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                    action_name=effective_action,
                    output={"error": "拒绝为即时一次性请求创建任务。用户要求现在运行此任务，"
                                     "因此请在当前前台线程中完成任务。仅当用户明确要求调度、"
                                     "自动化或创建重复例程/任务时才调用 mission_create/routine_create。"},
                    is_error=True,
                    duration_ms=0,
                )

            name = effective_params.get("name") or "未命名任务"
            goal = effective_params.get("goal") or ""
            cadence_str = effective_params.get("cadence")

            if cadence_str is None:
                return ActionResult(
                    call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                    action_name=effective_action,
                    output={"error": "cadence 是必需的。使用 'manual'、cron 表达式（例如 '0 9 * * *'）、"
                                     "'event:<channel>:<pattern>'（例如 'event:telegram:.*'）或 'webhook:<path>'"},
                    is_error=True,
                    duration_ms=0,
                )

            timezone = effective_params.get("timezone") or getattr(context.user_timezone, 'name', lambda: None)
            if timezone:
                timezone = ValidTimezone.parse(timezone) if hasattr(ValidTimezone, 'parse') else timezone

            try:
                cadence = parse_cadence(cadence_str, timezone)
            except Exception as e:
                return ActionResult(
                    call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                    action_name=effective_action,
                    output={"error": str(e)},
                    is_error=True,
                    duration_ms=0,
                )

            notify_channels = effective_params.get("notify_channels", [])
            if not isinstance(notify_channels, list):
                notify_channels = [context.source_channel] if context.source_channel else []

            target_project = effective_params.get("project_id")
            if target_project:
                try:
                    target_project = await resolve_project_ref(mgr.store(), target_project, context)
                except Exception as e:
                    return ActionResult(
                        call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                        action_name=effective_action,
                        output={"error": str(e)},
                        is_error=True,
                        duration_ms=0,
                    )
            else:
                target_project = context.project_id

            try:
                mid = await mgr.create_mission(
                    target_project, context.user_id, name, goal, cadence, notify_channels,
                )
                return ActionResult(
                    call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                    action_name=effective_action,
                    output={"mission_id": str(mid), "name": name, "status": "created"},
                    is_error=False,
                    duration_ms=0,
                )
            except Exception as e:
                return ActionResult(
                    call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                    action_name=effective_action,
                    output={"error": str(e)},
                    is_error=True,
                    duration_ms=0,
                )

        elif effective_action == "mission_list":
            try:
                missions = await mgr.list_missions(context.project_id, context.user_id)
                result_list = [
                    {
                        "id": str(m.id),
                        "name": m.name,
                        "goal": m.goal,
                        "status": str(m.status),
                        "threads": len(m.thread_history),
                    }
                    for m in missions
                ]
                return ActionResult(
                    call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                    action_name=effective_action,
                    output=result_list,
                    is_error=False,
                    duration_ms=0,
                )
            except Exception as e:
                return ActionResult(
                    call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                    action_name=effective_action,
                    output={"error": str(e)},
                    is_error=True,
                    duration_ms=0,
                )

        elif effective_action == "mission_fire":
            try:
                mid = await resolve_mission_id(mgr, context.project_id, context.user_id, effective_params)
                tid = await mgr.fire_mission(mid, context.user_id, None)
                if tid is not None:
                    return ActionResult(
                        call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                        action_name=effective_action,
                        output={"thread_id": str(tid), "status": "fired"},
                        is_error=False,
                        duration_ms=0,
                    )
                else:
                    return ActionResult(
                        call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                        action_name=effective_action,
                        output={"status": "not_fired", "reason": "任务已终止或预算耗尽"},
                        is_error=False,
                        duration_ms=0,
                    )
            except Exception as e:
                return ActionResult(
                    call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                    action_name=effective_action,
                    output={"error": str(e)},
                    is_error=True,
                    duration_ms=0,
                )

        elif effective_action in ("mission_pause", "mission_resume"):
            try:
                mid = await resolve_mission_id(mgr, context.project_id, context.user_id, effective_params)
                if effective_action == "mission_pause":
                    await mgr.pause_mission(mid, context.user_id)
                else:
                    await mgr.resume_mission(mid, context.user_id)
                return ActionResult(
                    call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                    action_name=effective_action,
                    output={"status": "ok"},
                    is_error=False,
                    duration_ms=0,
                )
            except Exception as e:
                return ActionResult(
                    call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                    action_name=effective_action,
                    output={"error": str(e)},
                    is_error=True,
                    duration_ms=0,
                )

        elif effective_action == "mission_complete":
            try:
                mid = await resolve_mission_id(mgr, context.project_id, context.user_id, effective_params)
                await mgr.complete_mission(mid)
                return ActionResult(
                    call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                    action_name=effective_action,
                    output={"status": "completed"},
                    is_error=False,
                    duration_ms=0,
                )
            except Exception as e:
                return ActionResult(
                    call_id=context.current_call_id or synthetic_action_call_id(effective_action),
                    action_name=effective_action,
                    output={"error": str(e)},
                    is_error=True,
                    duration_ms=0,
                )

        return None

    def reset_call_count(self) -> None:
        """重置每步调用计数器（在线程/步骤之间调用）"""
        self.call_count = 0

    async def execute_resolved_pending_action(
            self,
            action_name: str,
            parameters: dict,
            lease: Any,
            context: Any,
            approval_already_granted: bool,
    ) -> ActionResult:
        """执行已解析的挂起动作"""
        return await self.execute_action_internal(
            action_name, parameters, lease, context, approval_already_granted,
        )

    async def execute_action_internal(
            self,
            action_name: str,
            parameters: dict,
            lease: Any,
            context: Any,
            approval_already_granted: bool,
    ) -> ActionResult:
        """内部动作执行"""
        start = time.monotonic()
        canonical_action_name = action_name

        if context.available_actions_snapshot is not None:
            resolved = ActionDiscovery.resolve(context.available_actions_snapshot, action_name)
            if resolved is not None:
                canonical_action_name = resolved.name

        resolved_name = await self.tools.resolve_name(canonical_action_name)
        lookup_name = resolved_name or canonical_action_name

        # ── 每步调用限制（防止放大循环）──
        MAX_CALLS_PER_STEP = 50
        self.call_count += 1
        if self.call_count >= MAX_CALLS_PER_STEP:
            raise EngineError.Effect(
                reason=f"工具调用限制已达到（{MAX_CALLS_PER_STEP} 次/代码步骤）。请将任务分解为多个步骤。"
            )

        # 处理任务调用
        mission_result = await self.handle_mission_call(canonical_action_name, parameters, context)
        if mission_result is not None:
            mission_result.duration_ms = int((time.monotonic() - start) * 1000)
            return mission_result

        # 处理 tool_info
        if canonical_action_name == "tool_info":
            return await self.execute_tool_info_from_snapshot(ToolInfoSnapshotContext(
                action_name=action_name,
                canonical_action_name=canonical_action_name,
                lookup_name=lookup_name,
                parameters=parameters,
                lease=lease,
                context=context,
                approval_already_granted=approval_already_granted,
                started_at=start,
            ))

        # 检查 v1 专用工具
        if is_v1_only_tool(lookup_name):
            raise EngineError.Effect(
                reason=f"工具 '{action_name}' 在引擎 v2 中不可用。告诉用户改用斜杠命令（例如 /routine、/job）。"
            )

        if is_v1_auth_tool(lookup_name):
            raise EngineError.Effect(
                reason=f"工具 '{action_name}' 在引擎 v2 中不可用。认证由内核自动处理。"
            )

        # 速率限制检查
        tool = await self.tools.get(lookup_name)
        if tool is not None and hasattr(tool, 'rate_limit_config') and self.rate_limiter is not None:
            rl_config = tool.rate_limit_config()
            if rl_config is not None:
                result = await self.rate_limiter.check_and_record(context.user_id, lookup_name, rl_config)
                if hasattr(result, 'is_limited') and result.is_limited:
                    raise EngineError.Effect(
                        reason=f"工具 '{action_name}' 已被速率限制。请在 {result.retry_after:.0f} 秒后重试。"
                    )

        # 执行工具
        job_ctx = JobContext.with_user(context.user_id, "engine_v2", f"Thread {context.thread_id}")
        if self.http_interceptor is not None:
            job_ctx.http_interceptor = self.http_interceptor

        try:
            output = await execute_tool_with_safety(
                self.tools, self.safety, lookup_name, parameters, job_ctx,
            )
        except Exception as e:
            error_msg = f"工具 '{lookup_name}' 失败: {e}"
            sanitized = self.safety.sanitize_tool_output(lookup_name, error_msg)
            return ActionResult(
                call_id=context.current_call_id or synthetic_action_call_id(action_name),
                action_name=action_name,
                output={"error": sanitized.content},
                is_error=True,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        sanitized = self.safety.sanitize_tool_output(lookup_name, output)
        wrapped = self.safety.wrap_for_llm(lookup_name, sanitized.content)
        try:
            output_value = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            output_value = wrapped

        return ActionResult(
            call_id=context.current_call_id or synthetic_action_call_id(action_name),
            action_name=action_name,
            output=output_value,
            is_error=False,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def is_known_credential(self, credential_name: str) -> bool:
        """防御凭证名称注入：工具可以编造包含攻击者选择的 `credential_name`
        的 `authentication_required` 错误来钓鱼用户。我们仅在名称对应于主机实际注册的凭证时才接受门控请求

        **故障关闭：** 当没有凭证注册表接线时，我们拒绝门控请求而不是接受它。
        没有注册表的测试/嵌入工具没有凭证名称的真实来源，在该模式下信任工具的声明
        会让任何工具提示用户输入任何凭证名称
        """
        registry = self.tools.credential_registry() if hasattr(self.tools, 'credential_registry') else None
        if registry is None:
            return False
        return registry.has_secret(credential_name) if hasattr(registry, 'has_secret') else False

    # ── EffectExecutor 接口实现 ──────────────────────────────

    async def execute_action(
            self,
            action_name: str,
            parameters: dict,
            lease: Any,
            context: Any,
    ) -> ActionResult:
        """执行能力动作"""
        # 外部工具短路。如果每线程目录声明了此动作名称，
        # 调用者将执行它；我们使用 `ResumeKind::External { ext_tool:<call_id> }` 暂停线程并等待恢复负载
        catalog = await self.external_tool_catalog_get()
        if catalog is not None:
            hit = False
            for key in self.external_tool_catalog_keys(context):
                if await catalog.contains(key, action_name):
                    hit = True
                    break

            if hit:
                call_id = context.current_call_id
                if not call_id:
                    call_id = f"call_ext_{uuid.uuid4().hex[:8]}"
                raise self.gate_paused(
                    "external_tool",
                    action_name,
                    call_id,
                    parameters,
                    ResumeKind.External(callback_id=f"ext_tool:{call_id}"),
                    None,
                    lease,
                )

        # 遵循引擎的一次性批准标志。由内联门控等待重试路径在用户解决门控后设置；
        # 镜像传递 `approval_already_granted=true` 以跳过每次调用批准检查的旧版路径
        approval_already_granted = context.call_approval_granted
        return await self.execute_action_internal(
            action_name, parameters, lease, context, approval_already_granted,
        )

    async def available_actions(
            self, leases: List[Any], context: Any
    ) -> List[Any]:
        """列出可用动作"""
        inventory = await self.available_action_inventory(leases, context)
        return inventory.inline

    async def available_action_inventory(
            self, leases: List[Any], context: Any
    ) -> Any:
        """列出完整动作清单"""
        auth_manager = self.auth_manager
        capability_registry = self.capability_registry
        extensions = await self.fetch_extension_map(auth_manager, context)

        inventory = await ActionProjector.project_inventory(
            self.tools, auth_manager, capability_registry, leases, context, extensions,
        )

        # 合并每线程外部工具
        catalog = await self.external_tool_catalog_get()
        if catalog is not None:
            external = []
            for key in self.external_tool_catalog_keys(context):
                entries = await catalog.list(key)
                if entries:
                    external = entries
                    break

            if external:
                existing_names = {a.name for a in inventory.inline}
                for a in external:
                    if a.name not in existing_names:
                        inventory.inline.append(a)

        return inventory

    async def available_capabilities(
            self, leases: List[Any], context: Any
    ) -> List[Any]:
        """列出能力后台摘要"""
        auth_manager = self.auth_manager
        extensions = await self.fetch_extension_list(auth_manager, context)
        return await CapabilityProjector.project(auth_manager, leases, context, extensions)


# ── 辅助函数 ─────────────────────────────────────────────────

def _external_tool_catalog_keys(context: ThreadExecutionContext) -> List[str]:
    """获取外部工具目录的查找键"""
    keys = [str(context.thread_id)]
    if context.conversation_scope is not None and context.conversation_scope != context.thread_id:
        keys.append(str(context.conversation_scope))
    return keys


def _gate_paused(
        gate_name: str,
        action_name: str,
        call_id: str,
        parameters: dict,
        resume_kind: Any,
        resume_output: Optional[dict],
        paused_lease: Optional[CapabilityLease],
) -> EngineError:
    """创建门控暂停错误"""
    return EngineError(
        error_type="GatePaused",
        gate_name=gate_name,
        action_name=action_name,
        call_id=call_id or "",
        parameters=parameters,
        resume_kind=resume_kind,
        resume_output=resume_output,
        paused_lease=paused_lease,
    )


def _synthetic_action_call_id(action_name: str) -> str:
    """生成合成的动作调用 ID"""
    return f"call_{action_name}_{uuid.uuid4().hex[:8]}"


def _is_v1_only_tool(tool_name: str) -> bool:
    """检查是否为 v1 专用工具"""
    V1_ONLY_TOOLS = {"routine_create", "routine_update", "routine_delete",
                     "routine_fire", "event_emit", "create_job", "job_prompt"}
    return tool_name in V1_ONLY_TOOLS


def _is_v1_auth_tool(tool_name: str) -> bool:
    """检查是否为 v1 认证工具"""
    V1_AUTH_TOOLS = {"tool_auth", "tool_remove", "tool_upgrade", "secret_list", "secret_delete"}
    return tool_name in V1_AUTH_TOOLS


async def _resolve_mission_id(
        mgr: MissionManager,
        project_id: ProjectId,
        user_id: str,
        params: dict,
) -> MissionId:
    """解析任务 ID（按 UUID 或名称）"""
    # 尝试按 UUID 解析
    id_str = params.get("id") or params.get("mission_id")
    if id_str:
        try:
            return MissionId(uuid.UUID(id_str))
        except (ValueError, AttributeError):
            pass

    # 尝试按名称解析
    name = params.get("name")
    if name:
        missions = await mgr.list_missions(project_id, user_id)
        for m in missions:
            if m.name == name:
                return m.id
        raise EngineError(f"Effect: 未找到名为 '{name}' 的任务")

    raise EngineError("Effect: 需要任务 ID 或名称")


def _parse_cadence(cadence_str: str, timezone: Optional[ValidTimezone] = None) -> Any:
    """解析节奏字符串"""
    if cadence_str == "manual":
        return Manual()
    elif cadence_str.startswith("cron:"):
        expression = cadence_str[5:]
        return Cron(expression=expression, timezone=timezone)
    elif cadence_str.startswith("event:"):
        parts = cadence_str[6:].split(":", 1)
        if len(parts) == 2:
            return OnEvent(event_pattern=parts[1], channel=parts[0])
    elif cadence_str.startswith("webhook:"):
        return Webhook(path=cadence_str[8:])

    # 默认按 cron 表达式处理
    return Cron(expression=cadence_str, timezone=timezone)
