from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Union, Optional, Tuple
from agent import SessionManager as AgentSessionManager
from agent.cost_guard import CostGuard
from channels.web.log_layer import LogBroadcaster
from config import Config
from context import ContextManager
from db import Database, DatabaseHandles
from extensions import ExtensionManager
from hooks import HookRegistry
from llm import LlmProvider, RecordingLlm, SessionManager, build_provider_chain
from safety import SafetyLayer
from secrets import SecretsStore
from skills import SkillRegistry
from skills.catalog import SkillCatalog
from tools import ToolRegistry
from tools.mcp import McpProcessManager, McpSessionManager
from tools.wasm import SharedCredentialRegistry
from tools.wasm import WasmToolRuntime
from workspace import EmbeddingProvider, Workspace
from extensions import RegistryEntry
from dataclasses import dataclass
from pathlib import Path
import logging
from tools.mcp.config import load_mcp_servers_from_db, load_mcp_servers
from tools.mcp import create_client_from_config
from tools.wasm import WasmToolLoader, load_dev_tools
import asyncio
from registry import RegistryCatalog
from extensions.registry import builtin_entries
from secrets import InMemorySecretsStore, SecretsCrypto, SecretString
from secrets.keychain import generate_master_key_hex

logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class AppComponents:
    """
    已完全初始化的应用程序组件，可用于通道连接和智能体构建。
    """
    # 经过数据库重载和密钥注入后的（可能已被修改的）配置。
    config: Config
    db: Optional[Database]
    # <Arc<dyn SecretsStore + Send + Sync>>
    secrets_store: Optional[SecretsStore]
    llm: LlmProvider
    cheap_llm: Optional[LlmProvider]
    safety: SafetyLayer
    tools: ToolRegistry
    embeddings: Optional[EmbeddingProvider]
    workspace: Optional[Workspace]
    extension_manager: Optional[ExtensionManager]
    mcp_session_manager: McpSessionManager
    mcp_process_manager: McpProcessManager
    wasm_tool_runtime: Optional[WasmToolRuntime]
    log_broadcaster: LogBroadcaster
    context_manager: ContextManager
    hooks: HookRegistry
    # 标准智能体运行时所使用的共享线程/会话管理器。
    agent_session_manager: AgentSessionManager
    # <Arc<std::sync::RwLock<SkillRegistry>>>
    skill_registry: Optional[SkillRegistry]
    skill_catalog: Optional[SkillCatalog]
    cost_guard: CostGuard
    recording_handle: Optional[RecordingLlm]
    session: SessionManager
    catalog_entries: List[RegistryEntry]
    dev_loaded_tool_names: List[str]


@dataclass
class AppBuilderFlags:
    """
    控制可选初始化阶段的选项。
    """
    no_db: bool = False  # 默认为 False，表示启用数据库


