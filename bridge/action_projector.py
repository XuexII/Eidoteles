from engine import (
    ActionDef, ActionDiscoveryMetadata, ActionDiscoverySummary, ActionInventory, CapabilityLease,
    CapabilityRegistry, CapabilityStatus, EngineError, ModelToolSurface, ThreadExecutionContext
)

from auth.extension import AuthManager
from bridge.capability_projector import (
    capability_status_for_extension, capability_surface_subject_for_extension
)

from bridge.tool_permissions import ToolPermissionSnapshot
from bridge.tool_surface import (
    InvocationMode, SurfacePolicyInput, SurfaceSubjectKind, assign_surface
)
from extensions.naming import extension_name_candidates
from extensions import InstalledExtension, LatentProviderAction
from tools import ToolRegistry
from tools.permissions import PermissionState

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Set, Any
import logging

logger = logging.getLogger(__name__)


# ── 动作清单 ─────────────────────────────────────────────────

@dataclass
class ActionInventory:
    """动作清单"""
    inline: List[Any] = field(default_factory=list)  # List[ActionDef]
    discoverable: List[Any] = field(default_factory=list)  # List[ActionDef]


# ── 库存输入 ─────────────────────────────────────────────────

@dataclass
class InventoryInputs:
    """构建动作清单所需的输入"""
    tool_defs: List[Any] = field(default_factory=list)  # List[Tool]
    extension_statuses: Optional[Dict[str, Any]] = None  # Dict[str, InstalledExtension]
    latent_actions: List[Any] = field(default_factory=list)  # List[LatentProviderAction]
    tool_permissions: Optional[Any] = None  # ToolPermissionSnapshot


# ── 投射的动作 ───────────────────────────────────────────────

class ProjectedActionType:
    """投射动作的类型"""
    Inline = "Inline"
    Discoverable = "Discoverable"
    Hidden = "Hidden"


@dataclass
class ProjectedAction:
    """投射动作的结果"""
    action_type: str
    action: Optional[Any] = None  # ActionDef


# ── 动作投射器 ───────────────────────────────────────────────

class ActionProjector:
    """从工具注册表和能力注册表投射可用动作集合"""

    @staticmethod
    async def project_inventory(
            tools: Any,  # ToolRegistry
            auth_manager: Optional[Any] = None,  # AuthManager
            capability_registry: Optional[Any] = None,  # CapabilityRegistry
            leases: List[Any] = None,  # List[CapabilityLease]
            context: Any = None,  # ThreadExecutionContext
            prefetched_extensions: Optional[Dict[str, Any]] = None,  # Dict[str, InstalledExtension]
    ) -> ActionInventory:
        """投射可用动作集合

        当 `prefetched_extensions` 为 `Some` 时，投射器使用该映射而不是从
        `auth_manager` 获取。这允许调用者（通常是 `EffectBridgeAdapter`）
        在 `ActionProjector` 和 `CapabilityProjector` 之间共享单次获取
        """
        inputs = await _load_inventory_inputs(tools, auth_manager, context, prefetched_extensions)
        return _classify_projected_actions(inputs, capability_registry, leases or [])


async def _load_inventory_inputs(
        tools: Any,
        auth_manager: Optional[Any],
        context: Any,
        prefetched_extensions: Optional[Dict[str, Any]],
) -> InventoryInputs:
    """加载库存输入"""
    tool_defs = await tools.all()

    extension_statuses = None
    if prefetched_extensions is not None:
        extension_statuses = dict(prefetched_extensions)
    elif auth_manager is not None:
        try:
            extensions = await auth_manager.list_capability_extensions(context.user_id)
            extension_statuses = {ext.name: ext for ext in extensions}
        except Exception as error:
            logger.debug(
                f"加载 available_actions 的扩展库存失败；"
                f"省略扩展支持的动作: user_id={context.user_id}, error={error}"
            )
            extension_statuses = {}

    latent_actions = []
    if auth_manager is not None:
        latent_actions = await auth_manager.latent_provider_actions(context.user_id)

    tool_permissions = await ToolPermissionSnapshot.load(tools, context.user_id)

    return InventoryInputs(
        tool_defs=tool_defs,
        extension_statuses=extension_statuses,
        latent_actions=latent_actions,
        tool_permissions=tool_permissions,
    )


