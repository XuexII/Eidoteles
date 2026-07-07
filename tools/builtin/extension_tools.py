"""
智能体可调用的扩展管理工具（MCP 服务器和 WASM 工具）。

这些内置工具从对话中管理扩展发现和生命周期。
在引擎 v2 中，已安装但未认证的工具可直接调用：引擎的
认证预检在执行时引发 `Authentication` 门控，内联等待
机制暂停 VM，OAuth 回调传递已解析的凭证以重试操作。
`tool_search`、`tool_list` 和 `tool_info` 支持发现；
`tool_install` / `tool_auth` 涵盖更窄的安装 + 手动认证流程。
"""

import time
import json
from typing import Optional, List, Any
from enum import Enum


class ApprovalRequirement(Enum):
    """工具批准要求"""
    Never = "Never"
    Always = "Always"
    UnlessAutoApproved = "UnlessAutoApproved"


class EngineCompatibility(Enum):
    """引擎兼容性"""
    V1Only = "V1Only"
    V2 = "V2"
    Both = "Both"


def output_from_ensure_ready(outcome: "EnsureReadyOutcome") -> dict:
    """从 EnsureReadyOutcome 构建 JSON 输出

    Args:
        outcome: 确保就绪操作的结果

    Returns:
        表示结果的 JSON 字典
    """
    if outcome.type == "Ready" and outcome.activation is not None:
        return {
            "status": "ready",
            "name": outcome.name,
            "kind": outcome.kind,
            "tools_loaded": outcome.activation.tools_loaded,
            "message": outcome.activation.message,
        }
    elif outcome.type == "Ready":
        return {
            "status": "ready",
            "name": outcome.name,
            "kind": outcome.kind,
            "phase": outcome.phase,
            "message": f"扩展 '{outcome.name}' 已就绪。",
        }
    elif outcome.type == "NeedsAuth":
        value = outcome.auth.to_dict() if hasattr(outcome.auth, 'to_dict') else {"error": "序列化失败"}
        if outcome.credential_name is not None and isinstance(value, dict):
            value["credential_name"] = outcome.credential_name
        return value
    elif outcome.type == "NeedsSetup":
        return {
            "status": "needs_setup",
            "name": outcome.name,
            "kind": outcome.kind,
            "instructions": outcome.instructions,
            "setup_url": outcome.setup_url,
        }
    return {}


# ── tool_search ──────────────────────────────────────────────────────────

class ToolSearchTool:
    """搜索可用扩展以添加新能力。

    扩展包括频道（Telegram、Slack、Discord — 连接消息平台以便 IronClaw 可以在此接收和回复）、
    工具和 MCP 服务器。使用 `tool_install` 安装发现的集成；安装后，其工具变为可直接调用
    （当凭证缺失时，引擎的认证预检查在执行时引发认证门控）。
    使用 `message` 工具进行主动出站发送。
    当内置注册表没有结果时，使用 discover:true 在线搜索
    """

    def __init__(self, manager: "ExtensionManager"):
        self.manager: ExtensionManager = manager

    def name(self) -> str:
        return "tool_search"

    def description(self) -> str:
        return (
            "搜索可用扩展以添加新能力。扩展包括频道（Telegram、Slack、Discord — 连接消息平台以便 "
            "IronClaw 可以在此接收和回复）、工具和 MCP 服务器。使用 `tool_install` 安装发现的集成；"
            "安装后，其工具变为可直接调用（当凭证缺失时，引擎的认证预检查在执行时引发认证门控）。"
            "使用 `message` 工具进行主动出站发送。当内置注册表没有结果时，使用 discover:true 在线搜索。"
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询（名称、关键词或描述片段）",
                },
                "discover": {
                    "type": "boolean",
                    "description": "如果为 true，同时在线搜索（较慢，5-15 秒）。请先尝试不使用此选项。",
                    "default": False,
                },
            },
            "required": ["query"],
        }

    async def execute(
            self,
            params: dict,
            ctx: "JobContext",
    ) -> "ToolOutput":
        """执行工具搜索"""
        start = time.monotonic()

        query = params.get("query", "")
        discover = params.get("discover", False)

        try:
            results = await self.manager.search(query, discover)
        except Exception as e:
            raise ToolError.ExecutionFailed(str(e))

        output = {
            "results": results,
            "count": len(results),
            "searched_online": discover,
        }

        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.success(output, duration_ms)


