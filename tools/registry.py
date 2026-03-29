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


class ToolRegistry(BaseModel):
    """
    可用工具注册表。
    """
    model_config = ConfigDict(extra="ignore")

    # 带异步锁的工具
    tools: Dict[str, Tool] = Field(default_factory=dict)
    # 追踪哪些名称已注册为内置名称（受保护，不可被覆盖）。
    # 带异步锁的工具
    builtin_names: set[str] = Field(default_factory=set)
    # 由 WASM 工具填充、供 HTTP 工具使用的共享凭证注册表。
    credential_registry: Optional[SharedCredentialRegistry] = None
    # 用于凭证注入的密钥存储（与 HTTP 工具共享）。
    # 使用typing.Protocol声明，表示任何实现了 SecretsStore trait 的类型，并且额外要求该类型满足 Send（可跨线程传递所有权）和 Sync（可跨线程共享引用）
    secrets_store: Optional[SecretsStore] = None
    # 用于内置工具调用的共享速率限制器。
    rate_limiter: RateLimiter = Field(default_factory=RateLimiter)
    # 用于按轮次设置上下文的消息工具的引用。
    message_tool: Optional[MessageTool] = None  # 带异步锁实现
    _rwlock: aiorwlock.RWLock = Field(frozen=False, default_factory=aiorwlock.RWLock)

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
        # TODO
        async with self._rwlock.reader_lock:
            if name in self.builtin_names:
                logging.warning(f"拒绝工具注册：该工具将覆盖一个内置工具。: {name}")
                return

        async with self._rwlock.writer_lock:

    pub async fn
    register( & self, tool: Arc < dyn
    Tool >) {
        let
    name = tool.name().to_string();
    if self.builtin_names.read().await.contains( & name) {
    tracing::
        warn!(
        tool = %name,
    "Rejected tool registration: would shadow a built-in tool"
    );
    return;
    }
    self.tools.write().
    await.insert(name.clone(), tool);
    tracing::trace!("Registered tool: {}", name);
    }

    pub fn register_sync(&self, tool: Arc<dyn Tool>) {
        let name = tool.name().to_string();
        if let Ok(mut tools) = self.tools.try_write() {
            tools.insert(name.clone(), tool);
            // Mark as built-in so it can't be shadowed later
            if PROTECTED_TOOL_NAMES.contains(&name.as_str())
                && let Ok(mut builtins) = self.builtin_names.try_write()
            {
                builtins.insert(name.clone());
            }
            tracing::debug!("Registered tool: {}", name);
        }
    }