def _classify_projected_actions(
        inputs: InventoryInputs,
        capability_registry: Optional[Any],
        leases: List[Any],
) -> ActionInventory:
    """分类投射的动作"""
    inline = []
    discoverable = []

    for tool in inputs.tool_defs:
        result = _classify_registered_tool(
            tool, inputs.extension_statuses, inputs.tool_permissions,
        )
        if result.action_type == ProjectedActionType.Inline:
            inline.append(result.action)
        elif result.action_type == ProjectedActionType.Discoverable:
            discoverable.append(result.action)
        # Hidden 类型被忽略

    seen_inline = {action.name for action in inline}

    # 从能力注册表添加引擎原生动作
    if capability_registry is not None:
        for lease in leases:
            if lease.capability_name == "tools":
                continue
            cap = capability_registry.get(lease.capability_name)
            if cap is None:
                continue
            for action in cap.actions:
                if not lease.granted_actions.covers(action.name):
                    continue
                if _is_v1_only_tool(action.name) or _is_v1_auth_tool(action.name):
                    continue

                assignment = _assign_surface(SurfacePolicyInput(
                    kind=SurfaceSubjectKind.EngineNativeDirectAction,
                    status=CapabilityStatus.Ready,
                    invocation_mode=InvocationMode.Direct,
                    leased_and_callable=True,
                ))
                if not assignment.available_actions or action.name in seen_inline:
                    continue
                seen_inline.add(action.name)
                inline.append(action.clone() if hasattr(action, 'clone') else action)

    # 处理潜在动作
    seen_discoverable = set(seen_inline)
    for latent in inputs.latent_actions:
        if inputs.tool_permissions is not None:
            perm = inputs.tool_permissions.resolve_permission(latent.action_name)
            if perm.effective == PermissionState.Disabled:
                continue

        action = _project_latent_action(latent)
        if action.name not in seen_discoverable:
            seen_discoverable.add(action.name)
            discoverable.append(action)

    # 从 discoverable 中移除已在内联中的动作
    discoverable = [a for a in discoverable if a.name not in seen_inline]

    inline.sort(key=lambda a: a.name)
    discoverable.sort(key=lambda a: a.name)

    return ActionInventory(inline=inline, discoverable=discoverable)


def _classify_registered_tool(
        tool: Any,
        extension_statuses: Optional[Dict[str, Any]],
        tool_permissions: Any,
) -> ProjectedAction:
    """分类已注册的工具"""
    if _is_v1_only_tool(tool.name()):
        return ProjectedAction(action_type=ProjectedActionType.Hidden)
    if _is_v1_auth_tool(tool.name()):
        return ProjectedAction(action_type=ProjectedActionType.Hidden)

    if tool_permissions is not None:
        perm = tool_permissions.resolve_permission(tool.name())
        if perm.effective == PermissionState.Disabled:
            return ProjectedAction(action_type=ProjectedActionType.Hidden)

    provider_extension = tool.provider_extension() if hasattr(tool, 'provider_extension') else None
    if provider_extension is not None:
        if extension_statuses is None:
            return ProjectedAction(action_type=ProjectedActionType.Hidden)

        extension = _provider_extension_status(extension_statuses, provider_extension)
        if extension is None:
            return ProjectedAction(action_type=ProjectedActionType.Hidden)

        status = _capability_status_for_extension(extension, False)
        kind, invocation_mode = _capability_surface_subject_for_extension(extension)
        assignment = _assign_surface(SurfacePolicyInput(
            kind=kind,
            status=status,
            invocation_mode=invocation_mode,
            leased_and_callable=False,
        ))
        action = _project_tool_action(tool)
        if assignment.available_actions:
            return ProjectedAction(action_type=ProjectedActionType.Inline, action=action)
        elif _supports_pre_activation_discovery(kind, invocation_mode, status):
            return ProjectedAction(action_type=ProjectedActionType.Discoverable, action=action)
        else:
            return ProjectedAction(action_type=ProjectedActionType.Hidden)
    else:
        return ProjectedAction(action_type=ProjectedActionType.Inline, action=_project_tool_action(tool))