# ── tool_install ─────────────────────────────────────────────────────────

class ToolInstallTool:
    """安装扩展（频道、工具或 MCP 服务器）。

    使用 tool_search 结果中的名称，或提供显式 URL。
    同时发现工作目录中的工具源代码
    （tools-src/、tool-src/ 或包含 Cargo.toml 的直接子目录）
    """

    def __init__(self, manager: "ExtensionManager"):
        self.manager: ExtensionManager = manager

    def name(self) -> str:
        return "tool_install"

    def description(self) -> str:
        return (
            "安装扩展（频道、工具或 MCP 服务器）。"
            "使用 tool_search 结果中的名称，或提供显式 URL。"
            "同时发现工作目录中的工具源代码"
            "（tools-src/、tool-src/ 或包含 Cargo.toml 的直接子目录）。"
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "扩展名称（来自搜索结果或自定义）",
                },
                "url": {
                    "type": "string",
                    "description": "显式 URL（用于不在注册表中的扩展）",
                },
                "kind": {
                    "type": "string",
                    "enum": ["mcp_server", "wasm_tool", "wasm_channel"],
                    "description": "扩展类型（如果省略则自动检测）",
                },
            },
            "required": ["name"],
        }

    async def execute(
            self,
            params: dict,
            ctx: "JobContext",
    ) -> "ToolOutput":
        """执行扩展安装"""
        start = time.monotonic()

        name = require_str(params, "name")
        url = params.get("url")
        kind_hint_str = params.get("kind")

        kind_hint = None
        if kind_hint_str == "mcp_server":
            kind_hint = ExtensionKind.McpServer
        elif kind_hint_str == "wasm_tool":
            kind_hint = ExtensionKind.WasmTool
        elif kind_hint_str == "wasm_channel":
            kind_hint = ExtensionKind.WasmChannel

        try:
            await self.manager.install(name, url, kind_hint, ctx.user_id)
        except Exception as e:
            raise ToolError.ExecutionFailed(str(e))

        try:
            result = await self.manager.ensure_extension_ready(
                name, ctx.user_id, EnsureReadyIntent.PostInstall,
            )
        except Exception as e:
            raise ToolError.ExecutionFailed(str(e))

        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.success(output_from_ensure_ready(result), duration_ms)

    def requires_approval(self, params: dict) -> ApprovalRequirement:
        return ApprovalRequirement.UnlessAutoApproved


# ── tool_auth ────────────────────────────────────────────────────────────

class ToolAuthTool:
    """为扩展发起认证。对于 OAuth，返回 URL。对于手动认证，返回指令。
    用户通过安全频道提供其令牌，切勿通过此工具
    """

    def __init__(self, manager: "ExtensionManager"):
        self.manager: ExtensionManager = manager

    def name(self) -> str:
        return "tool_auth"

    def description(self) -> str:
        return (
            "为扩展发起认证。对于 OAuth，返回 URL。"
            "对于手动认证，返回指令。用户通过安全频道提供其令牌，切勿通过此工具。"
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要认证的扩展名称",
                },
            },
            "required": ["name"],
        }

    async def execute(
            self,
            params: dict,
            ctx: "JobContext",
    ) -> "ToolOutput":
        """执行扩展认证"""
        start = time.monotonic()

        name = require_str(params, "name")

        try:
            result = await self.manager.ensure_extension_ready(
                name, ctx.user_id, EnsureReadyIntent.ExplicitAuth,
            )
        except Exception as e:
            raise ToolError.ExecutionFailed(str(e))

        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.success(output_from_ensure_ready(result), duration_ms)

    def requires_approval(self, params: dict) -> ApprovalRequirement:
        # 在网关模式下，tool_auth 仅返回认证 URL 供前端打开 —
        # 不会在服务器端启动浏览器，因此不需要批准
        if self.manager.should_use_gateway_mode():
            return ApprovalRequirement.Never
        else:
            return ApprovalRequirement.UnlessAutoApproved

    def engine_compatibility(self) -> EngineCompatibility:
        return EngineCompatibility.V1Only


