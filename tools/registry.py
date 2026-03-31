# 用于管理可用工具的工具注册表。


from secrets import SecretsStore

from context import ContextManager
from db import Database
from extensions import ExtensionManager
from llm import LlmProvider, ToolDefinition
from orchestrator.job_manager import ContainerJobManager
from skills.catalog import SkillCatalog
from skills.registry import SkillRegistry
from tools.builder import BuildSoftwareTool, BuilderConfig, LlmSoftwareBuilder
from tools.builtin import (
    ApplyPatchTool, CancelJobTool, CreateJobTool, EchoTool, ExtensionInfoTool, HttpTool,
    JobEventsTool, JobPromptTool, JobStatusTool, JsonTool, ListDirTool, ListJobsTool,
    MemoryReadTool, MemorySearchTool, MemoryTreeTool, MemoryWriteTool, PromptQueue, ReadFileTool,
    ShellTool, SkillInstallTool, SkillListTool, SkillRemoveTool, SkillSearchTool, TimeTool,
    ToolActivateTool, ToolAuthTool, ToolInstallTool, ToolListTool, ToolRemoveTool, ToolSearchTool,
    ToolUpgradeTool, WriteFileTool, MessageTool)
from tools.rate_limiter import RateLimiter
from tools.tool import ApprovalRequirement, Tool, ToolDomain
from tools.wasm import (
    Capabilities, OAuthRefreshConfig, ResourceLimits, SharedCredentialRegistry, WasmError,
    WasmStorageError, WasmToolRuntime, WasmToolStore, WasmToolWrapper)
from workspace import Workspace

from typing import Optional, List, Any, Dict, Union
from pydantic import BaseModel, Field, ConfigDict
import asyncio
import aiorwlock
import logging
from schems.async_schems import RWLockDict, RWLockSet, with_rwlock

PROTECTED_TOOL_NAMES = [
    "echo",
    "time",
    "json",
    "http",
    "shell",
    "read_file",
    "write_file",
    "list_dir",
    "apply_patch",
    "memory_search",
    "memory_write",
    "memory_read",
    "memory_tree",
    "create_job",
    "list_jobs",
    "job_status",
    "cancel_job",
    "build_software",
    "tool_search",
    "tool_install",
    "tool_auth",
    "tool_activate",
    "tool_list",
    "tool_remove",
    "routine_create",
    "routine_list",
    "routine_update",
    "routine_delete",
    "routine_fire",
    "routine_history",
    "event_emit",
    "skill_list",
    "skill_search",
    "skill_install",
    "skill_remove",
    "message",
    "web_fetch",
    "restart",
    "image_generate",
    "image_edit",
    "image_analyze",
    "tool_info",
]