def _supports_pre_activation_discovery(
        kind: Any, invocation_mode: Any, status: Any
) -> bool:
    """检查是否支持预激活发现"""
    valid_kinds = {SurfaceSubjectKind.ExtensionDirectAction, SurfaceSubjectKind.AvailableNotInstalledProviderEntry}
    valid_statuses = {
        CapabilityStatus.NeedsAuth, CapabilityStatus.NeedsSetup,
        CapabilityStatus.Inactive, CapabilityStatus.AvailableNotInstalled,
    }
    return kind in valid_kinds and invocation_mode == InvocationMode.Direct and status in valid_statuses


def _project_tool_action(tool: Any) -> Any:
    """投射工具动作为 ActionDef"""
    callable_name = tool.name().replace('-', '_')
    callable_schema = tool.parameters_schema() if hasattr(tool, 'parameters_schema') else {}
    discovery_schema = tool.discovery_schema() if hasattr(tool, 'discovery_schema') else callable_schema
    model_tool_surface = _default_model_tool_surface(callable_name)
    description = tool.description() if hasattr(tool, 'description') else ""

    return ActionDef(
        name=callable_name,
        description=description,
        parameters_schema=callable_schema,
        effects=[],
        requires_approval=False,
        model_tool_surface=model_tool_surface,
        discovery=None,
    )


def _project_latent_action(action: Any) -> Any:
    """投射潜在动作为 ActionDef"""
    callable_name = action.action_name.replace('-', '_')
    return ActionDef(
        name=callable_name,
        description=getattr(action, 'description', ''),
        parameters_schema=getattr(action, 'parameters_schema', {}),
        effects=[],
        requires_approval=False,
        model_tool_surface=_default_model_tool_surface(callable_name),
        discovery=None,
    )


def _default_model_tool_surface(action_name: str) -> Any:
    """获取默认的模型工具表面"""
    if (action_name in ("echo", "http", "json", "time")
            or action_name.startswith("memory_")
            or action_name.startswith("skill_")
            or action_name.startswith("tool_")):
        return ModelToolSurface.FullSchema
    else:
        return ModelToolSurface.CompactToolInfo


def _provider_extension_status(
        extension_statuses: Dict[str, Any],
        provider_extension: str,
) -> Optional[Any]:
    """获取提供者扩展状态"""
    candidates = _extension_name_candidates(provider_extension)
    best = None
    best_rank = -1
    for candidate in candidates:
        ext = extension_statuses.get(candidate)
        if ext is not None:
            rank = _provider_extension_rank(ext)
            if rank > best_rank:
                best_rank = rank
                best = ext
    return best


def _provider_extension_rank(extension: Any) -> int:
    """获取提供者扩展的排名"""
    status = _capability_status_for_extension(extension, False)
    rank_map = {
        CapabilityStatus.Ready: 5,
        CapabilityStatus.Inactive: 4,
        CapabilityStatus.NeedsAuth: 3,
        CapabilityStatus.NeedsSetup: 2,
        CapabilityStatus.Error: 1,
        CapabilityStatus.AvailableNotInstalled: 0,
    }
    return rank_map.get(status, 0)


# ── 占位符函数 ───────────────────────────────────────────────

def _is_v1_only_tool(name: str) -> bool:
    """检查是否为 v1 专用工具"""
    V1_ONLY = {"routine_create", "routine_update", "routine_delete", "routine_fire",
               "event_emit", "create_job", "job_prompt"}
    return name in V1_ONLY


def _is_v1_auth_tool(name: str) -> bool:
    """检查是否为 v1 认证工具"""
    V1_AUTH = {"tool_auth", "tool_remove", "tool_upgrade", "secret_list", "secret_delete"}
    return name in V1_AUTH


def _extension_name_candidates(name: str) -> List[str]:
    """获取扩展名称候选"""
    return [name]


def _capability_status_for_extension(extension: Any, _flag: bool) -> Any:
    """获取扩展的能力状态"""
    return getattr(extension, 'status', CapabilityStatus.Ready)


def _capability_surface_subject_for_extension(extension: Any) -> tuple:
    """获取扩展的能力表面主题"""
    return (SurfaceSubjectKind.ExtensionDirectAction, InvocationMode.Direct)


def _assign_surface(input_data: Any) -> Any:
    """分配表面策略"""

    class Assignment:
        available_actions = True

    return Assignment()