# ── tool_list ────────────────────────────────────────────────────────────

class ToolListTool:
    """列出扩展和内置工具及其认证、激活和权限状态。

    设置 include_available:true 以同时显示尚未安装的注册表条目。
    使用 kind="builtin" 仅列出内置 Rust 工具
    """

    def __init__(self, manager: "ExtensionManager"):
        self.manager: ExtensionManager = manager
        self.registry: Optional["ToolRegistry"] = None
        self.settings_store: Optional["SettingsStore"] = None

    def with_registry(self, registry: "ToolRegistry") -> "ToolListTool":
        """附加工具注册表，以便 `kind="builtin"` 列表可用"""
        self.registry = registry
        return self

    def with_settings_store(self, store: "SettingsStore") -> "ToolListTool":
        """附加设置存储，以便可以按用户读取权限状态"""
        self.settings_store = store
        return self

    def name(self) -> str:
        return "tool_list"

    def description(self) -> str:
        return (
            "列出扩展和内置工具及其认证、激活和权限状态。"
            "设置 include_available:true 以同时显示尚未安装的注册表条目。"
            "使用 kind=\"builtin\" 仅列出内置 Rust 工具。"
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["mcp_server", "wasm_tool", "wasm_channel", "builtin"],
                    "description": "按扩展类型过滤（省略则列出所有，包括内置工具）",
                },
                "include_available": {
                    "type": "boolean",
                    "description": "如果为 true，同时包含尚未安装的注册表条目",
                    "default": False,
                },
            },
        }

    async def execute(
            self,
            params: dict,
            ctx: "JobContext",
    ) -> "ToolOutput":
        """执行工具列表查询"""
        start = time.monotonic()

        kind_str = params.get("kind")
        want_builtin = kind_str is None or kind_str == "builtin"
        want_extensions = kind_str is None or kind_str != "builtin"
        include_available = params.get("include_available", False)

        # 加载每用户权限覆盖（尽力而为；任何失败时为空映射）
        perm_overrides = {}
        if self.settings_store is not None:
            try:
                settings_map = await self.settings_store.get_all_settings(ctx.user_id)
                settings = Settings.from_db_map(settings_map)
                perm_overrides = settings.tool_permissions
            except Exception as e:
                logger.warning(f"加载工具权限失败: {e}")

        output = {}

        # 内置工具部分
        if want_builtin and self.registry is not None:
            builtin_names = await self.registry.builtin_tool_names()
            tools = await self.registry.all()
            builtin_list = []
            for tool in tools:
                if tool.name() not in builtin_names:
                    continue
                name = tool.name()
                perm_state = effective_permission(name, perm_overrides)
                default_state = seeded_default_permission(name) or PermissionState.AskEachTime
                locked = tool_permission_locked(tool)
                builtin_list.append({
                    "name": name,
                    "description": tool.description(),
                    "permission_state": perm_state,
                    "default_state": default_state,
                    "locked": locked,
                    "locked_reason": TOOL_PERMISSION_LOCKED_REASON if locked else None,
                })

            output["builtins"] = builtin_list
            output["builtin_count"] = len(builtin_list)

        # 扩展部分
        if want_extensions:
            kind_filter = None
            if kind_str == "mcp_server":
                kind_filter = ExtensionKind.McpServer
            elif kind_str == "wasm_tool":
                kind_filter = ExtensionKind.WasmTool
            elif kind_str == "wasm_channel":
                kind_filter = ExtensionKind.WasmChannel

            try:
                extensions = await self.manager.list(kind_filter, include_available, ctx.user_id)
            except Exception as e:
                raise ToolError.ExecutionFailed(str(e))

            output["extensions"] = extensions
            output["count"] = len(extensions)

        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.success(output, duration_ms)


