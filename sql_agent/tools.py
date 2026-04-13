from typing import Any
from copy import deepcopy
from utils import _get_json_schema_type, make_json_serializable
from dataclasses import dataclass
from abc import ABC, abstractmethod
from functools import wraps

def validate_after_init(cls):
    original_init = cls.__init__

    @wraps(original_init)
    def new_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.validate_arguments()

    cls.__init__ = new_init
    return cls


class Tool(ABC):
    name: str
    description: str
    inputs: dict[str, dict[str, str | type | bool]]
    required: list[str]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        validate_after_init(cls)

    def __call__(self, *args, **kwargs) -> Any:
        if len(args) == 1 and len(kwargs) == 0 and isinstance(args[0], dict):
            potential_kwargs = args[0]

            if all(key in self.inputs for key in potential_kwargs):
                args = ()
                kwargs = potential_kwargs

        result = self.forward(*args, **kwargs)

        return result

    @abstractmethod
    def forward(self, *args, **kwargs):
        pass


@dataclass
class ToolCall:
    name: str
    arguments: Any
    id: str

    def dict(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": make_json_serializable(self.arguments),
            },
        }

def get_tool_json_schema(tool: Tool) -> dict:
    properties = deepcopy(tool.inputs)
    required = []
    for key, value in properties.items():
        if value["type"] == "any":
            value["type"] = "string"

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": tool.required,
            },
        },
    }


def validate_tool_arguments(tool: Tool, arguments: Any) -> None:
    """
    验证工具参数
    """
    if isinstance(arguments, dict):
        for key, value in arguments.items():
            if key not in tool.inputs:
                raise ValueError(f"参数 {key} 不在该工具的输入模式中")

            actual_type = _get_json_schema_type(type(value))["type"]
            expected_type = tool.inputs[key]["type"]
            expected_type_is_nullable = tool.inputs[key].get("nullable", False)

            if (
                    (actual_type != expected_type if isinstance(expected_type,
                                                                str) else actual_type not in expected_type)
                    and expected_type != "any"
                    and not (actual_type == "null" and expected_type_is_nullable)
            ):
                if actual_type == "integer" and expected_type == "number":
                    continue
                raise TypeError(f"参数 {key} 的类型为 '{actual_type}'，但应为 '{tool.inputs[key]['type']}'")

        for key, schema in tool.inputs.items():
            key_is_nullable = schema.get("nullable", False)
            if key not in arguments and not key_is_nullable:
                raise ValueError(f"参数 {key} 是必需的")
        return None
    else:
        expected_type = list(tool.inputs.values())[0]["type"]
        if _get_json_schema_type(type(arguments))["type"] != expected_type and not expected_type == "any":
            raise TypeError(f"参数的类型为 '{type(arguments).__name__}'，但应为 '{expected_type}'")
