from engine import ActionDef, ActionDiscoverySummary, ActionInventory
from tools import require_str, ToolError, ToolOutput

from dataclasses import dataclass, field
from typing import Optional, List, Any, Dict
from enum import Enum
import json
import logging

logger = logging.getLogger(__name__)


# ── 动作信息详情级别 ─────────────────────────────────────────

class ActionInfoDetail(Enum):
    """动作信息的详情级别"""
    Names = "Names"
    Summary = "Summary"
    Schema = "Schema"

    @classmethod
    def parse(cls, params: dict) -> "ActionInfoDetail":
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


# ── 动作发现 ─────────────────────────────────────────────────

@dataclass
class ActionDiscoverySummary:
    """动作发现摘要"""
    always_required: List[str] = field(default_factory=list)
    conditional_requirements: List[Any] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)


@dataclass
class ToolOutput:
    """工具输出"""
    result: Any = None
    is_error: bool = False
    duration_ms: int = 0

    @classmethod
    def success(cls, result: Any, duration_ms: int = 1) -> "ToolOutput":
        """创建成功的工具输出"""
        return cls(result=result, is_error=False, duration_ms=duration_ms)


class ActionDiscovery:
    """动作发现：从动作清单中查询工具信息"""

    @staticmethod
    def tool_info(params: dict, inventory: Any) -> Optional[ToolOutput]:
        """从动作清单中获取工具信息

        Args:
            params: 包含 name 和 detail 字段的参数字典
            inventory: 包含 inline 和 discoverable 动作的清单

        Returns:
            工具信息输出，如果未找到则返回 None
        """
        name = _require_str(params, "name")
        detail = ActionInfoDetail.parse(params)

        # 先在内联动作中查找，然后在可发现动作中查找
        action = ActionDiscovery.resolve(inventory.inline, name)
        if action is None:
            action = ActionDiscovery.resolve(inventory.discoverable, name)

        if action is None:
            return None

        return ActionDiscovery._tool_output(action, detail)

    @staticmethod
    def tool_info_from_actions(params: dict, actions: List[Any]) -> Optional[ToolOutput]:
        """从动作列表中获取工具信息

        Args:
            params: 包含 name 和 detail 字段的参数字典
            actions: 动作定义列表

        Returns:
            工具信息输出，如果未找到则返回 None
        """
        name = _require_str(params, "name")
        detail = ActionInfoDetail.parse(params)

        action = ActionDiscovery.resolve(actions, name)
        if action is None:
            return None

        return ActionDiscovery._tool_output(action, detail)

    @staticmethod
    def _tool_output(action: Any, detail: ActionInfoDetail) -> ToolOutput:
        """构建工具输出的内部实现"""
        schema = action.discovery_schema() if hasattr(action, 'discovery_schema') else {}
        info = {
            "name": action.discovery_name() if hasattr(action, 'discovery_name') else action.name,
            "description": action.description if hasattr(action, 'description') else "",
            "parameters": _schema_param_names(schema),
        }

        if detail == ActionInfoDetail.Summary:
            summary = None
            if hasattr(action, 'discovery_summary'):
                summary = action.discovery_summary()
            if summary is None:
                summary = _fallback_summary(schema)
            info["summary"] = _serialize_summary(summary)
        elif detail == ActionInfoDetail.Schema:
            info["schema"] = schema
        # Names 详情不需要额外处理

        return ToolOutput.success(info)

    @staticmethod
    def resolve(actions: List[Any], name: str) -> Optional[Any]:
        """在动作列表中按名称解析动作

        Args:
            actions: 动作定义列表
            name: 要查找的动作名称（支持连字符和下划线互换）

        Returns:
            匹配的动作定义，如果未找到则返回 None
        """
        for action in actions:
            if _matches_name(action, name):
                return action
        return None


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


def _matches_name(action: Any, name: str) -> bool:
    """检查动作名称是否匹配（支持连字符和下划线互换）"""
    action_name = action.name if hasattr(action, 'name') else ""
    if action_name == name:
        return True
    # 也匹配连字符/下划线变体
    if action_name.replace('-', '_') == name.replace('-', '_'):
        return True
    return False


def _schema_param_names(schema: Any) -> List[str]:
    """从 JSON Schema 中提取参数名称列表

    从顶层 properties 以及 allOf/oneOf/anyOf 变体中收集参数名称，
    使用有序集合去重并按字母顺序排序

    Args:
        schema: JSON Schema 对象（字典或可转换为字典的对象）

    Returns:
        排序后的参数名称列表
    """
    names = set()

    if isinstance(schema, dict):
        # 从顶层 properties 收集
        props = schema.get("properties")
        if isinstance(props, dict):
            names.update(props.keys())

        # 从 allOf/oneOf/anyOf 变体收集
        for key in ("allOf", "oneOf", "anyOf"):
            variants = schema.get(key)
            if isinstance(variants, list):
                for variant in variants:
                    if isinstance(variant, dict):
                        variant_props = variant.get("properties")
                        if isinstance(variant_props, dict):
                            names.update(variant_props.keys())

    return sorted(names)


def _fallback_summary(schema: Any) -> ActionDiscoverySummary:
    """从 JSON Schema 构建回退摘要

    当动作没有显式定义 discovery_summary 时，
    从 schema 的 required 字段提取始终必需的参数

    Args:
        schema: JSON Schema 对象

    Returns:
        ActionDiscoverySummary 对象
    """
    always_required = []
    if isinstance(schema, dict):
        required = schema.get("required")
        if isinstance(required, list):
            always_required = [str(item) for item in required if isinstance(item, str)]

    return ActionDiscoverySummary(always_required=always_required)


def _serialize_summary(summary: ActionDiscoverySummary) -> dict:
    """将 ActionDiscoverySummary 序列化为字典"""
    return {
        "always_required": summary.always_required,
        "conditional_requirements": summary.conditional_requirements,
        "notes": summary.notes,
        "examples": summary.examples,
    }


class ToolError(Exception):
    """工具错误"""

    class InvalidParameters(Exception):
        """无效参数错误"""

        def __init__(self, message: str):
            super().__init__(message)

    class ExecutionFailed(Exception):
        """执行失败错误"""

        def __init__(self, message: str):
            super().__init__(message)
