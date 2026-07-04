from __future__ import annotations
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict
import logging
from context import ContextManager
from db import (Database, UserStore)
from extensions import ExtensionManager
from orchestrator.job_manager import ContainerJobManager
from secrets import SecretsStore
from tools.builder import (
    BuildSoftwareTool, BuilderConfig, LlmSoftwareBuilder, SoftwareBuilder,
)
from tools.builtin import (
    ApplyPatchTool,
    CancelJobTool,
    CreateJobTool,
    EchoTool,
    ExtensionInfoTool,
    FileUndoTool,
    GlobTool,
    GrepTool,
    HttpTool,
    JobEventsTool,
    JobPromptTool,
    JobStatusTool,
    JsonTool,
    ListDirTool,
    ListJobsTool,
    MemoryReadTool,
    MemorySearchTool,
    MemoryTreeTool,
    MemoryWriteTool,
    PlanUpdateTool,
    PromptQueue,
    ReadFileTool,
    ShellTool,
    SkillInstallTool,
    SkillListTool,
    SkillRemoveTool,
    SkillSearchTool,
    TimeTool,
    ToolAuthTool,
    ToolInstallTool,
    ToolListTool,
    ToolPermissionSetTool,
    ToolRemoveTool,
    ToolSearchTool,
    ToolUpgradeTool,
    WriteFileTool,
    shared_file_history,
    shared_read_file_state,
    MessageTool,
    MessageTool
)
from tools.rate_limiter import RateLimiter
from tools.tool import (
    ApprovalRequirement, EngineVersion, Tool, ToolDiscoverySummary, ToolDomain,
)
from tools.wasm import (
    Capabilities, OAuthRefreshConfig, ResourceLimits, SharedCredentialRegistry, WasmError,
    WasmStorageError, WasmToolRuntime, WasmToolStore, WasmToolWrapper,
)
from workspace import Workspace
from llm.recording import HttpInterceptor
from llm import (LlmProvider, ToolDefinition)
from skills.catalog import SkillCatalog
from skills.registry import SkillRegistry
from dataclasses import dataclass, field
import asyncio

logger = logging.getLogger(__name__)

# 内置工具名称列表，不能被动态注册覆盖，也不应被自我修复系统重建。
# 受保护的工具是作为 ironclaw 二进制文件的一部分编写的 ——
# 这些工具的错误是调用方问题（来自 LLM 的错误参数），而不是工具缺陷。
#
# 保持此列表与 src/tools/builtin/ 和 src/tools/builder/（对于 build_software）
# 中所有 fn name() -> &str 实现同步。为完整性起见，包含 web_fetch 等别名。
# 添加新的内置工具时，也将其名称添加到此列表中。
PROTECTED_TOOL_NAMES: list[str] = [
    # 核心工具
    "echo",
    "time",
    "json",
    "http",
    "shell",
    "restart",
    "message",
    # 文件工具
    "read_file",
    "write_file",
    "list_dir",
    "apply_patch",
    "glob",
    "grep",
    "file_undo",
    # 内存工具
    "memory_search",
    "memory_write",
    "memory_read",
    "memory_tree",
    # 作业工具
    "create_job",
    "list_jobs",
    "job_status",
    "job_events",
    "job_prompt",
    "cancel_job",
    # 扩展/工具管理
    "build_software",
    "tool_search",
    "tool_install",
    "tool_auth",
    "tool_list",
    "tool_remove",
    "tool_upgrade",
    "tool_info",
    "extension_info",
    # 例程工具
    "routine_create",
    "routine_list",
    "routine_update",
    "routine_delete",
    "routine_fire",
    "routine_history",
    "event_emit",
    # 技能工具
    "skill_list",
    "skill_search",
    "skill_install",
    "skill_remove",
    # 密钥工具
    "secret_list",
    "secret_delete",
    # 图片工具
    "image_generate",
    "image_edit",
    "image_analyze",
    # 计划工具
    "plan_update",
    # 权限工具
    "tool_permission_set",
    # 配对工具
    "pairing_approve",
    # 别名（web_fetch 在某些上下文中是 http 的别名）
    "web_fetch",
]


