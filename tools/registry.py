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
    ToolUpgradeTool, WriteFileTool, MessageTool, ToolInfoTool, SchedulerSlot, SecretDeleteTool, SecretListTool)
from tools.rate_limiter import RateLimiter
from tools.tool import ApprovalRequirement, Tool, ToolDomain
from tools.wasm import (
    Capabilities, OAuthRefreshConfig, ResourceLimits, SharedCredentialRegistry, WasmError,
    WasmStorageError, WasmToolRuntime, WasmToolStore, WasmToolWrapper)
from workspace import Workspace
from agent.routine_engine import RoutineEngine

from channels import ChannelManager
from extensions import ExtensionManager

from typing import Optional, List, Any, Dict, Union
from pydantic import BaseModel, Field, ConfigDict
import asyncio
import aiorwlock
import logging
from schems.async_schems import RWLockDict, RWLockSet, with_rwlock
import weakref
from pathlib import Path

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
    # RwLock<Option<Arc<crate::tools::builtin::MessageTool>>>
    message_tool: Optional[MessageTool] = None  # TODO 不管是不是None，都得带锁

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

        self.register_sync(EchoTool())
        self.register_sync(TimeTool())
        self.register_sync(JsonTool())

        http = HttpTool()

        if self.credential_registry and self.secrets_store:
            http = http.with_credentials(self.credential_registry, self.secrets_store)

        self.register_sync(http)

        logging.debug("共注册 {} 个内置工具", self.count())

    def register_tool_info(self):
        """
        注册 tool_info 发现工具。
        需要 Arc<Self> 以便该工具能够在运行时向注册表查询其他工具的架构
        请在 register_builtin_tools() 之后调用
        :return: 
        """
        # # 弱引用: 当对象不再当对象不再使用时，缓存会自动失效。强引用会阻止对象销毁
        tool = ToolInfoTool(weakref.ref(self))
        self.register_sync(tool)
        logging.debug("注册 tool_info 发现工具。")

    def register_orchestrator_tools(self):
        """
        仅注册编排器域工具（对主进程安全）
        此方法注册不会触及文件系统或运行 shell 命令的工具：
        echo、time、json、http。当 allow_local_tools = false 且容器域工具仅应在沙盒容器内可用时使用此方法。
        :return: 
        """
        self.register_builtin_tools()
        # register_builtin_tools 已经仅注册编排器域工具。

    def register_container_tools(self):
        """
        注册容器域工具（文件系统、shell、代码）
        这些工具旨在沙盒化的 Docker 容器内运行。
        请在工作进程中调用此方法，而非编排器中（除非 allow_local_tools = true）
        :return:
        """
        self.register_dev_tools()

    async def tool_definitions_for_domain(self, domain: ToolDomain) -> List[ToolDefinition]:
        """
        获取按域过滤的工具定义。
        :param domain:
        :return:
        """
        defs = []
        async with self.tools.read:
            for _, tool in self.tools.items():
                if tool.domain == domain:
                    tool_def = ToolDefinition(name=tool.name, description=tool.description,
                                              parameters=tool.parameters_schema)
                    defs.append(tool_def)

        return defs


    async def tool_definitions_excluding(self, deny: List[str]) -> List[ToolDefinition]:
        """
        获取工具定义，并按名称排除特定工具。

        供轻量级例程使用，用于过滤掉被禁止和需审批的工具，
        使得大语言模型仅能看到其实际允许调用的工具。
        :param deny:
        :return:
        """

        empty_params = {}
        defs = []
        async with self.tools.read:
            for _, tool in self.tools.items():
                if tool.domain in deny:
                    # 排除被禁用的工具
                    continue

                if tool.requires_approval(empty_params) != ApprovalRequirement.NEVER:
                    # 排除需要审批的工具（只保留 NEVER 类型）
                    continue

                tool_def = ToolDefinition(name=tool.name, description=tool.description,
                                          parameters=tool.parameters_schema)
                defs.append(tool_def)

        defs = sorted(defs, key=lambda x: x["name"])
        return defs

    def register_dev_tools(self):
        """
        注册用于构建软件的开发工具

        这些工具提供软件开发所需的 shell 访问、文件操作和代码编辑能力
        请在 register_builtin_tools() 之后调用此方法以启用代码生成功能
        :return:
        """
        self.register_sync(ShellTool())
        self.register_sync(ReadFileTool())
        self.register_sync(WriteFileTool())
        self.register_sync(ListDirTool())
        self.register_sync(ApplyPatchTool())

        logging.debug("注册5个开发工具")

    def register_memory_tools(self, workspace: Workspace):
        """
        使用工作区注册记忆工具

        记忆工具需要工作区来实现持久化。如果您有可用的工作区，请在 register_builtin_tools() 之后调用此方法
        :param workspace:
        :return:
        """
        self.register_sync(MemorySearchTool(workspace=workspace))
        self.register_sync(MemoryWriteTool(workspace=workspace))
        self.register_sync(MemoryReadTool(workspace=workspace))
        self.register_sync(MemoryTreeTool(workspace=workspace))


        logging.debug("注册4个记忆工具")

    def register_job_tools(
        self,
        context_manager: ContextManager,
        scheduler_slot: Optional[SchedulerSlot]=None,
        job_manager: Optional[ContainerJobManager]=None,
        store: Optional[Database]=None,
        # TODO Optional<tokio::sync::broadcast::Sender<(uuid::Uuid, crate::channels::web::types::SseEvent)>,
        job_event_tx: Optional=None,
        # TODO tokio::sync::mpsc::Sender<crate::channels::IncomingMessage>
        inject_tx: Optional=None,
        prompt_queue: Optional[PromptQueue]=None,
        # TODO <Arc<dyn SecretsStore + Send + Sync>>
        secrets_store: Optional=None
    ):
        """
        注册作业管理工具

        作业工具允许大语言模型创建、列出、检查状态和取消作业
        当提供沙盒依赖时，create_job 会自动委托给 Docker 容器处理
        否则，它将通过调度器进行分发（调度器负责持久化到数据库并生成工作进程）
        :param context_manager:
        :param scheduler_slot:
        :param job_manager:
        :param store:
        :param job_event_tx:
        :param inject_tx:
        :param prompt_queue:
        :param secrets_store:
        :return:
        """

        create_tool = CreateJobTool(context_manager=context_manager)
        if scheduler_slot:
            create_tool = create_tool.with_scheduler_slot(scheduler_slot)

        if job_manager:
            create_tool = create_tool.with_sandbox(job_manager, store)

        if job_event_tx and inject_tx:
            create_tool = create_tool.with_monitor_deps(job_event_tx, inject_tx)

        if secrets_store:
            create_tool = create_tool.with_secrets(secrets_store)

        self.register_sync(create_tool)
        self.register_sync(ListJobsTool(context_manager=context_manager))
        self.register_sync(JobStatusTool(context_manager=context_manager))
        self.register_sync(CancelJobTool(context_manager=context_manager))

        # 基本工具: create, list, status, cancel
        job_tool_count = 4

        # 如果存储可用，则注册事件读取器。
        if store:
            self.register_sync(JobEventsTool(store=store, context_manager=context_manager))
            job_tool_count += 1

        # 注册prompt工具
        if prompt_queue:
            self.register_sync(JobPromptTool(prompt_queue=prompt_queue, context_manager=context_manager))
            job_tool_count += 1

        logging.debug("Registered {} job management tools", job_tool_count)

    def register_secrets_tools(
        self,
        # TODO <Arc<dyn SecretsStore + Send + Sync>>
        store,
    ):
        """
        注册密钥管理工具（列出、删除）。
        
        这些工具允许大语言模型将 API 密钥和令牌加密后持久化存储在数据库中
        密钥值永远不会返回给大语言模型，仅暴露名称和元数据
        :param store: 
        :return: 
        """

        self.register_sync(SecretListTool(store=store))
        self.register_sync(SecretDeleteTool(store=store))
        logging.debug("注册2个密钥管理工具（列出、删除）")


    def register_extension_tools(self, manager: ExtensionManager):
        """
        注册扩展管理工具（搜索、安装、认证、激活、列出、移除）

        这些工具允许大语言模型通过对话来管理 MCP 服务器和 WASM 工具
        :param manager:
        :return:
        """
        # self.register_sync(Arc::new(ToolSearchTool::new(Arc::clone(&manager))));
        # self.register_sync(Arc::new(ToolInstallTool::new(Arc::clone(&manager))));
        # self.register_sync(Arc::new(ToolAuthTool::new(Arc::clone(&manager))));
        # self.register_sync(Arc::new(ToolActivateTool::new(Arc::clone(&manager))));
        # self.register_sync(Arc::new(ToolListTool::new(Arc::clone(&manager))));
        # self.register_sync(Arc::new(ToolRemoveTool::new(Arc::clone(&manager))));
        # self.register_sync(Arc::new(ToolUpgradeTool::new(Arc::clone(&manager))));
        # self.register_sync(Arc::new(ExtensionInfoTool::new(manager)));
        # tracing::debug!("Registered 8 extension management tools");


    def register_skill_tools(
        self,
        registry: SkillRegistry, # TODO Arc<std::sync::RwLock<SkillRegistry>>
        catalog: SkillCatalog
    ):
        """
        注册技能管理工具（列出、搜索、安装、移除）

        这些工具允许大语言模型通过对话来管理提示词级别的技能
        :param registry:
        :param catalog:
        :return:
        """
        # self.register_sync(Arc::new(SkillListTool::new(Arc::clone(&registry))));
        # self.register_sync(Arc::new(SkillSearchTool::new(
        #     Arc::clone(&registry),
        #     Arc::clone(&catalog),
        # )));
        # self.register_sync(Arc::new(SkillInstallTool::new(
        #     Arc::clone(&registry),
        #     Arc::clone(&catalog),
        # )));
        # self.register_sync(Arc::new(SkillRemoveTool::new(registry)));
        # tracing::debug!("Registered 4 skill management tools");


    def register_routine_tools(
        self,
        store: Database,
        engine: RoutineEngine
    ):
        """
        注册例程管理工具

        这些工具允许大语言模型创建、列出、更新、删除以及查看例程的历史记录（包括定时任务和事件驱动型任务）
        :param store:
        :param engine:
        :return:
        """
        # use crate::tools::builtin::{
        #     EventEmitTool, RoutineCreateTool, RoutineDeleteTool, RoutineFireTool,
        #     RoutineHistoryTool, RoutineListTool, RoutineUpdateTool,
        # };
        # self.register_sync(Arc::new(RoutineCreateTool::new(
        #     Arc::clone(&store),
        #     Arc::clone(&engine),
        # )));
        # self.register_sync(Arc::new(RoutineListTool::new(Arc::clone(&store))));
        # self.register_sync(Arc::new(RoutineUpdateTool::new(
        #     Arc::clone(&store),
        #     Arc::clone(&engine),
        # )));
        # self.register_sync(Arc::new(RoutineDeleteTool::new(
        #     Arc::clone(&store),
        #     Arc::clone(&engine),
        # )));
        # self.register_sync(Arc::new(RoutineFireTool::new(
        #     Arc::clone(&store),
        #     Arc::clone(&engine),
        # )));
        # self.register_sync(Arc::new(RoutineHistoryTool::new(store)));
        # self.register_sync(Arc::new(EventEmitTool::new(engine)));
        # tracing::debug!("Registered 7 routine management tools");

    async def register_message_tools(
        self,
        channel_manager: ChannelManager,
        extension_manager: Optional[ExtensionManager]
    ):
        """
        注册用于向频道发送消息的消息工具
        :param channel_manager:
        :param extension_manager:
        :return:
        """
        # use crate::tools::builtin::MessageTool;
        # let mut tool = MessageTool::new(channel_manager);
        # if let Some(extension_manager) = extension_manager {
        #     tool = tool.with_extension_manager(extension_manager);
        # }
        # let tool = Arc::new(tool);
        # *self.message_tool.write().await = Some(Arc::clone(&tool));
        # self.tools
        #     .write()
        #     .await
        #     .insert(tool.name().to_string(), tool as Arc<dyn Tool>);
        # self.builtin_names
        #     .write()
        #     .await
        #     .insert("message".to_string());
        # tracing::debug!("Registered message tool");

    async def set_message_tool_context(self, channel: Optional[str], target: Optional[str]):
        """
        为消息工具设置默认频道和目标

        在每次智能体轮次之前，使用当前对话的上下文调用此方法
        :param channel:
        :param target:
        :return:
        """

        async with self.message_tool.read:
            if self.message_tool:
                await self.message_tool.set_context(channel, target)


    def register_image_tools(
        self,
        api_base_url: str,
        api_key: str,
        gen_model: str,
        base_dir: Optional[Path]
    ):
        """
        注册图像生成与编辑工具

        这些工具允许大语言模型通过云 API 生成和编辑图像
        需要图像生成后端的 API 基础 URL、API 密钥和模型名称
        :param api_base_url:
        :param api_key:
        :param gen_model:
        :param base_dir:
        :return:
        """

        # use crate::tools::builtin::{ImageEditTool, ImageGenerateTool};
        # self.register_sync(Arc::new(ImageGenerateTool::new(
        #     api_base_url.clone(),
        #     api_key.clone(),
        #     gen_model.clone(),
        # )));
        # self.register_sync(Arc::new(ImageEditTool::new(
        #     api_base_url,
        #     api_key,
        #     gen_model,
        #     base_dir,
        # )));
        # tracing::debug!("Registered 2 image tools (generate, edit)");


    def register_vision_tools(
        self,
        api_base_url: str,
        api_key: str,
        vision_model: str,
        base_dir: Optional[Path]
    ):
        """
        注册视觉/图像分析工具

        这些工具允许大语言模型使用具备视觉能力的模型来分析图像
        :param api_base_url:
        :param api_key:
        :param vision_model:
        :param base_dir:
        :return:
        """
        # use crate::tools::builtin::ImageAnalyzeTool;
        # self.register_sync(Arc::new(ImageAnalyzeTool::new(
        #     api_base_url,
        #     api_key,
        #     vision_model,
        #     base_dir,
        # )));
        # tracing::debug!("Registered 1 vision tool (analyze)");
        #


    async def register_builder_tool(
        self,
        llm: LlmProvider,
        config: Optional[BuilderConfig],
    ):
        """
        注册软件开发工具

        该开发工具允许智能体创建新的软件，包括 WASM 工具、命令行应用程序和脚本。它使用大语言模型驱动的迭代构建循环
        此操作还会注册开发工具（shell、文件操作），这些是开发工具所必需的
        :param llm:
        :param config:
        :return:
        """
        # // First register dev tools needed by the builder
        # self.register_dev_tools();
        #
        # // Create the builder (arg order: config, llm, tools)
        # let builder = Arc::new(LlmSoftwareBuilder::new(
        #     config.unwrap_or_default(),
        #     llm,
        #     Arc::clone(self),
        # ));
        #
        # // Register the build_software tool
        # self.register(Arc::new(BuildSoftwareTool::new(builder)))
        #     .await;
        #
        # tracing::debug!("Registered software builder tool");



    async def register_wasm(self, reg: WasmToolRegistration) -> Union[WasmError]:
        """
        从字节数组注册一个 WASM 工具

        该方法会验证并编译 WASM 组件，然后将其注册为一个工具
        该工具将在沙盒环境中以指定的能力执行
        :param reg: 带有生命周期参数的工具注册结构体，用于注册 WebAssembly 工具

        """


    #     // Prepare the module (validates and compiles)
    #     let prepared = reg
    #         .runtime
    #         .prepare(reg.name, reg.wasm_bytes, reg.limits)
    #         .await?;
    #
    #     // Extract credential mappings before capabilities are moved into the wrapper
    #     let credential_mappings: Vec<crate::secrets::CredentialMapping> = reg
    #         .capabilities
    #         .http
    #         .as_ref()
    #         .map(|http| http.credentials.values().cloned().collect())
    #         .unwrap_or_default();
    #
    #     // Create the wrapper
    #     let mut wrapper = WasmToolWrapper::new(Arc::clone(reg.runtime), prepared, reg.capabilities);
    #
    #     // Apply overrides if provided
    #     if let Some(desc) = reg.description {
    #         wrapper = wrapper.with_description(desc);
    #     }
    #     if let Some(s) = reg.schema {
    #         wrapper = wrapper.with_schema(s);
    #     }
    #     if let Some(store) = reg.secrets_store {
    #         wrapper = wrapper.with_secrets_store(store);
    #     }
    #     if let Some(oauth) = reg.oauth_refresh {
    #         wrapper = wrapper.with_oauth_refresh(oauth);
    #     }
    #
    #     // Register the tool
    #     self.register(Arc::new(wrapper)).await;
    #
    #     // Add credential mappings to the shared registry (for HTTP tool injection)
    #     if let Some(cr) = self.credential_registry
    #         && !credential_mappings.is_empty()
    #     {
    #         let count = credential_mappings.len();
    #         cr.add_mappings(credential_mappings);
    #         tracing::debug!(
    #             name = reg.name,
    #             credential_count = count,
    #             "Added credential mappings from WASM tool"
    #         );
    #     }
    #
    #     tracing::debug!(name = reg.name, "Registered WASM tool");
    #     Ok(())
    # }


    async def register_wasm_from_storage(
        self,
        store:WasmToolStore,
        runtime: WasmToolRuntime,
        user_id: str,
        name: str,
    ) -> Union[WasmRegistrationError]:
        """
        从数据库存储中注册一个 WASM 工具

        加载 WASM 二进制文件并进行完整性验证，同时配置其能力
        :param store:
        :param runtime:
        :param user_id:
        :param name:
        :return:
        """

    #     // Load tool with integrity verification
    #     let tool_with_binary = store
    #         .get_with_binary(user_id, name)
    #         .await
    #         .map_err(WasmRegistrationError::Storage)?;
    #
    #     // Load capabilities
    #     let stored_caps = store
    #         .get_capabilities(tool_with_binary.tool.id)
    #         .await
    #         .map_err(WasmRegistrationError::Storage)?;
    #
    #     let capabilities = stored_caps.map(|c| c.to_capabilities()).unwrap_or_default();
    #
    #     // Register the tool
    #     self.register_wasm(WasmToolRegistration {
    #         name: &tool_with_binary.tool.name,
    #         wasm_bytes: &tool_with_binary.wasm_binary,
    #         runtime,
    #         capabilities,
    #         limits: None,
    #         description: Some(&tool_with_binary.tool.description),
    #         schema: Some(tool_with_binary.tool.parameters_schema.clone()),
    #         secrets_store: self.secrets_store.clone(),
    #         oauth_refresh: None,
    #     })
    #     .await
    #     .map_err(WasmRegistrationError::Wasm)?;
    #
    #     tracing::debug!(
    #         name = tool_with_binary.tool.name,
    #         user_id = user_id,
    #         trust_level = %tool_with_binary.tool.trust_level,
    #         "Registered WASM tool from storage"
    #     );
    #
    #     Ok(())
    # }

