from dataclasses import dataclass

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
    """
    在关键生命周期点执行自定义逻辑，实现可扩展的事件处理。

    入站钩子（Inbound）：

    消息到达时执行
    可用于输入验证、内容过滤
    可以拒绝或修改消息
    工具调用钩子（BeforeToolCall）：

    工具执行前触发
    可用于参数修改、权限检查
    可以拒绝工具调用
    出站钩子（Outbound）：

    响应发送前触发
    可用于内容审查、日志记录
    可以修改最终响应
    """
    hooks: RWLockList[HookEntry] = field(default_factory=RWLockList)