def is_protected_tool_name(name: str) -> bool:
    """
    检查工具名称是否为受保护的内置工具，不应被自我修复系统重建。
    受保护的工具是作为 ironclaw 二进制文件的一部分编写的；
    这些工具的错误是调用方问题（来自 LLM 的错误参数），而不是工具缺陷。

    参数:
        name: 要检查的工具名称。

    返回:
        bool: 如果工具名称在受保护列表中则返回 True。
    """
    return name in PROTECTED_TOOL_NAMES


# 注册可用工具
@dataclass
class ToolRegistry:
    tools: Dict[str, Tool] = field(default_factory=dict)

    # 追踪哪些名称是通过内置启动路径注册的。
    builtin_names: set[str] = field(default_factory=set)
    # 由 WASM 工具填充、供 HTTP 工具使用的共享凭证注册表。
    credential_registry: Optional[SharedCredentialRegistry] = None
    # 用于凭证注入的密钥存储（与 HTTP 工具共享）。
    secrets_store: Optional[SecretsStore] = None
    # 用于运行时凭证回退的缩小范围角色查找。
    role_lookup: Optional[UserStore] = None
    # 用于多租户凭证回退中用户角色检查的数据库句柄。
    db: Optional[Database] = None
    # 用于内置工具调用的共享速率限制器。
    rate_limiter: RateLimiter = field(default_factory=RateLimiter)
    # 可选的 HTTP 拦截器，会传播到注册的 WASM 包装器中。
    http_interceptor: Optional[HttpInterceptor] = None
    # 用于按轮次设置上下文的消息工具的引用。
    message_tool: Optional[MessageTool] = None
    # 当前活动的引擎版本。控制通过 `tool_definitions()`、`all()` 等方法可见的工具。
    # 默认为 V1。
    engine_version: EngineVersion = EngineVersion.V1

    # ---------- 静态辅助方法 ----------

    @staticmethod
    def tool_definition(tool: Tool) -> ToolDefinition:
        """
        从工具实例构建 ToolDefinition。

        """
        schema = tool.schema
        return ToolDefinition(
            name=schema.name,
            description=schema.description,
            parameters=schema.parameters,
        )

    @staticmethod
    def is_engine_visible(tool: Tool, version: EngineVersion) -> bool:
        """
        检查工具对指定引擎版本是否可见。

        对应 Rust: fn is_engine_visible(tool: &dyn Tool, version: EngineVersion) -> bool
        """
        return tool.engine_compatibility.is_visible_in(version)

    @staticmethod
    def resolve_key(tools: Dict[str, Tool], name: str) -> Optional[str]:
        """
        将工具名称解析为注册时使用的键，先尝试精确名称，
        然后尝试连字符→下划线和下划线→连字符别名。

        对应 Rust: fn resolve_key(tools: &HashMap<String, Arc<dyn Tool>>, name: &str) -> Option<String>
        """
        if name in tools:
            return name
        # 反向别名：连字符 → 下划线（LLM 规范化）
        underscore_alias = name.replace('-', '_')
        if underscore_alias != name and underscore_alias in tools:
            return underscore_alias
        # 遗留别名：下划线 → 连字符（旧版 WASM 扩展）
        hyphen_alias = name.replace('_', '-')
        if hyphen_alias != name and hyphen_alias in tools:
            return hyphen_alias
        return None

    # ---------- 构建器方法 ----------

    def with_credentials(
            self,
            credential_registry: SharedCredentialRegistry,
            secrets_store: SecretsStore,
    ) -> ToolRegistry:
        """
        创建带有凭据注入支持的注册表。

        对应 Rust: pub fn with_credentials(mut self, ...) -> Self
        """
        self.credential_registry = credential_registry
        self.secrets_store = secrets_store
        return self

    def with_database(self, db: Database) -> ToolRegistry:
        """
        附加数据库句柄以启用用户角色感知的工具行为。

        对应 Rust: pub fn with_database(mut self, db: Arc<dyn Database>) -> Self
        """
        self.role_lookup = db
        self.db = db
        return self

    def with_role_lookup(self, role_lookup: UserStore) -> ToolRegistry:
        """
        设置角色查找器。

        对应 Rust: pub fn with_role_lookup(mut self, role_lookup: Arc<dyn UserStore>) -> Self
        """
        self.role_lookup = role_lookup
        return self

    def with_http_interceptor(self, interceptor: HttpInterceptor) -> ToolRegistry:
        """
        设置 HTTP 拦截器。

        对应 Rust: pub fn with_http_interceptor(mut self, interceptor: Arc<dyn HttpInterceptor>) -> Self
        """
        self.http_interceptor = interceptor
        return self

    def with_engine_version(self, version: EngineVersion) -> ToolRegistry:
        """
        设置引擎版本。必须在包装到 Arc 之前调用。

        对应 Rust: pub fn with_engine_version(mut self, version: EngineVersion) -> Self
        """
        self.engine_version = version
        return self

    # ---------- 访问器 ----------

    def engine_version(self) -> EngineVersion:
        """对应 Rust: pub fn engine_version(&self) -> EngineVersion"""
        return self.engine_version

    def credential_registry(self) -> Optional[SharedCredentialRegistry]:
        """对应 Rust: pub fn credential_registry(&self) -> Option<&Arc<SharedCredentialRegistry>>"""
        return self.credential_registry

    def secrets_store(self) -> Optional[SecretsStore]:
        """对应 Rust: pub fn secrets_store(&self) -> Option<&Arc<dyn SecretsStore + Send + Sync>>"""
        return self.secrets_store

    def rate_limiter(self) -> RateLimiter:
        """对应 Rust: pub fn rate_limiter(&self) -> &RateLimiter"""
        return self.rate_limiter

    def database(self) -> Optional[Database]:
        """对应 Rust: pub fn database(&self) -> Option<&Arc<dyn Database>>"""
        return self.db

    def role_lookup(self) -> Optional[UserStore]:
        """对应 Rust: pub fn role_lookup(&self) -> Option<&Arc<dyn UserStore>>"""
        return self.role_lookup

    # ---------- 注册/注销 ----------

    async def register(self, tool: Tool) -> None:
        """
        注册一个工具。拒绝试图覆盖受保护内置名称的动态工具。
        同时拒绝包含 '.' 的工具名称，因为这与设置路径解析冲突。

        对应 Rust: pub async fn register(&self, tool: Arc<dyn Tool>)
        """
        name = tool.name()
        if '.' in name:
            logger.warning(
                "拒绝工具注册: 名称包含 '.' 与设置路径解析冲突: tool=%s", name
            )
            return

        async with self._builtin_names_lock:
            is_builtin = name in self._builtin_names

        if name in PROTECTED_TOOL_NAMES and is_builtin:
            logger.warning(
                "拒绝工具注册: 将覆盖内置工具: tool=%s", name
            )
            return

        async with self._tools_lock:
            self._tools[name] = tool

        logger.log(5, "已注册工具: %s", name)  # tracing::trace!

    def register_sync(self, tool: Tool) -> None:
        """
        注册一个工具（启动时使用的同步版本，标记为内置）。
        同时拒绝包含 '.' 的工具名称，因为这与设置路径解析冲突。

        对应 Rust: pub fn register_sync(&self, tool: Arc<dyn Tool>)
        """
        name = tool.name()
        if '.' in name:
            logger.warning(
                "拒绝工具注册: 名称包含 '.' 与设置路径解析冲突: tool=%s", name
            )
            return

        self._tools[name] = tool
        self._builtin_names.add(name)

        logger.debug("已注册工具: %s", name)

    async def unregister(self, name: str) -> Optional[Tool]:
        """
        注销一个工具。使用与 get() 相同的别名解析，
        因此传递连字符名称的调用者仍能找到以下划线注册的工具。

        对应 Rust: pub async fn unregister(&self, name: &str) -> Option<Arc<dyn Tool>>
        """
        async with self._tools_lock:
            key = self.resolve_key(self._tools, name)
            if key is None:
                return None
            return self._tools.pop(key, None)

    # ---------- 查找 ----------

    async def get(self, name: str) -> Optional[Tool]:
        """
        按名称获取工具。
        当精确名称未找到时，回退到连字符→下划线别名，
        以便来自规范化连字符的 LLM 提供商的工具调用仍能正确解析。

        对应 Rust: pub async fn get(&self, name: &str) -> Option<Arc<dyn Tool>>
        """
        async with self._tools_lock:
            key = self.resolve_key(self._tools, name)
            if key is None:
                return None
            return self._tools.get(key)

    async def resolve_name(self, name: str) -> Optional[str]:
        """
        将调用者提供的操作/工具名称解析为注册的工具 ID。

        对应 Rust: pub async fn resolve_name(&self, name: &str) -> Option<String>
        """
        async with self._tools_lock:
            return self.resolve_key(self._tools, name)

    async def get_resolved(self, name: str) -> Optional[tuple]:
        """
        解析并返回工具键和工具实例。

        对应 Rust: pub async fn get_resolved(&self, name: &str) -> Option<(String, Arc<dyn Tool>)>
        """
        async with self._tools_lock:
            key = self.resolve_key(self._tools, name)
            if key is None:
                return None
            tool = self._tools.get(key)
            if tool is None:
                return None
            return key, tool

    async def provider_extension_for_tool(self, name: str) -> Optional[str]:
        """
        解析工具/操作名称到其所属的提供商扩展。

        对应 Rust: pub async fn provider_extension_for_tool(&self, name: &str) -> Option<String>
        """
        async with self._tools_lock:
            key = self.resolve_key(self._tools, name)
            if key is None:
                return None
            tool = self._tools.get(key)
            if tool is None:
                return None
            return tool.provider_extension()

    async def has(self, name: str) -> bool:
        """对应 Rust: pub async fn has(&self, name: &str) -> bool"""
        return await self.get(name) is not None

    async def list(self) -> List[str]:
        """
        列出当前引擎版本中可见的工具名称。

        对应 Rust: pub async fn list(&self) -> Vec<String>
        """
        version = self.engine_version
        async with self._tools_lock:
            return [
                tool.name()
                for tool in self._tools.values()
                if self.is_engine_visible(tool, version)
            ]

    async def retain_only(self, names: List[str]) -> None:
        """
        仅保留名称在给定允许列表中的工具。
        如果 names 为空，则不执行任何操作（保留所有工具）。

        对应 Rust: pub async fn retain_only(&self, names: &[&str])
        """
        if not names:
            return
        names_set = set(names)
        async with self._tools_lock:
            self._tools = {k: v for k, v in self._tools.items() if k in names_set}

    def count(self) -> int:
        """对应 Rust: pub fn count(&self) -> usize"""
        return len(self._tools)

    async def all(self) -> List[Tool]:
        """
        获取当前引擎版本中可见的所有工具。

        对应 Rust: pub async fn all(&self) -> Vec<Arc<dyn Tool>>
        """
        version = self.engine_version
        async with self._tools_lock:
            return [
                tool
                for tool in self._tools.values()
                if self.is_engine_visible(tool, version)
            ]

    async def builtin_tool_names(self) -> Set[str]:
        """对应 Rust: pub async fn builtin_tool_names(&self) -> HashSet<String>"""
        async with self._builtin_names_lock:
            return set(self._builtin_names)

    # ---------- 工具定义 ----------

    async def tool_definitions(self) -> List[ToolDefinition]:
        """
        获取用于 LLM 函数调用的工具定义。
        自动按注册表的引擎版本过滤。

        对应 Rust: pub async fn tool_definitions(&self) -> Vec<ToolDefinition>
        """
        return await self.tool_definitions_for_engine(self.engine_version)

    async def tool_definitions_visible_under(
            self,
            policy: EffectiveRuntimePolicy,
    ) -> List[ToolDefinition]:
        """
        获取同时按引擎版本和已解析运行时策略过滤的工具定义。

        对应 Rust: pub async fn tool_definitions_visible_under(&self, policy: &EffectiveRuntimePolicy) -> Vec<ToolDefinition>
        """
        version = self.engine_version

        async with self._tools_lock:
            defs = [
                self.tool_definition(tool)
                for tool in self._tools.values()
                if self.is_engine_visible(tool, version)
                   and is_visible_under(policy, tool.runtime_affordance())
            ]

        defs.sort(key=lambda d: d.name)
        return defs

    async def tool_definitions_for_engine(self, version: EngineVersion) -> List["ToolDefinition"]:
        """
        获取按引擎版本过滤的工具定义。

        对应 Rust: pub async fn tool_definitions_for_engine(&self, version: EngineVersion) -> Vec<ToolDefinition>
        """
        async with self._tools_lock:
            defs = [
                self.tool_definition(tool)
                for tool in self._tools.values()
                if self.is_engine_visible(tool, version)
            ]

        defs.sort(key=lambda d: d.name)
        return defs

    async def tool_definitions_for(self, names: List[str]) -> List["ToolDefinition"]:
        """
        获取特定工具的工具定义。

        对应 Rust: pub async fn tool_definitions_for(&self, names: &[&str]) -> Vec<ToolDefinition>
        """
        async with self._tools_lock:
            result = []
            for name in names:
                key = self.resolve_key(self._tools, name)
                if key is not None:
                    tool = self._tools.get(key)
                    if tool is not None:
                        result.append(self.tool_definition(tool))
            return result

    async def tool_definitions_for_domain(self, domain: ToolDomain) -> List[ToolDefinition]:
        """
        获取按工具域过滤的工具定义。

        对应 Rust: pub async fn tool_definitions_for_domain(&self, domain: ToolDomain) -> Vec<ToolDefinition>
        """
        version = self.engine_version
        async with self._tools_lock:
            return [
                self.tool_definition(tool)
                for tool in self._tools.values()
                if tool.domain() == domain and self.is_engine_visible(tool, version)
            ]

    async def tool_definitions_excluding(self, deny: List[str]) -> List["ToolDefinition"]:
        """
        获取排除特定名称工具的工具定义。
        用于轻量级例程过滤掉被拒绝和需要审批的工具。

        对应 Rust: pub async fn tool_definitions_excluding(&self, deny: &[&str]) -> Vec<ToolDefinition>
        """
        empty_params = {}
        version = self.engine_version

        async with self._tools_lock:
            defs = []
            for tool in self._tools.values():
                if not self.is_engine_visible(tool, version):
                    continue
                if tool.name() in deny:
                    continue
                if tool.requires_approval(empty_params) != ApprovalRequirement.NEVER:
                    continue
                defs.append(self.tool_definition(tool))

        defs.sort(key=lambda d: d.name)
        return defs

    # ---------- 内置工具注册 ----------

    def register_builtin_tools(self) -> None:
        """注册所有内置工具。"""
        self.register_sync(EchoTool())
        self.register_sync(TimeTool())
        self.register_sync(JsonTool())
        self.register_sync(PlanUpdateTool())

        http = HttpTool()
        if self.credential_registry is not None and self.secrets_store is not None:
            http = http.with_credentials(self.credential_registry, self.secrets_store)
        if self.role_lookup is not None:
            http = http.with_role_lookup(self.role_lookup)
        self.register_sync(http)

        logger.debug("已注册 %d 个内置工具", self.count())

    def register_tool_info(self) -> None:
        """注册 tool_info 发现工具。

        需要 `Arc<Self>` 以便该工具能够在运行时向注册表查询其他工具的架构。
        请在 `register_builtin_tools()` 之后调用。
        """
        from tools.builtin import ToolInfoTool
        tool = ToolInfoTool(self)
        self.register_sync(tool)
        logger.debug("已注册 tool_info 发现工具")

    def register_system_tools(self) -> None:
        """注册系统自省工具（tools_list、version）。"""
        from tools.builtin.system import SystemToolsListTool, SystemVersionTool
        self.register_sync(SystemToolsListTool(self))
        self.register_sync(SystemVersionTool())
        logger.debug("已注册系统自省工具")

    def register_orchestrator_tools(self) -> None:
        """仅注册编排器域工具（对主进程安全）。"""
        self.register_builtin_tools()

    def register_container_tools(self) -> None:
        """注册容器域工具（文件系统、shell、代码）。"""
        self.register_dev_tools()

    def register_dev_tools(self) -> None:
        """注册开发工具用于构建软件。"""
        file_history = shared_file_history()
        read_state = shared_read_file_state()

        self.register_sync(ShellTool())
        self.register_sync(ReadFileTool().with_read_state(read_state))
        self.register_sync(WriteFileTool().with_file_history(file_history).with_read_state(read_state))
        self.register_sync(ListDirTool())
        self.register_sync(ApplyPatchTool().with_file_history(file_history).with_read_state(read_state))
        self.register_sync(GlobTool())
        self.register_sync(GrepTool())
        self.register_sync(FileUndoTool(file_history))

        logger.debug("已注册 8 个开发工具")

    def register_memory_tools_with_resolver(
            self,
            resolver: WorkspaceResolver,
            reasoning_llm: Optional["LlmProvider"],
            reasoning_enabled: bool,
    ) -> None:
        """
        使用工作区解析器注册记忆工具。

        记忆工具需要工作区解析器来实现持久化。如果您有可用的工作区，请在 `register_builtin_tools()` 之后调用此方法。
        接受可选的大语言模型提供者和推理标志，用于 `memory_search` 的推理增强召回。
        当 `reasoning_llm` 为 `Some` 且 `reasoning_enabled` 为 `true` 时，搜索工具可以在返回结果之前通过大语言模型调用对结果进行综合。"""
        self.register_sync(MemorySearchTool.with_reasoning(resolver, reasoning_llm, reasoning_enabled))
        self.register_sync(MemoryWriteTool(resolver))
        self.register_sync(MemoryReadTool(resolver))
        self.register_sync(MemoryTreeTool(resolver))
        logger.debug("已注册 4 个内存工具")

    def register_memory_tools(self, workspace: Workspace) -> None:
        """使用固定工作空间注册内存工具（向后兼容）。"""
        self.register_sync(MemorySearchTool.from_workspace(workspace))
        self.register_sync(MemoryWriteTool.from_workspace(workspace))
        self.register_sync(MemoryReadTool.from_workspace(workspace))
        self.register_sync(MemoryTreeTool.from_workspace(workspace))
        logger.debug("已注册 4 个内存工具")

    def register_job_tools(
            self,
            context_manager: ContextManager,
            scheduler_slot: Optional[SchedulerSlot] = None,
            job_manager: Optional["ContainerJobManager"] = None,
            store: Optional[Database] = None,
            job_event_tx: Optional["broadcast.Sender"] = None,
            inject_tx: Optional["mpsc.Sender"] = None,
            prompt_queue: Optional["PromptQueue"] = None,
            secrets_store: Optional[SecretsStore] = None,
    ) -> None:
        """注册作业管理工具。"""
        create_tool = CreateJobTool(context_manager)
        if scheduler_slot is not None:
            create_tool = create_tool.with_scheduler_slot(scheduler_slot)

        jm_for_cancel = job_manager
        store_for_cancel = store

        if job_manager is not None:
            create_tool = create_tool.with_sandbox(job_manager, store)
        if job_event_tx is not None and inject_tx is not None:
            create_tool = create_tool.with_monitor_deps(job_event_tx, inject_tx)
        if secrets_store is not None:
            create_tool = create_tool.with_secrets(secrets_store)

        self.register_sync(create_tool)
        self.register_sync(ListJobsTool(context_manager))
        self.register_sync(JobStatusTool(context_manager))

        cancel_tool = CancelJobTool(context_manager)
        if jm_for_cancel is not None:
            cancel_tool = cancel_tool.with_sandbox(jm_for_cancel, store_for_cancel)
        self.register_sync(cancel_tool)

        job_tool_count = 4

        if store is not None:
            self.register_sync(JobEventsTool(store, context_manager))
            job_tool_count += 1

        if prompt_queue is not None:
            self.register_sync(JobPromptTool(prompt_queue, context_manager))
            job_tool_count += 1

        logger.debug("已注册 %d 个作业管理工具", job_tool_count)

    def register_secrets_tools(self, store: SecretsStore) -> None:
        """注册密钥管理工具（list、delete）。"""
        self.register_sync(SecretListTool(store))
        self.register_sync(SecretDeleteTool(store))
        logger.debug("已注册 2 个密钥管理工具（list、delete）")

    def register_extension_tools(self, manager: ExtensionManager) -> None:
        """注册扩展管理工具。"""
        self.register_sync(ToolSearchTool(manager))
        self.register_sync(ToolInstallTool(manager))
        self.register_sync(ToolAuthTool(manager))
        self.register_sync(ToolListTool(manager))
        self.register_sync(ToolRemoveTool(manager))
        self.register_sync(ToolUpgradeTool(manager))
        self.register_sync(ExtensionInfoTool(manager))
        logger.debug("已注册 7 个扩展管理工具")

    def register_permission_tools(
            self,
            settings_store: Optional["SettingsStore"] = None,
    ) -> None:
        """注册权限管理工具（tool_permission_set）。"""
        self.register_sync(ToolPermissionSetTool(self, settings_store))
        logger.debug("已注册 tool_permission_set")

    def upgrade_tool_list(
            self,
            manager: ExtensionManager,
            settings_store: Optional[SettingsStore] = None,
    ) -> None:
        """升级 tool_list 以包含内置工具列表和按用户权限状态。"""
        list_tool = ToolListTool(manager).with_registry(self)
        if settings_store is not None:
            list_tool = list_tool.with_settings_store(settings_store)
        self.register_sync(list_tool)
        logger.debug("已使用内置注册表支持升级 tool_list")

    def register_skill_tools(
            self,
            registry: SkillRegistry,
            catalog: SkillCatalog,
    ) -> None:
        """注册技能管理工具。"""
        self.register_sync(SkillListTool(registry))
        self.register_sync(SkillSearchTool(registry, catalog))
        self.register_sync(SkillInstallTool(registry, catalog))
        self.register_sync(SkillRemoveTool(registry))
        logger.debug("已注册 4 个技能管理工具")

    def register_routine_tools(
            self,
            store: Database,
            engine: RoutineEngine,
    ) -> None:
        """注册例程管理工具。"""
        self.register_sync(RoutineCreateTool(store, engine))
        self.register_sync(RoutineListTool(store))
        self.register_sync(RoutineUpdateTool(store, engine))
        self.register_sync(RoutineDeleteTool(store, engine))
        self.register_sync(RoutineFireTool(store, engine))
        self.register_sync(RoutineHistoryTool(store))
        self.register_sync(EventEmitTool(engine))
        logger.debug("已注册 7 个例程管理工具")

    def register_plan_tools(self, sse: Optional["SseManager"] = None) -> None:
        """注册计划管理工具。"""
        tool = PlanUpdateTool()
        if sse is not None:
            tool = tool.with_sse(sse)
        self.register_sync(tool)
        logger.debug("已注册 plan_update 工具")

    async def register_message_tools(
            self,
            channel_manager: ChannelManager,
            extension_manager: Optional["ExtensionManager"] = None,
    ) -> None:
        """注册消息工具用于向频道发送消息。"""
        tool = MessageTool(channel_manager)
        if extension_manager is not None:
            tool = tool.with_extension_manager(extension_manager)

        async with self._message_tool_lock:
            self._message_tool = tool

        async with self._tools_lock:
            self._tools[tool.name()] = tool

        async with self._builtin_names_lock:
            self._builtin_names.add("message")

        logger.debug("已注册 message 工具")

    async def set_message_tool_context(self, channel: Optional[str], target: Optional[str]) -> None:
        """
        设置消息工具的默认频道和目标。
        在每个代理回合之前调用，传入当前对话的上下文。

        """
        if self.message_tool:
            await self.message_tool.set_context(channel, target)

    def register_image_tools(
            self,
            api_base_url: str,
            api_key: str,
            gen_model: str,
            base_dir: Optional[str] = None,
    ) -> None:
        """注册图片生成和编辑工具。"""
        self.register_sync(ImageGenerateTool(api_base_url, api_key, gen_model))
        self.register_sync(ImageEditTool(api_base_url, api_key, gen_model, base_dir))
        logger.debug("已注册 2 个图片工具（generate、edit）")

    def register_vision_tools(
            self,
            api_base_url: str,
            api_key: str,
            vision_model: str,
            base_dir: Optional[str] = None,
    ) -> None:
        """注册视觉/图片分析工具。"""
        self.register_sync(ImageAnalyzeTool(api_base_url, api_key, vision_model, base_dir))
        logger.debug("已注册 1 个视觉工具（analyze）")

    async def register_builder_tool(
            self,
            llm: LlmProvider,
            config: Optional[BuilderConfig] = None,
    ) -> SoftwareBuilder:
        """注册软件构建器工具。"""
        self.register_dev_tools()

        builder = LlmSoftwareBuilder(config or BuilderConfig(), llm, self)
        await self.register(BuildSoftwareTool(builder))
        logger.debug("已注册 software builder 工具")
        return builder

    async def register_wasm(self, reg: WasmToolRegistration) -> None:
        """从字节数组注册 WASM 工具。"""
        prepared = await reg.runtime.prepare(reg.name, reg.wasm_bytes, reg.limits)

        credential_mappings = reg.capabilities.http.credentials.values() if reg.capabilities.http else []
        oauth_refresh = reg.oauth_refresh

        wrapper = WasmToolWrapper(reg.runtime, prepared, reg.capabilities)

        if reg.description is not None:
            wrapper = wrapper.with_description(reg.description)
        if reg.schema is not None:
            wrapper = wrapper.with_schema(reg.schema)
        if reg.discovery_summary is not None:
            wrapper = wrapper.with_discovery_summary(reg.discovery_summary)
        if reg.secrets_store is not None:
            wrapper = wrapper.with_secrets_store(reg.secrets_store)
        if reg.role_lookup is not None:
            wrapper = wrapper.with_role_lookup(reg.role_lookup)
        if oauth_refresh is not None:
            wrapper = wrapper.with_oauth_refresh(oauth_refresh)
        if self.http_interceptor is not None:
            wrapper = wrapper.with_http_interceptor(self.http_interceptor)

        await self.register(wrapper)

        if self.credential_registry is not None and credential_mappings:
            self.credential_registry.add_mappings(list(credential_mappings))
            if oauth_refresh is not None:
                self.credential_registry.add_oauth_refresh_configs([(oauth_refresh.secret_name, oauth_refresh)])
            logger.debug(
                "已从 WASM 工具添加凭据映射: name=%s, credential_count=%d",
                reg.name, len(credential_mappings),
            )

        logger.debug("已注册 WASM 工具: name=%s", reg.name)

    async def register_wasm_from_storage(
            self,
            store: WasmToolStore,
            runtime: WasmToolRuntime,
            user_id: str,
            name: str,
    ) -> None:
        """从数据库存储注册 WASM 工具。"""
        tool_with_binary = await store.get_with_binary(user_id, name)
        stored_caps = await store.get_capabilities(tool_with_binary.tool.id)
        capabilities = stored_caps.to_capabilities() if stored_caps else None

        await self.register_wasm(WasmToolRegistration(
            name=tool_with_binary.tool.name,
            wasm_bytes=tool_with_binary.wasm_binary,
            runtime=runtime,
            capabilities=capabilities,
            limits=None,
            description=tool_with_binary.tool.description,
            schema=tool_with_binary.tool.parameters_schema,
            discovery_summary=None,
            secrets_store=self.secrets_store,
            role_lookup=self.role_lookup,
            oauth_refresh=None,
        ))

        logger.debug(
            "已从存储注册 WASM 工具: name=%s, user_id=%s, trust_level=%s",
            tool_with_binary.tool.name, user_id, tool_with_binary.tool.trust_level,
        )