@dataclass
class AppBuilder:
    """
    负责协调 5 个机械式初始化阶段的构建器。
    创建一个新的构建器。

    session 和 log_broadcaster 在构建器之前创建，
    因为任何初始化阶段运行之前都必须先初始化追踪系统，且日志广播器是追踪层的一部分。
    """

    config: Config
    flags: AppBuilderFlags
    toml_path: Optional[Path]
    session: SessionManager
    log_broadcaster: LogBroadcaster

    # 累积状态
    db: Optional[Database] = None
    # <Arc<dyn SecretsStore + Send + Sync>>
    secrets_store: Optional[SecretsStore] = None

    # 测试覆盖项
    llm_override: Optional[LlmProvider] = None

    # 密钥存储所需的后端特定句柄
    handles: Optional[DatabaseHandles] = None

    def with_database(self, db: Database):
        """
        注入一个预先创建的数据库，跳过 `init_database()`。
        """
        self.db = db

    def with_llm(self, llm: LlmProvider):
        """
        注入一个预先创建的LLM provider，跳过`init_llm()`
        """
        self.llm_override = llm

    async def init_database(self):
        """
        阶段 1：初始化数据库后端。

        创建数据库连接、运行迁移、从数据库重新加载配置、
        将会话管理器关联到数据库、并清理过期作业。
        """
        if self.db:
            logger.debug("已经提供数据库，跳过`init_database()`")
            return

    async def init_secrets(self):
        """
        阶段 2：创建密钥存储。

        需要主密钥和后端特定的数据库句柄。创建存储后，
        将任何加密的大语言模型 API 密钥注入到配置覆盖层中，并重新解析配置。
        """
        pass

    async def init_llm(self) -> Tuple[LlmProvider, Optional[LlmProvider], Optional[RecordingLlm]]:
        """
        3：初始化大语言模型提供商链。

        委托给 `build_provider_chain` 函数，该函数会应用所有装饰器
        重试、智能路由、故障转移、熔断器、响应缓存）。
        """

        llm, cheap_llm, recording_handle = await build_provider_chain(self.config.llm, self.session)
        return llm, cheap_llm, recording_handle

    async def init_tools(self, llm: LlmProvider):
        """
        阶段 4：初始化安全模块、工具、嵌入模型和工作区。
        """

        safety = SafetyLayer(self.config.safety)
        logger.debug("Safety Layer初始化成功")

    async def _get_mcp_tools(
            self,
            server,
            mcp_session_manager,
            mcp_process_manager,
            secrets_store,
            owner_id,
            tools: ToolRegistry
    ):
        server_name = server.name

        try:
            client = await create_client_from_config(
                server,
                mcp_session_manager,
                mcp_process_manager,
                secrets_store,
                owner_id,
            )
        except Exception as e:
            logger.warning(f"创建{server_name}的MCP client失败: {e}")
            return

        try:
            mcp_tools = await client.list_tools()
        except Exception as e:
            logger.warning(f"")
            return

        try:
            tool_impls = await client.create_tools()
            for tool in tool_impls:
                await tools.register(tool)
            logger.debug(f"从MCP服务{server_name}中加载了{len(mcp_tools)}个工具")

        except Exception as e:
            logger.warning(f"从MCP服务{server_name}中创建工具时失败: {e}")

    async def init_extensions(
            self,
            tools: ToolRegistry,
            hooks: HookRegistry
    ) -> Tuple[
        McpSessionManager,
        McpProcessManager,
        WasmToolRuntime,
        ExtensionManager,
        List[RegistryEntry],
        List[str]]:
        """
        阶段 5：加载 WASM 工具、MCP 服务器，并创建扩展管理器。
        """

        mcp_session_manager = McpSessionManager()
        mcp_process_manager = McpProcessManager()

        # 提前创建 WASM 工具运行时，以便在启动后安装的扩展（例如通过 Web UI 安装的扩展）仍能被激活。
        # 工具目录仅在加载模块时需要，引擎初始化时并不需要。
        wasm_tool_runtime = None
        if self.config.wasm.enabled:
            try:
                wasm_tool_runtime = WasmToolRuntime(self.config.wasm.to_runtime_config())
            except Exception as e:
                logging.warning(f"Failed to initialize WASM runtime: {e}")

        # 并发加载 WASM 工具和 MCP 服务器
        async def _load_wasm_tools():
            """
            并发加载 WASM 工具和 MCP 服务器
            """
            dev_loaded_tool_names = []

            if not wasm_tool_runtime:
                return dev_loaded_tool_names

            loader = WasmToolLoader(runtime=wasm_tool_runtime, tools=tools)

            if self.secrets_store:
                loader = loader.with_secrets_store(self.secrets_store)

            tools_dir = self.config.wasm.tools_dir
            try:
                results = await loader.load_from_dir(tools_dir)
                if results.loaded:
                    logger.debug(f"从 {tools_dir} 中加载了 {len(results.loaded)} 个WASM工具")

                for path, err in results.errors:
                    logging.warning(f"加载WASM工具 {path} 失败: {err}")

            except Exception as e:
                logging.warning(f"扫描 WASM 工具目录失败: {e}")

            try:
                dev_results = await load_dev_tools(loader, tools_dir)
                dev_loaded_tool_names.extend(dev_results.loaded)
                if dev_loaded_tool_names:
                    logger.debug(f"从构建产物中加载了 {len(dev_loaded_tool_names)} 个开发版 WASM 工具")

            except Exception as e:
                logger.debug(f"没有发现dev WASM工具: {e}")

            return dev_loaded_tool_names

        async def _load_mcp_servers():
            try:
                if self.db:
                    servers = await load_mcp_servers_from_db(self.db, self.config.owner_id)
                else:
                    servers = await load_mcp_servers()

                enabled = servers.enabled_servers()
                if enabled:
                    logger.debug(f"正在加载 {len(enabled)} 个已配置的 MCP 服务器...")

                tasks = []

                for server in enabled:
                    task = self._get_mcp_tools(
                        server,
                        mcp_session_manager,
                        mcp_process_manager,
                        self.secrets_store,
                        self.config.owner_id,
                        tools
                    )
                    tasks.append(task)

                async for coro in asyncio.as_completed(tasks):
                    try:
                        result = await coro
                    except Exception as e:
                        logger.warning("MCP服务器加载任务发生报错：{e}")

            except Exception as e:
                logger.debug(f"MCP服务器加载报错")

        (dev_loaded_tool_names, _) = await asyncio.gather(_load_wasm_tools(), _load_mcp_servers())

        # 加载扩展发现所需的注册表目录条目
        catalog_entries = []
        try:
            catalog = RegistryCatalog.load_or_embedded()
            for m in catalog:
                m = m.to_registry_entry()
                if m:
                    catalog_entries.append(m)
            logger.debug(f"已加载用于扩展发现的注册表目录条目 {len(catalog_entries)}条")
        except Exception as e:
            logger.warning(f"加载注册表目录报错: {e}")

        # 追加内置条目（例如频道中继集成），以便它们出现在
        # Web UI 的可用扩展列表中。
        builtin = builtin_entries()
        for entry in builtin:
            # 检查 catalog_entries 中是否已存在相同名称的条目
            if not any(e.name == entry.name for e in catalog_entries):
                catalog_entries.append(entry)
        # 创建扩展管理器。如果未配置持久化存储，则使用临时的内存密钥存储
        # （列出/安装/激活功能仍然可用）。
        ext_secrets = self.secrets_store
        if not ext_secrets:
            ephemeral_key = SecretString(generate_master_key_hex())
            try:
                crypto = SecretsCrypto(ephemeral_key)
                ext_secrets = InMemorySecretsStore(crypto)
            except Exception as e:
                raise RuntimeError("ephemeral crypto") from e

        extension_manager = ExtensionManager(
            mcp_session_manager,
            mcp_process_manager,
            ext_secrets,
            tools,
            hooks,
            wasm_tool_runtime,
            self.config.wasm.tools_dir,
            self.config.channels.wasm_channels_dir,
            self.config.tunnel.public_url,
            self.config.owner_id,
            self.db,
            catalog_entries
        )
        tools.register_extension_tools(extension_manager)
        logging.debug("扩展管理器已初始化，并启用了对话内发现工具")

        # register_builder_tool() 内部已经调用了 register_dev_tools()，
        # 因此仅在构建器尚未注册开发工具时，才在此处注册。
        # 计算 builder_registered_dev_tools 标志
        builder_registered_dev_tools = (
                self.config.builder.enabled and
                (self.config.agent.allow_local_tools or not self.config.sandbox.enabled)
        )

        # 如果允许本地工具，但尚未注册 builder 的开发工具，则进行注册
        if self.config.agent.allow_local_tools and not builder_registered_dev_tools:
            tools.register_dev_tools()

        extensions = (mcp_session_manager,
                      mcp_process_manager,
                      wasm_tool_runtime,
                      extension_manager,
                      catalog_entries,
                      dev_loaded_tool_names)

        return extensions

    async def build_all(self) -> AppComponents:
        """
        按顺序运行所有初始化阶段并返回组装完成的组件。
        """
        await self.init_database()
        await self.init_secrets()

        # 初始化后验证：如果选择了非 nearai 后端，
        # 但凭证始终未能解析（延迟解析未找到任何密钥），
        # 则尽早失败并给出清晰的错误提示，而不是在运行时产生令人困惑的错误。
        if self.config.llm.backend != "nearai" and not self.config.llm.provider:
            backend = self.config

        if llm := self.llm_override.take():
            llm, cheap_llm, recording_handle = llm, None, None
        else:
            llm, cheap_llm, recording_handle = await self.init_llm()

        safety, tools, embeddings, workspace = await self.init_tools()

        # 提前创建钩子注册表，以便运行时扩展激活时能够注册钩子。
        hooks = HookRegistry()
        agent_session_manager = AgentSessionManager().with_hooks(hooks)
        (
            mcp_session_manager,
            mcp_process_manager,
            wasm_tool_runtime,
            extension_manager,
            catalog_entries,
            dev_loaded_tool_names,
        ) = self.init_extensions(tools, hooks)

        components = AppComponents(
            config=self.config,
            db=self.db,
            secrets_store=self.secrets_store,
            llm=llm,
            cheap_llm=cheap_llm,
            safety=safety,
            tools=tools,
            embeddings=embeddings,
            workspace=workspace,
            extension_manager=extension_manager,
            mcp_session_manager=mcp_session_manager,
            mcp_process_manager=mcp_process_manager,
            wasm_tool_runtime=wasm_tool_runtime,
            log_broadcaster=self.log_broadcaster,
            context_manager=context_manager,
            hooks=hooks,
            agent_session_manager=agent_session_manager,
            skill_registry=skill_registry,
            skill_catalog=skill_catalog,
            cost_guard=cost_guard,
            recording_handle=recording_handle,
            session=self.session,
            catalog_entries=catalog_entries,
            dev_loaded_tool_names=dev_loaded_tool_names
        )
