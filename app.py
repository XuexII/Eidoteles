from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Union, Optional, Tuple
from channels.web.log_layer import LogBroadcaster
from config import Config
from llm import LlmProvider, RecordingLlm, SessionManager, build_provider_chain
from dataclasses import dataclass
from pathlib import Path
import logging
import asyncio
from db import Database
from tools import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class AppComponents:
    """
    已完全初始化的应用程序组件，可用于通道连接和智能体构建。
    """
    # 经过数据库重载和密钥注入后的（可能已被修改的）配置。
    config: Config
    db: Optional[Database] = None
    llm: LlmProvider
    tools: ToolRegistry


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
    # secrets_store: Optional[SecretsStore] = None

    # 测试覆盖项
    # llm_override: Optional[LlmProvider] = None

    # 密钥存储所需的后端特定句柄
    # handles: Optional[DatabaseHandles] = None

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

        # safety = SafetyLayer(self.config.safety)
        # logger.debug("Safety Layer初始化成功")

        safety = None
        tools = None
        embeddings = None
        workspace = None

        return safety, tools, embeddings, workspace

    async def build_all(self) -> AppComponents:
        """
        按顺序运行所有初始化阶段并返回组装完成的组件。
        """
        # 初始化数据库
        # await self.init_database()
        # 初始化密钥库
        # await self.init_secrets()

        # 初始化llm
        llm, cheap_llm, recording_handle = await self.init_llm()

        # 初始化后验证：如果选择了非 nearai 后端，
        # 但凭证始终未能解析（延迟解析未找到任何密钥），
        # 则尽早失败并给出清晰的错误提示，而不是在运行时产生令人困惑的错误。
        # if self.config.llm.backend != "nearai" and not self.config.llm.provider:
        #     backend = self.config
        #
        # if llm := self.llm_override.take():
        #     llm, cheap_llm, recording_handle = llm, None, None
        # else:
        #     llm, cheap_llm, recording_handle = await self.init_llm()

        safety, tools, embeddings, workspace = await self.init_tools(llm)

        # 提前创建钩子注册表，以便运行时扩展激活时能够注册钩子。
        # hooks = HookRegistry()
        # agent_session_manager = AgentSessionManager().with_hooks(hooks)
        # (
        #     mcp_session_manager,
        #     mcp_process_manager,
        #     wasm_tool_runtime,
        #     extension_manager,
        #     catalog_entries,
        #     dev_loaded_tool_names,
        # ) = self.init_extensions(tools, hooks)

        components = AppComponents(
            config=self.config,
            llm=llm,
            tools=tools
        )
        return components
