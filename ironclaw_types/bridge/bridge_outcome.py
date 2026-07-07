# 定义bridge的输出数据

from dataclasses import dataclass


# ----------数据结构: 定义bridge的返回结果类型---------
# 职责说明:
#   1. 定义"完成有文本响应"、"完成无文本响应"、""

@dataclass
class BridgeRespondOutcome:
    """将此文本响应发送给用户并结束本轮对话。"""
    text: str


@dataclass
class BridgeNoResponseOutcome:
    """无文本响应，但本轮对话正常完成。"""
    pass


@dataclass
class BridgePendingOutcome:
    """
    轮次已暂停——已创建门控（批准/身份验证/外部），用户必须解决该门控后才能继续。
    智能体循环不得发出终止的 `Done` 状态。
    """
    pass


BridgeOutcome = BridgeRespondOutcome | BridgeNoResponseOutcome | BridgePendingOutcome
