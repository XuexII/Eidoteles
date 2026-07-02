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
    auto_approved: Set[str]  = field(default_factory=set, init=False)
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

    # _lock = asyncio.Lock()


    def with_global_auto_approve(self, enabled: bool) -> "EffectBridgeAdapter":
        """镜像 v1 调度器行为，用于全局自动批准的工具"""
        self.auto_approve_tools = enabled
        return self

    async def auto_approve_tool(self, tool_name: str) -> None:
        """标记工具为自动批准（用户说"始终"）"""
        async with self._lock:
            self.auto_approved.add(tool_name)

    async def revoke_auto_approve(self, tool_name: str) -> None:
        """撤销工具的自动批准（恢复失败时回滚）"""
        async with self._lock:
            self.auto_approved.discard(tool_name)

    def reset_call_count(self) -> None:
        """重置每步调用计数器（在线程/步骤之间调用）"""
        self.call_count = 0

    async def set_external_tool_catalog(self, catalog: ExternalToolCatalog) -> None:
        """设置每线程外部工具目录"""
        async with self._lock:
            self.external_tool_catalog = catalog

    async def set_workspace_mounts(self, mounts: Optional[WorkspaceMounts]) -> None:
        """设置每项目工作区挂载表"""
        async with self._lock:
            self.workspace_mounts = mounts

    async def set_capability_registry(self, registry: CapabilityRegistry) -> None:
        """安装引擎能力注册表"""
        async with self._lock:
            self.capability_registry = registry

    async def set_http_interceptor(self, interceptor: HttpInterceptor) -> None:
        """安装追踪 HTTP 拦截器"""
        async with self._lock:
            self.http_interceptor = interceptor

    async def set_engine_store(self, store: Store) -> None:
        """提供实时引擎存储"""
        async with self._lock:
            self.engine_store = store

    async def set_skill_registry(self, registry: SkillRegistry) -> None:
        """提供 v1 技能注册表"""
        async with self._lock:
            self.skill_registry = registry

    async def set_auth_manager(self, mgr: AuthManager) -> None:
        """设置认证管理器"""
        async with self._lock:
            self.auth_manager = mgr

    async def set_mission_manager(self, mgr: MissionManager) -> None:
        """设置任务管理器（在引擎初始化后调用）"""
        async with self._lock:
            self.mission_manager = mgr

    def tools_ref(self) -> ToolRegistry:
        """访问底层工具注册表"""
        return self.tools

    def safety_ref(self) -> SafetyLayer:
        """访问底层安全层"""
        return self.safety

    # ── EffectExecutor 接口实现 ──────────────────────────────

    async def execute_action(
            self,
            action_name: str,
            parameters: dict,
            lease: CapabilityLease,
            context: ThreadExecutionContext,
    ) -> ActionResult:
        """执行能力动作"""
        # 外部工具短路。如果每线程目录声明了此动作名称，
        # 调用者将执行它；我们使用 `ResumeKind::External` 暂停线程并等待恢复负载

        # 参数跳过调度时验证（调用者的工具模式未在主机注册），
        # 但恢复负载在到达 LLM 之前通过 `bridge::router` 中的
        # `SafetyLayer::sanitize_tool_output` 运行

        if self.external_tool_catalog is not None:
            catalog = self.external_tool_catalog
            hit = False
            for key in _external_tool_catalog_keys(context):
                if await catalog.contains(key, action_name):
                    hit = True
                    break

            if hit:
                call_id = context.current_call_id or f"call_ext_{uuid.uuid4().hex[:8]}"
                raise _gate_paused(
                    "external_tool",
                    action_name,
                    call_id,
                    parameters,
                    ResumeKind.External(callback_id=f"ext_tool:{call_id}"),
                    None,
                    lease,
                )

        # 遵循引擎的一次性批准标志
        approval_already_granted = context.call_approval_granted
        return await self._execute_action_internal(
            action_name, parameters, lease, context, approval_already_granted,
        )

    async def available_actions(
            self,
            leases: List[CapabilityLease],
            context: ThreadExecutionContext,
    ) -> List[ActionDef]:
        """列出可用动作"""
        inventory = await self.available_action_inventory(leases, context)
        return inventory.inline

    async def available_action_inventory(
            self,
            leases: List[CapabilityLease],
            context: ThreadExecutionContext,
    ) -> ActionInventory:
        """列出完整动作清单"""
        auth_manager = self.auth_manager
        # 所有已知能力的注册表
        capability_registry = self.capability_registry
        # 获取扩展映射
        extensions = await self.fetch_extension_map(auth_manager, context)

        inventory = await ActionProjector.project_inventory(
            self.tools,
            auth_manager,
            capability_registry,
            leases,
            context,
            extensions,
        )

        # 合并每线程外部工具
        if self.external_tool_catalog is not None:
            external = []
            for key in _external_tool_catalog_keys(context):
                entries = await self.external_tool_catalog.list(key)
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
            self,
            leases: List[CapabilityLease],
            context: ThreadExecutionContext,
    ) -> List[CapabilitySummary]:
        """列出能力后台摘要"""
        auth_manager = self.auth_manager
        extensions = await self._fetch_extension_list(auth_manager, context)
        return await CapabilityProjector.project(auth_manager, leases, context, extensions)

    # ── 内部方法 ────────────────────────────────────────────

    async def _fetch_extension_list(
            self,
            auth_manager: Optional[AuthManager],
            context: ThreadExecutionContext,
    ) -> Optional[List[InstalledExtension]]:
        """获取扩展列表"""
        if auth_manager is None:
            return None
        try:
            return await auth_manager.list_capability_extensions(context.user_id)
        except Exception as error:
            logger.debug(f"加载扩展清单失败: user_id={context.user_id}, error={error}")
            return []

    async def fetch_extension_map(
            self,
            auth_manager: Optional[AuthManager],
            context: ThreadExecutionContext,
    ) -> Optional[Dict[str, InstalledExtension]]:
        """获取扩展映射"""
        extensions = await self._fetch_extension_list(auth_manager, context)
        if extensions is None:
            return None
        return {ext.name: ext for ext in extensions}

    async def _execute_action_internal(
            self,
            action_name: str,
            parameters: dict,
            lease: CapabilityLease,
            context: ThreadExecutionContext,
            approval_already_granted: bool,
    ) -> ActionResult:
        """内部动作执行"""
        start = time.monotonic()
        canonical_action_name = action_name
        lookup_name = action_name

        # ── 每步调用限制（防止放大循环）──
        MAX_CALLS_PER_STEP = 50
        self.call_count += 1
        if self.call_count >= MAX_CALLS_PER_STEP:
            raise EngineError(
                f"Effect: 工具调用限制已达到（{MAX_CALLS_PER_STEP} 次/代码步骤）。"
                f"请将任务分解为多个步骤。"
            )

        # 处理任务调用
        result = await self._handle_mission_call(canonical_action_name, parameters, context)
        if result is not None:
            r = result
            r.duration_ms = int((time.monotonic() - start) * 1000)
            r.call_id = context.current_call_id or _synthetic_action_call_id(action_name)
            return r

        # 检查 v1 专用工具
        if _is_v1_only_tool(lookup_name):
            raise EngineError(
                f"Effect: 工具 '{action_name}' 在引擎 v2 中不可用。"
                f"告诉用户改用斜杠命令（例如 /routine、/job）。"
            )

        if _is_v1_auth_tool(lookup_name):
            raise EngineError(
                f"Effect: 工具 '{action_name}' 在引擎 v2 中不可用。"
                f"认证由内核自动处理。"
            )

        # 速率限制检查
        tool = await self.tools.get(lookup_name)
        if tool is not None and tool.rate_limit_config() is not None:
            rl_config = tool.rate_limit_config()
            result = await self.rate_limiter.check_and_record(
                context.user_id, lookup_name, rl_config,
            )
            if result.is_limited:
                raise EngineError(
                    f"Effect: 工具 '{action_name}' 已被速率限制。"
                    f"请在 {result.retry_after:.0f} 秒后重试。"
                )

        # 执行工具
        job_ctx = JobContext.with_user(
            context.user_id,
            "engine_v2",
            f"Thread {context.thread_id}",
        )
        if self.http_interceptor is not None:
            job_ctx.http_interceptor = self.http_interceptor

        try:
            output = await execute_tool_with_safety(
                self.tools,
                self.safety,
                lookup_name,
                parameters,
                job_ctx,
            )
        except Exception as e:
            error_msg = f"工具 '{lookup_name}' 失败: {e}"
            sanitized = self.safety.sanitize_tool_output(lookup_name, error_msg)
            return ActionResult(
                call_id=context.current_call_id or _synthetic_action_call_id(action_name),
                action_name=action_name,
                output={"error": sanitized.content},
                is_error=True,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        sanitized = self.safety.sanitize_tool_output(lookup_name, output)
        wrapped = self.safety.wrap_for_llm(lookup_name, sanitized.content)
        try:
            output_value = json.loads(output)
        except json.JSONDecodeError:
            output_value = wrapped

        return ActionResult(
            call_id=context.current_call_id or _synthetic_action_call_id(action_name),
            action_name=action_name,
            output=output_value,
            is_error=False,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    async def _handle_mission_call(
            self,
            action_name: str,
            params: dict,
            context: ThreadExecutionContext,
    ) -> Optional[ActionResult]:
        """处理 mission_* 和 routine_* 函数调用"""
        if not action_name.startswith("mission_"):
            return None

        mgr = self.mission_manager
        if mgr is None:
            return ActionResult(
                call_id=context.current_call_id or _synthetic_action_call_id(action_name),
                action_name=action_name,
                output={"error": "任务管理器不可用"},
                is_error=True,
                duration_ms=0,
            )

        try:
            if action_name == "mission_list":
                missions = await mgr.list_missions(context.project_id, context.user_id)
                result_list = [
                    {
                        "id": str(m.id),
                        "name": m.name,
                        "goal": m.goal,
                        "status": str(m.status),
                    }
                    for m in missions
                ]
                return ActionResult(
                    call_id=context.current_call_id or _synthetic_action_call_id(action_name),
                    action_name=action_name,
                    output=result_list,
                    is_error=False,
                    duration_ms=0,
                )
            elif action_name == "mission_create":
                name = params.get("name", "未命名任务")
                goal = params.get("goal", "")
                cadence_str = params.get("cadence", "manual")
                cadence = _parse_cadence(cadence_str, context.user_timezone)

                mid = await mgr.create_mission(
                    context.project_id,
                    context.user_id,
                    name,
                    goal,
                    cadence,
                    [],
                )
                return ActionResult(
                    call_id=context.current_call_id or _synthetic_action_call_id(action_name),
                    action_name=action_name,
                    output={"mission_id": str(mid), "name": name, "status": "created"},
                    is_error=False,
                    duration_ms=0,
                )
            elif action_name == "mission_fire":
                mid = await _resolve_mission_id(mgr, context.project_id, context.user_id, params)
                tid = await mgr.fire_mission(mid, context.user_id, None)
                return ActionResult(
                    call_id=context.current_call_id or _synthetic_action_call_id(action_name),
                    action_name=action_name,
                    output={"thread_id": str(tid), "status": "fired"} if tid else {"status": "not_fired"},
                    is_error=False,
                    duration_ms=0,
                )
            elif action_name == "mission_pause":
                mid = await _resolve_mission_id(mgr, context.project_id, context.user_id, params)
                await mgr.pause_mission(mid, context.user_id)
                return ActionResult(
                    call_id=context.current_call_id or _synthetic_action_call_id(action_name),
                    action_name=action_name,
                    output={"status": "ok"},
                    is_error=False,
                    duration_ms=0,
                )
            elif action_name == "mission_resume":
                mid = await _resolve_mission_id(mgr, context.project_id, context.user_id, params)
                await mgr.resume_mission(mid, context.user_id)
                return ActionResult(
                    call_id=context.current_call_id or _synthetic_action_call_id(action_name),
                    action_name=action_name,
                    output={"status": "ok"},
                    is_error=False,
                    duration_ms=0,
                )
            else:
                return None
        except Exception as e:
            return ActionResult(
                call_id=context.current_call_id or _synthetic_action_call_id(action_name),
                action_name=action_name,
                output={"error": str(e)},
                is_error=True,
                duration_ms=0,
            )


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
