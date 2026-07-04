# 系统内省工具。
#
# 这些工具将硬编码的系统命令（`/tools`、`/version`）替换为标准的 `Tool` 实现，这些实现经过标准分发管道，并带有审计追踪。它们可在 v1 和 v2 引擎中工作。
#
# 未来的工具（`system_skills_list`、`system_model_get/set`）计划作为 #2049 第 4 阶段后续工作的一部分。

from context import JobContext
from tools.registry import ToolRegistry
from tools.tool import Tool, ToolDiscoverySummary, ToolError, ToolOutput, require_str

import time
from typing import List, Any


# ── system_tools_list ────────────────────────────────────────

class SystemToolsListTool(Tool):
    """列出所有已注册的工具及其名称和描述"""

    def __init__(self, registry: "ToolRegistry"):
        self.registry: ToolRegistry = registry

    def name(self) -> str:
        return "system_tools_list"

    def description(self) -> str:
        return "列出所有已注册的工具及其名称和描述"

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    async def execute(
        self,
        params: dict,
        ctx: "JobContext",
    ) -> "ToolOutput":
        """执行工具列表查询"""
        start = time.monotonic()
        defs = await self.registry.tool_definitions()
        tools = [
            {"name": td.name, "description": td.description}
            for td in defs
        ]
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.success(
            {"tools": tools, "count": len(tools)},
            duration_ms,
        )


# ── system_version ───────────────────────────────────────────

class SystemVersionTool(Tool):
    """返回代理版本信息"""

    # 版本信息 — 在实际部署中这些值来自构建系统
    VERSION = "0.1.0"
    NAME = "ironclaw"

    def name(self) -> str:
        return "system_version"

    def description(self) -> str:
        return "获取代理版本和构建信息"

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    async def execute(
        self,
        params: dict,
        ctx: "JobContext",
    ) -> "ToolOutput":
        """执行版本查询"""
        start = time.monotonic()
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.success(
            {
                "version": self.VERSION,
                "name": self.NAME,
            },
            duration_ms,
        )