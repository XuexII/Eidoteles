# 注册 `tool_info` 发现工具。
#
# 需要 `Arc<Self>` 以便该工具能够在运行时向注册表查询其他工具的架构。
# 请在 `register_builtin_tools()` 之后调用。


import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Any
import json
import logging
from context import JobContext
from tools.registry import ToolRegistry
from tools.tool import Tool, ToolDiscoverySummary, ToolError, ToolOutput, require_str

logger = logging.getLogger(__name__)


# ── 工具信息详情级别 ─────────────────────────────────────────

class ToolInfoDetail(Enum):
    """工具信息的详情级别"""
    Names = "Names"
    Summary = "Summary"
    Schema = "Schema"

    @classmethod
    def parse(cls, params: dict) -> "ToolInfoDetail":
        """从参数中解析详情级别

        Args:
            params: 包含 detail 或 include_schema 字段的参数字典

        Returns:
            解析后的详情级别

        Raises:
            ToolError: 当 detail 值无效时
        """
        # 向后兼容：include_schema=true 等同于 Schema
        if params.get("include_schema", False):
            return cls.Schema

        detail = params.get("detail")
        if detail is None or detail == "names":
            return cls.Names
        elif detail == "summary":
            return cls.Summary
        elif detail == "schema":
            return cls.Schema
        else:
            raise ToolError.InvalidParameters(
                f"无效的 detail '{detail}'（预期 'names'、'summary' 或 'schema'）"
            )


# ── 辅助函数 ─────────────────────────────────────────────────

def schema_param_names(schema: dict) -> List[str]:
    """从 JSON Schema 中提取参数名称列表

    从顶层 properties 以及 allOf/oneOf/anyOf 变体中收集参数名称，
    使用有序集合去重并按字母顺序排序

    Args:
        schema: JSON Schema 对象

    Returns:
        排序后的参数名称列表
    """
    names = set()

    # 从顶层 properties 收集
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict):
        names.update(props.keys())

    # 从 allOf/oneOf/anyOf 变体收集
    if isinstance(schema, dict):
        for key in ("allOf", "oneOf", "anyOf"):
            variants = schema.get(key)
            if isinstance(variants, list):
                for variant in variants:
                    if isinstance(variant, dict):
                        variant_props = variant.get("properties")
                        if isinstance(variant_props, dict):
                            names.update(variant_props.keys())

    return sorted(names)


def fallback_summary(schema: dict) -> "ToolDiscoverySummary":
    """从 JSON Schema 构建回退摘要

    当工具没有显式定义 discovery_summary 时，
    从 schema 的 required 字段提取始终必需的参数

    Args:
        schema: JSON Schema 对象

    Returns:
        ToolDiscoverySummary 对象
    """
    always_required = []
    if isinstance(schema, dict):
        required = schema.get("required")
        if isinstance(required, list):
            always_required = [str(item) for item in required if isinstance(item, str)]

    return ToolDiscoverySummary(always_required=always_required)


# ── ToolInfoTool ─────────────────────────────────────────────

@dataclass
class ToolDiscoverySummary:
    """工具发现摘要"""
    always_required: List[str] = field(default_factory=list)
    conditional_requirements: List[Any] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)


class ToolInfoTool(Tool):
    """获取任何工具信息的工具：描述、参数名称、精选摘要指导或完整的发现模式"""

    def __init__(self, registry: "ToolRegistry"):
        self.registry: ToolRegistry = registry

    def name(self) -> str:
        """工具名称"""
        return "tool_info"

    def description(self) -> str:
        """工具描述"""
        return "获取任何工具的信息：描述、参数名称、精选摘要指导或完整的发现模式。"

    def parameters_schema(self) -> dict:
        """参数 Schema"""
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要获取信息的工具名称",
                },
                "detail": {
                    "type": "string",
                    "enum": ["names", "summary", "schema"],
                    "description": "响应详情级别。'names' 仅返回参数名称。'summary' 添加精选规则/示例。'schema' 返回完整的发现模式。",
                    "default": "names",
                },
                "include_schema": {
                    "type": "boolean",
                    "description": "已弃用的兼容性别名，等同于 detail='schema'。如果为 true，包含完整的发现模式。",
                    "default": False,
                },
            },
            "required": ["name"],
        }

    async def execute(
            self,
            params: dict,
            ctx: "JobContext",
    ) -> "ToolOutput":
        """执行 tool_info 查询

        Args:
            params: 包含 name 和 detail 字段的参数字典
            ctx: 作业上下文

        Returns:
            工具输出结果

        Raises:
            ToolError: 当注册表不可用、工具未找到或参数无效时
        """
        start = time.monotonic()
        name = require_str(params, "name")
        detail = ToolInfoDetail.parse(params)

        if self.registry is None:
            raise ToolError.ExecutionFailed(
                "工具注册表不再可用于 tool_info"
            )

        tool = await self.registry.get(name)
        if tool is None:
            raise ToolError.InvalidParameters(f"未注册名为 '{name}' 的工具")

        # 拒绝在当前引擎版本中不可用的工具
        if hasattr(tool, 'engine_compatibility') and hasattr(self.registry, 'engine_version'):
            if not tool.engine_compatibility().is_visible_in(self.registry.engine_version()):
                raise ToolError.InvalidParameters(
                    f"工具 '{name}' 在当前引擎版本中不可用"
                )

        schema = tool.discovery_schema() if hasattr(tool, 'discovery_schema') else {}
        param_names = schema_param_names(schema)

        info = {
            "name": tool.name() if hasattr(tool, 'name') else name,
            "description": tool.description() if hasattr(tool, 'description') else "",
            "parameters": param_names,
        }

        if detail == ToolInfoDetail.Summary:
            summary = None
            if hasattr(tool, 'discovery_summary'):
                summary = tool.discovery_summary()
            if summary is None:
                summary = fallback_summary(schema)
            try:
                info["summary"] = {
                    "always_required": summary.always_required,
                    "conditional_requirements": summary.conditional_requirements,
                    "notes": summary.notes,
                    "examples": summary.examples,
                }
            except Exception as err:
                raise ToolError.ExecutionFailed(
                    f"序列化发现摘要失败: {err}"
                )
        elif detail == ToolInfoDetail.Schema:
            info["schema"] = schema
        # Names 详情不需要额外处理

        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.success(info, duration_ms)


# ── 辅助函数 ─────────────────────────────────────────────────

def _require_str(params: dict, key: str) -> str:
    """从参数字典中提取必需的字符串值

    Args:
        params: 参数字典
        key: 键名

    Returns:
        字符串值

    Raises:
        ToolError: 当值缺失或不是字符串时
    """
    value = params.get(key)
    if value is None:
        raise ToolError.InvalidParameters(f"缺少必需参数 '{key}'")
    if not isinstance(value, str):
        raise ToolError.InvalidParameters(f"参数 '{key}' 必须是字符串")
    return value