# ── tool_remove ──────────────────────────────────────────────────────────

class ToolRemoveTool:
    """从磁盘永久移除已安装的扩展（频道、工具或 MCP 服务器）。
    此操作无法撤消 — WASM 二进制文件和配置文件将被删除
    """

    def __init__(self, manager: "ExtensionManager"):
        self.manager: ExtensionManager = manager

    def name(self) -> str:
        return "tool_remove"

    def description(self) -> str:
        return (
            "从磁盘永久移除已安装的扩展（频道、工具或 MCP 服务器）。"
            "此操作无法撤消 — WASM 二进制文件和配置文件将被删除。"
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要移除的扩展名称",
                },
            },
            "required": ["name"],
        }

    async def execute(
            self,
            params: dict,
            ctx: "JobContext",
    ) -> "ToolOutput":
        """执行扩展移除"""
        start = time.monotonic()

        name = require_str(params, "name")

        try:
            message = await self.manager.remove(name, ctx.user_id)
        except Exception as e:
            raise ToolError.ExecutionFailed(str(e))

        output = {"name": name, "message": message}
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.success(output, duration_ms)

    def requires_approval(self, params: dict) -> ApprovalRequirement:
        return ApprovalRequirement.Always

    def engine_compatibility(self) -> EngineCompatibility:
        return EngineCompatibility.V1Only


# ── tool_upgrade ─────────────────────────────────────────────────────

class ToolUpgradeTool:
    """升级已安装的 WASM 扩展（频道和工具）以匹配当前主机 WIT 版本。
    如果省略 name，则检查并升级所有已安装的 WASM 扩展。认证和密钥被保留
    """

    def __init__(self, manager: "ExtensionManager"):
        self.manager: ExtensionManager = manager

    def name(self) -> str:
        return "tool_upgrade"

    def description(self) -> str:
        return (
            "升级已安装的 WASM 扩展（频道和工具）以匹配当前主机 WIT 版本。"
            "如果省略 name，则检查并升级所有已安装的 WASM 扩展。认证和密钥被保留。"
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要升级的扩展名称（省略则升级所有）",
                },
            },
        }

    async def execute(
            self,
            params: dict,
            ctx: "JobContext",
    ) -> "ToolOutput":
        """执行扩展升级"""
        start = time.monotonic()

        name = params.get("name")

        try:
            result = await self.manager.upgrade(name, ctx.user_id)
        except Exception as e:
            raise ToolError.ExecutionFailed(str(e))

        output = result.to_dict() if hasattr(result, 'to_dict') else {"error": "序列化失败"}
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.success(output, duration_ms)

    def requires_approval(self, params: dict) -> ApprovalRequirement:
        return ApprovalRequirement.UnlessAutoApproved


# ── extension_info ────────────────────────────────────────────────────

class ExtensionInfoTool:
    """显示已安装扩展的详细信息，包括版本和 WIT 版本兼容性"""

    def __init__(self, manager: "ExtensionManager"):
        self.manager: ExtensionManager = manager

    def name(self) -> str:
        return "extension_info"

    def description(self) -> str:
        return "显示已安装扩展的详细信息，包括版本和 WIT 版本兼容性。"

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要获取信息的扩展名称",
                },
            },
            "required": ["name"],
        }

    async def execute(
            self,
            params: dict,
            ctx: "JobContext",
    ) -> "ToolOutput":
        """执行扩展信息查询"""
        start = time.monotonic()

        name = require_str(params, "name")

        try:
            info = await self.manager.extension_info(name, ctx.user_id)
        except Exception as e:
            raise ToolError.ExecutionFailed(str(e))

        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.success(info, duration_ms)


# ── tool_permission_set ───────────────────────────────────────────────────