class ToolRegistry(BaseModel):
    """
    可用工具注册表。
    """
    model_config = ConfigDict(extra="ignore")

    # 带异步锁的工具
    tools: RWLockDict = Field(default_factory=RWLockDict)
    # 追踪哪些名称已注册为内置名称（受保护，不可被覆盖）。
    # 带异步锁的工具
    builtin_names: RWLockSet = Field(default_factory=RWLockSet)
    # 由 WASM 工具填充、供 HTTP 工具使用的共享凭证注册表。
    credential_registry: Optional[SharedCredentialRegistry] = None
    # 用于凭证注入的密钥存储（与 HTTP 工具共享）。
    # 使用typing.Protocol声明，表示任何实现了 SecretsStore trait 的类型，并且额外要求该类型满足 Send（可跨线程传递所有权）和 Sync（可跨线程共享引用）
    secrets_store: Optional[SecretsStore] = None
    # 用于内置工具调用的共享速率限制器。
    rate_limiter: RateLimiter = Field(default_factory=RateLimiter)
    # 用于按轮次设置上下文的消息工具的引用。
    message_tool: Optional[MessageTool] = None  # 带异步锁实现

    def with_credentials(
            self,
            credential_registry: SharedCredentialRegistry,
            secrets_store: SecretsStore
    ):
        """
        创建一个支持凭证注入的注册表。
        :param credential_registry:
        :param secrets_store:
        :return:
        """
        self.credential_registry = credential_registry
        self.secrets_store = secrets_store

    async def register(self, tool: Tool):
        """
        注册一个工具。拒绝尝试覆盖内置名称的动态工具。
        :param tool:
        :return:
        """
        name = tool.name
        async with self.builtin_names.read:
            if name in self.builtin_names:
                logging.warning(f"拒绝工具注册：该工具将覆盖一个内置工具。: {name}")
                return

        async with self.tools.write:
            self.tools[name] = tool
        logging.debug(f"工具已注册: {name}")

    def register_sync(self, tool: Tool):
        """
        注册工具（启动时使用的同步版本，标记为内置）
        :param tool:
        :return:
        """
        name = tool.name

        self.tools[name] = tool
        # 标记为内置，使其以后无法被覆盖。
        if name in PROTECTED_TOOL_NAMES:
            self.builtin_names.add(name)

        logging.debug(f"工具已注册: {name}")

    async def unregister(self, name: str) -> Optional[Tool]:
        """
        卸载工具
        :param self:
        :param name:
        :return:
        """
        async with self.tools.write:
            return self.tools.pop(name, None)

    async def get(self, name: str) -> Optional[Tool]:
        """
        通过name获取工具
        :param name:
        :return:
        """
        async with self.tools.read:
            return self.tools.get(name, None)

    async def has(self, name: str) -> bool:
        """
        检查工具是否存在
        :param name:
        :return:
        """
        async with self.tools.read:
            return name in self.tools

    async def list(self) -> List[str]:
        """
        获取所有的工具名称列表
        :return:
        """
        async with self.tools.read:
            return list(self.tools.keys())

    async def retain_only(self, names: List[str]):
        """
        仅保留名称位于指定允许列表中的工具。
        如果 names 为空，则此操作无效（保留所有工具）。
        :param names: 
        :return: 
        """

        tools_names = await self.list()

        async with self.tools.write:
            for name in tools_names:
                if name not in names:
                    self.tools.pop(name, None)

    async def acount(self) -> int:
        """
        获取注册工具的数量
        :return:
        """

        async with self.tools.read:
            return len(self.tools)

    async def all(self) -> List[Tool]:
        """
        获取所有的工具
        :return:
        """
        async with self.tools.read:
            return list(self.tools.values())

    async def tool_definitions(self) -> List[ToolDefinition]:
        """
        获取用于大语言模型函数调用的工具定义。
        """

        defs = []
        async with self.tools.read:
            for _, tool in self.tools.items():
                tool_def = ToolDefinition(name=tool.name, description=tool.description,
                                          parameters=tool.parameters_schema)
                defs.append(tool_def)

        defs = sorted(defs, key=lambda x: x["name"])

        return defs


    async def tool_definitions_for(self, names: List[str]):
        """
        获取特定工具的描述
        """
        defs = []
        async with self.tools.read:
            for name in names:
                if tool := self.tools.get(name, None):
                    tool_def = ToolDefinition(name=tool.name, description=tool.description,
                                              parameters=tool.parameters_schema)
                    defs.append(tool_def)

        defs = sorted(defs, key=lambda x: x["name"])

        return defs

    def register_builtin_tools(self):
        """
        注册所有内置工具。
        """

        self.register_sync(EchoTool)
        self.register_sync(TimeTool)
        self.register_sync(JsonTool)

        http = HttpTool()

        if self.credential_registry and self.secrets_store:
            http = http.with_credentials(self.credential_registry, self.secrets_store)

        self.register_sync(http)

        logging.debug("共注册 {} 个内置工具", self.count())

    /// Register the `tool_info` discovery tool.
    ///
    /// Requires `Arc<Self>` so the tool can query the registry for other tools'
    /// schemas at runtime. Call after `register_builtin_tools()`.
    def register_tool_info(self: &Arc<Self>) {
        use crate::tools::builtin::ToolInfoTool;
        let tool = ToolInfoTool::new(Arc::downgrade(self));
        self.register_sync(Arc::new(tool));
        tracing::debug!("Registered tool_info discovery tool");
    }

    /// Register only orchestrator-domain tools (safe for the main process).
    ///
    /// This registers tools that don't touch the filesystem or run shell commands:
    /// echo, time, json, http. Use this when `allow_local_tools = false` and
    /// container-domain tools should only be available inside sandboxed containers.
    def register_orchestrator_tools(self) {
        self.register_builtin_tools();
        // register_builtin_tools already only registers orchestrator-domain tools
    }

    /// Register container-domain tools (filesystem, shell, code).
    ///
    /// These tools are intended to run inside sandboxed Docker containers.
    /// Call this in the worker process, not the orchestrator (unless `allow_local_tools = true`).
    def register_container_tools(self) {
        self.register_dev_tools();
    }

    /// Get tool definitions filtered by domain.
    async def tool_definitions_for_domain(self, domain: ToolDomain) -> Vec<ToolDefinition> {
        self.tools
            .read()
            .await
            .values()
            .filter(|tool| tool.domain() == domain)
            .map(|tool| ToolDefinition {
                name: tool.name().to_string(),
                description: tool.description().to_string(),
                parameters: tool.parameters_schema(),
            })
            .collect()
    }

    /// Get tool definitions excluding specific tools by name.
    ///
    /// Used by lightweight routines to filter out denylisted and approval-gated tools
    /// so the LLM only sees tools it is actually allowed to call.
    async def tool_definitions_excluding(self, deny: &[&str]) -> Vec<ToolDefinition> {
        let empty_params = serde_json::Value::Object(serde_json::Map::new());
        let mut defs: Vec<ToolDefinition> = self
            .tools
            .read()
            .await
            .values()
            .filter(|tool| {
                // Exclude denylisted tools
                if deny.contains(&tool.name()) {
                    return false;
                }
                // Exclude tools that require approval
                matches!(
                    tool.requires_approval(&empty_params),
                    ApprovalRequirement::Never
                )
            })
            .map(|tool| ToolDefinition {
                name: tool.name().to_string(),
                description: tool.description().to_string(),
                parameters: tool.parameters_schema(),
            })
            .collect();
        defs.sort_unstable_by(|a, b| a.name.cmp(&b.name));
        defs
    }

    /// Register development tools for building software.
    ///
    /// These tools provide shell access, file operations, and code editing
    /// capabilities needed for the software builder. Call this after
    /// `register_builtin_tools()` to enable code generation features.
    def register_dev_tools(self) {
        self.register_sync(Arc::new(ShellTool::new()));
        self.register_sync(Arc::new(ReadFileTool::new()));
        self.register_sync(Arc::new(WriteFileTool::new()));
        self.register_sync(Arc::new(ListDirTool::new()));
        self.register_sync(Arc::new(ApplyPatchTool::new()));

        tracing::debug!("Registered 5 development tools");
    }

    /// Register memory tools with a workspace.
    ///
    /// Memory tools require a workspace for persistence. Call this after
    /// `register_builtin_tools()` if you have a workspace available.
    def register_memory_tools(self, workspace: Arc<Workspace>) {
        self.register_sync(Arc::new(MemorySearchTool::new(Arc::clone(&workspace))));
        self.register_sync(Arc::new(MemoryWriteTool::new(Arc::clone(&workspace))));
        self.register_sync(Arc::new(MemoryReadTool::new(Arc::clone(&workspace))));
        self.register_sync(Arc::new(MemoryTreeTool::new(workspace)));

        tracing::debug!("Registered 4 memory tools");
    }

    /// Register job management tools.
    ///
    /// Job tools allow the LLM to create, list, check status, and cancel jobs.
    /// When sandbox deps are provided, `create_job` automatically delegates to
    /// Docker containers. Otherwise it dispatches via the Scheduler (which
    /// persists to DB and spawns a worker).
    #[allow(clippy::too_many_arguments)]
    def register_job_tools(
        self,
        context_manager: Arc<ContextManager>,
        scheduler_slot: Option<crate::tools::builtin::SchedulerSlot>,
        job_manager: Option<Arc<ContainerJobManager>>,
        store: Option<Arc<dyn Database>>,
        job_event_tx: Option<
            tokio::sync::broadcast::Sender<(uuid::Uuid, crate::channels::web::types::SseEvent)>,
        >,
        inject_tx: Option<tokio::sync::mpsc::Sender<crate::channels::IncomingMessage>>,
        prompt_queue: Option<PromptQueue>,
        secrets_store: Option<Arc<dyn SecretsStore + Send + Sync>>,
    ) {
        let mut create_tool = CreateJobTool::new(Arc::clone(&context_manager));
        if let Some(slot) = scheduler_slot {
            create_tool = create_tool.with_scheduler_slot(slot);
        }
        if let Some(jm) = job_manager {
            create_tool = create_tool.with_sandbox(jm, store.clone());
        }
        if let (Some(etx), Some(itx)) = (job_event_tx, inject_tx) {
            create_tool = create_tool.with_monitor_deps(etx, itx);
        }
        if let Some(secrets) = secrets_store {
            create_tool = create_tool.with_secrets(secrets);
        }
        self.register_sync(Arc::new(create_tool));
        self.register_sync(Arc::new(ListJobsTool::new(Arc::clone(&context_manager))));
        self.register_sync(Arc::new(JobStatusTool::new(Arc::clone(&context_manager))));
        self.register_sync(Arc::new(CancelJobTool::new(Arc::clone(&context_manager))));

        // Base tools: create, list, status, cancel
        let mut job_tool_count = 4;

        // Register event reader if store is available
        if let Some(store) = store {
            self.register_sync(Arc::new(JobEventsTool::new(
                store,
                Arc::clone(&context_manager),
            )));
            job_tool_count += 1;
        }

        // Register prompt tool if queue is available
        if let Some(pq) = prompt_queue {
            self.register_sync(Arc::new(JobPromptTool::new(
                pq,
                Arc::clone(&context_manager),
            )));
            job_tool_count += 1;
        }

        tracing::debug!("Registered {} job management tools", job_tool_count);
    }

    /// Register secret management tools (list, delete).
    ///
    /// These allow the LLM to persist API keys and tokens encrypted in the database.
    /// Values are never returned to the LLM; only names and metadata are exposed.
    def register_secrets_tools(
        self,
        store: Arc<dyn crate::secrets::SecretsStore + Send + Sync>,
    ) {
        use crate::tools::builtin::{SecretDeleteTool, SecretListTool};
        self.register_sync(Arc::new(SecretListTool::new(Arc::clone(&store))));
        self.register_sync(Arc::new(SecretDeleteTool::new(store)));
        tracing::debug!("Registered 2 secret management tools (list, delete)");
    }

    /// Register extension management tools (search, install, auth, activate, list, remove).
    ///
    /// These allow the LLM to manage MCP servers and WASM tools through conversation.
    def register_extension_tools(self, manager: Arc<ExtensionManager>) {
        self.register_sync(Arc::new(ToolSearchTool::new(Arc::clone(&manager))));
        self.register_sync(Arc::new(ToolInstallTool::new(Arc::clone(&manager))));
        self.register_sync(Arc::new(ToolAuthTool::new(Arc::clone(&manager))));
        self.register_sync(Arc::new(ToolActivateTool::new(Arc::clone(&manager))));
        self.register_sync(Arc::new(ToolListTool::new(Arc::clone(&manager))));
        self.register_sync(Arc::new(ToolRemoveTool::new(Arc::clone(&manager))));
        self.register_sync(Arc::new(ToolUpgradeTool::new(Arc::clone(&manager))));
        self.register_sync(Arc::new(ExtensionInfoTool::new(manager)));
        tracing::debug!("Registered 8 extension management tools");
    }

    /// Register skill management tools (list, search, install, remove).
    ///
    /// These allow the LLM to manage prompt-level skills through conversation.
    def register_skill_tools(
        self,
        registry: Arc<std::sync::RwLock<SkillRegistry>>,
        catalog: Arc<SkillCatalog>,
    ) {
        self.register_sync(Arc::new(SkillListTool::new(Arc::clone(&registry))));
        self.register_sync(Arc::new(SkillSearchTool::new(
            Arc::clone(&registry),
            Arc::clone(&catalog),
        )));
        self.register_sync(Arc::new(SkillInstallTool::new(
            Arc::clone(&registry),
            Arc::clone(&catalog),
        )));
        self.register_sync(Arc::new(SkillRemoveTool::new(registry)));
        tracing::debug!("Registered 4 skill management tools");
    }

    /// Register routine management tools.
    ///
    /// These allow the LLM to create, list, update, delete, and view history
    /// of routines (scheduled and event-driven tasks).
    def register_routine_tools(
        self,
        store: Arc<dyn Database>,
        engine: Arc<crate::agent::routine_engine::RoutineEngine>,
    ) {
        use crate::tools::builtin::{
            EventEmitTool, RoutineCreateTool, RoutineDeleteTool, RoutineFireTool,
            RoutineHistoryTool, RoutineListTool, RoutineUpdateTool,
        };
        self.register_sync(Arc::new(RoutineCreateTool::new(
            Arc::clone(&store),
            Arc::clone(&engine),
        )));
        self.register_sync(Arc::new(RoutineListTool::new(Arc::clone(&store))));
        self.register_sync(Arc::new(RoutineUpdateTool::new(
            Arc::clone(&store),
            Arc::clone(&engine),
        )));
        self.register_sync(Arc::new(RoutineDeleteTool::new(
            Arc::clone(&store),
            Arc::clone(&engine),
        )));
        self.register_sync(Arc::new(RoutineFireTool::new(
            Arc::clone(&store),
            Arc::clone(&engine),
        )));
        self.register_sync(Arc::new(RoutineHistoryTool::new(store)));
        self.register_sync(Arc::new(EventEmitTool::new(engine)));
        tracing::debug!("Registered 7 routine management tools");
    }

    /// Register message tool for sending messages to channels.
    async def register_message_tools(
        self,
        channel_manager: Arc<crate::channels::ChannelManager>,
        extension_manager: Option<Arc<crate::extensions::ExtensionManager>>,
    ) {
        use crate::tools::builtin::MessageTool;
        let mut tool = MessageTool::new(channel_manager);
        if let Some(extension_manager) = extension_manager {
            tool = tool.with_extension_manager(extension_manager);
        }
        let tool = Arc::new(tool);
        *self.message_tool.write().await = Some(Arc::clone(&tool));
        self.tools
            .write()
            .await
            .insert(tool.name().to_string(), tool as Arc<dyn Tool>);
        self.builtin_names
            .write()
            .await
            .insert("message".to_string());
        tracing::debug!("Registered message tool");
    }

    /// Set the default channel and target for the message tool.
    /// Call this before each agent turn with the current conversation's context.
    async def set_message_tool_context(self, channel: Option<String>, target: Option<String>) {
        if let Some(tool) = self.message_tool.read().await.as_ref() {
            tool.set_context(channel, target).await;
        }
    }

    /// Register image generation and editing tools.
    ///
    /// These tools allow the LLM to generate and edit images using cloud APIs.
    /// Requires an API base URL, API key, and model name for the image generation backend.
    def register_image_tools(
        self,
        api_base_url: String,
        api_key: String,
        gen_model: String,
        base_dir: Option<std::path::PathBuf>,
    ) {
        use crate::tools::builtin::{ImageEditTool, ImageGenerateTool};
        self.register_sync(Arc::new(ImageGenerateTool::new(
            api_base_url.clone(),
            api_key.clone(),
            gen_model.clone(),
        )));
        self.register_sync(Arc::new(ImageEditTool::new(
            api_base_url,
            api_key,
            gen_model,
            base_dir,
        )));
        tracing::debug!("Registered 2 image tools (generate, edit)");
    }

    /// Register vision/image analysis tools.
    ///
    /// These tools allow the LLM to analyze images using a vision-capable model.
    def register_vision_tools(
        self,
        api_base_url: String,
        api_key: String,
        vision_model: String,
        base_dir: Option<std::path::PathBuf>,
    ) {
        use crate::tools::builtin::ImageAnalyzeTool;
        self.register_sync(Arc::new(ImageAnalyzeTool::new(
            api_base_url,
            api_key,
            vision_model,
            base_dir,
        )));
        tracing::debug!("Registered 1 vision tool (analyze)");
    }

    /// Register the software builder tool.
    ///
    /// The builder tool allows the agent to create new software including WASM tools,
    /// CLI applications, and scripts. It uses an LLM-driven iterative build loop.
    ///
    /// This also registers the dev tools (shell, file operations) needed by the builder.
    async def register_builder_tool(
        self: &Arc<Self>,
        llm: Arc<dyn LlmProvider>,
        config: Option<BuilderConfig>,
    ) {
        // First register dev tools needed by the builder
        self.register_dev_tools();

        // Create the builder (arg order: config, llm, tools)
        let builder = Arc::new(LlmSoftwareBuilder::new(
            config.unwrap_or_default(),
            llm,
            Arc::clone(self),
        ));

        // Register the build_software tool
        self.register(Arc::new(BuildSoftwareTool::new(builder)))
            .await;

        tracing::debug!("Registered software builder tool");
    }

    /// Register a WASM tool from bytes.
    ///
    /// This validates and compiles the WASM component, then registers it as a tool.
    /// The tool will be executed in a sandboxed environment with the given capabilities.
    ///
    /// # Example
    ///
    /// ```ignore
    /// let runtime = Arc::new(WasmToolRuntime::new(WasmRuntimeConfig::default())?);
    /// let wasm_bytes = std::fs::read("my_tool.wasm")?;
    ///
    /// registry.register_wasm(WasmToolRegistration {
    ///     name: "my_tool",
    ///     wasm_bytes: &wasm_bytes,
    ///     runtime: &runtime,
    ///     description: Some("My custom tool description"),
    ///     ..Default::default()
    /// }).await?;
    /// ```
    async def register_wasm(self, reg: WasmToolRegistration<'_>) -> Result<(), WasmError> {
        // Prepare the module (validates and compiles)
        let prepared = reg
            .runtime
            .prepare(reg.name, reg.wasm_bytes, reg.limits)
            .await?;

        // Extract credential mappings before capabilities are moved into the wrapper
        let credential_mappings: Vec<crate::secrets::CredentialMapping> = reg
            .capabilities
            .http
            .as_ref()
            .map(|http| http.credentials.values().cloned().collect())
            .unwrap_or_default();

        // Create the wrapper
        let mut wrapper = WasmToolWrapper::new(Arc::clone(reg.runtime), prepared, reg.capabilities);

        // Apply overrides if provided
        if let Some(desc) = reg.description {
            wrapper = wrapper.with_description(desc);
        }
        if let Some(s) = reg.schema {
            wrapper = wrapper.with_schema(s);
        }
        if let Some(store) = reg.secrets_store {
            wrapper = wrapper.with_secrets_store(store);
        }
        if let Some(oauth) = reg.oauth_refresh {
            wrapper = wrapper.with_oauth_refresh(oauth);
        }

        // Register the tool
        self.register(Arc::new(wrapper)).await;

        // Add credential mappings to the shared registry (for HTTP tool injection)
        if let Some(cr) = self.credential_registry
            && !credential_mappings.is_empty()
        {
            let count = credential_mappings.len();
            cr.add_mappings(credential_mappings);
            tracing::debug!(
                name = reg.name,
                credential_count = count,
                "Added credential mappings from WASM tool"
            );
        }

        tracing::debug!(name = reg.name, "Registered WASM tool");
        Ok(())
    }

    /// Register a WASM tool from database storage.
    ///
    /// Loads the WASM binary with integrity verification and configures capabilities.
    ///
    /// # Example
    ///
    /// ```ignore
    /// let store = PostgresWasmToolStore::new(pool);
    /// let runtime = Arc::new(WasmToolRuntime::new(WasmRuntimeConfig::default())?);
    ///
    /// registry.register_wasm_from_storage(
    ///     &store,
    ///     &runtime,
    ///     "user_123",
    ///     "my_tool",
    /// ).await?;
    /// ```
    async def register_wasm_from_storage(
        self,
        store: &dyn WasmToolStore,
        runtime: &Arc<WasmToolRuntime>,
        user_id: &str,
        name: &str,
    ) -> Result<(), WasmRegistrationError> {
        // Load tool with integrity verification
        let tool_with_binary = store
            .get_with_binary(user_id, name)
            .await
            .map_err(WasmRegistrationError::Storage)?;

        // Load capabilities
        let stored_caps = store
            .get_capabilities(tool_with_binary.tool.id)
            .await
            .map_err(WasmRegistrationError::Storage)?;

        let capabilities = stored_caps.map(|c| c.to_capabilities()).unwrap_or_default();

        // Register the tool
        self.register_wasm(WasmToolRegistration {
            name: &tool_with_binary.tool.name,
            wasm_bytes: &tool_with_binary.wasm_binary,
            runtime,
            capabilities,
            limits: None,
            description: Some(&tool_with_binary.tool.description),
            schema: Some(tool_with_binary.tool.parameters_schema.clone()),
            secrets_store: self.secrets_store.clone(),
            oauth_refresh: None,
        })
        .await
        .map_err(WasmRegistrationError::Wasm)?;

        tracing::debug!(
            name = tool_with_binary.tool.name,
            user_id = user_id,
            trust_level = %tool_with_binary.tool.trust_level,
            "Registered WASM tool from storage"
        );

        Ok(())
    }

