import logging
from dataclasses import dataclass, field

from hooks.hook import Hook
from utils.async_schems import RWLockList

logger = logging.getLogger(__name__)


@dataclass
class HookEntry:
    """
    一个已注册的钩子及其优先级。
    """
    hook: Hook
    priority: int


@dataclass
class HookRegistry:
    hooks: RWLockList[HookEntry] = field(default_factory=RWLockList)
