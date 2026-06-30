# 能力注册表——存储系统中可用的能力定义。

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..types.capability import ActionDef, Capability


@dataclass
class CapabilityRegistry:
    """所有已知能力的注册表

    能力在启动时注册（来自扩展、内置工具等），
    在授予租约或解析动作名称时查询
    """
    capabilities: Dict[str, Capability] = field(default_factory=dict)

    def register(self, capability: Capability) -> None:
        """注册一个能力。覆盖任何同名的现有能力"""
        self.capabilities[capability.name] = capability

    def get(self, name: str) -> Optional[Capability]:
        """按名称查找能力"""
        return self.capabilities.get(name)

    def list(self) -> List[Capability]:
        """列出所有已注册的能力"""
        return list(self.capabilities.values())

    def find_action(self, action_name: str) -> Optional[tuple]:
        """在所有能力中查找特定动作

        如果找到，返回 (capability_name, action_def)
        """
        for cap in self.capabilities.values():
            for action in cap.actions:
                if action.name == action_name:
                    return (cap.name, action)
        return None

    def get_action(self, capability_name: str, action_name: str) -> Optional[ActionDef]:
        """从特定能力中获取动作定义"""
        cap = self.capabilities.get(capability_name)
        if cap is None:
            return None
        for action in cap.actions:
            if action.name == action_name:
                return action
        return None

    def all_actions(self) -> List[ActionDef]:
        """收集所有能力中的所有动作定义"""
        actions = []
        for cap in self.capabilities.values():
            actions.extend(cap.actions)
        return actions

    def __len__(self) -> int:
        """已注册能力的数量"""
        return len(self.capabilities)

    def is_empty(self) -> bool:
        """注册表是否为空"""
        return len(self.capabilities) == 0
