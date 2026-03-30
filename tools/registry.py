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

    async def count(self) -> int:
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

    /// Get tool definitions for LLM function calling.
    pub async fn tool_definitions(&self) -> Vec<ToolDefinition> {
        let mut defs: Vec<ToolDefinition> = self
            .tools
            .read()
            .await
            .values()
            .map(|tool| ToolDefinition {
                name: tool.name().to_string(),
                description: tool.description().to_string(),
                parameters: tool.parameters_schema(),
            })
            .collect();
        defs.sort_unstable_by(|a, b| a.name.cmp(&b.name));
        defs
    }