class ToolPermissionSetTool:
    """获取或设置工具的权限状态。

    用于查看当前权限或提议更改（需要用户批准）。
    状态：always_allow（无提示）、ask_each_time（需要批准）、disabled（工具对 LLM 隐藏）
    """

    def __init__(
            self,
            registry: "ToolRegistry",
            settings_store: Optional["SettingsStore"] = None,
    ):
        self.registry: ToolRegistry = registry
        self.settings_store: Optional[SettingsStore] = settings_store

    def name(self) -> str:
        return "tool_permission_set"

    def description(self) -> str:
        return (
            "获取或设置工具的权限状态。用于查看当前权限或提议更改（需要用户批准）。"
            "状态：always_allow（无提示）、ask_each_time（需要批准）、disabled（工具对 LLM 隐藏）。"
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "要配置的工具名称",
                },
                "state": {
                    "type": "string",
                    "enum": ["always_allow", "ask_each_time", "disabled"],
                    "description": "新权限状态。省略则仅读取当前状态。",
                },
            },
            "required": ["tool_name"],
        }

    def requires_approval(self, params: dict) -> ApprovalRequirement:
        return ApprovalRequirement.Always

    async def execute(
            self,
            params: dict,
            ctx: "JobContext",
    ) -> "ToolOutput":
        """执行权限设置"""
        start = time.monotonic()

        tool_name = require_str(params, "tool_name")

        # 验证目标工具在注册表中存在
        target_tool = await self.registry.get(tool_name)
        if target_tool is None:
            raise ToolError.InvalidParameters(f"未知工具: '{tool_name}'")
        locked = tool_permission_locked(target_tool)

        # 加载用户当前设置
        if self.settings_store is not None:
            try:
                settings_map = await self.settings_store.get_all_settings(ctx.user_id)
                settings = Settings.from_db_map(settings_map)
            except Exception as e:
                raise ToolError.ExecutionFailed(f"加载设置失败: {e}")
        else:
            settings = Settings()

        prev_state = effective_permission(tool_name, settings.tool_permissions)

        # 只读模式
        state_str = params.get("state")
        if state_str is None:
            default_state = seeded_default_permission(tool_name) or PermissionState.AskEachTime
            output = {
                "tool_name": tool_name,
                "current_state": prev_state,
                "default_state": default_state,
                "locked": locked,
                "locked_reason": TOOL_PERMISSION_LOCKED_REASON if locked else None,
            }
            duration_ms = int((time.monotonic() - start) * 1000)
            return ToolOutput.success(output, duration_ms)

        if not isinstance(state_str, str):
            raise ToolError.InvalidParameters(
                "'state' 必须是字符串: always_allow、ask_each_time 或 disabled"
            )

        # 解析请求的新状态
        state_map = {
            "always_allow": PermissionState.AlwaysAllow,
            "ask_each_time": PermissionState.AskEachTime,
            "disabled": PermissionState.Disabled,
        }
        new_state = state_map.get(state_str)
        if new_state is None:
            raise ToolError.InvalidParameters(
                f"无效状态 '{state_str}'；预期 always_allow、ask_each_time 或 disabled"
            )

        if locked and new_state == PermissionState.AlwaysAllow:
            raise ToolError.InvalidParameters(
                f"'{tool_name}' 始终需要批准，无法设置为 always_allow"
            )

        # 持久化更新的权限
        if self.settings_store is not None:
            try:
                await self.settings_store.set_setting(
                    ctx.user_id,
                    f"tool_permissions.{tool_name}",
                    new_state.value,
                )
            except Exception as e:
                raise ToolError.ExecutionFailed(f"保存权限失败: {e}")
        else:
            raise ToolError.ExecutionFailed(
                "未配置设置存储 — 权限更改无法持久化"
            )

        output = {
            "tool_name": tool_name,
            "prev_state": prev_state,
            "new_state": new_state.value,
        }
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.success(output, duration_ms)

    def engine_compatibility(self) -> EngineCompatibility:
        return EngineCompatibility.V1Only