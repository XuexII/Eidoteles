from typing import Union

from runtime_policy import EffectiveRuntimePolicy, FilesystemBackendKind, NetworkMode, ProcessBackendKind
from tools.tool import ToolRuntimeAffordance


def is_visible_under(
        policy: EffectiveRuntimePolicy,
        affordance: ToolRuntimeAffordance,
) -> bool:
    """
    检查具有给定能力声明的工具在 `policy` 下是否应该在面向模型的工具列表中可见。

    `ToolRuntimeAffordance::None` 始终可见。其他变体根据每个变体文档所描述的
    已解析策略的后端/模式选择进行匹配。
    参数:
        policy: 已解析的运行时策略。
        affordance: 工具声明的运行时能力。
    返回:
        bool: 工具是否可见。
    """
    if affordance == ToolRuntimeAffordance.NONE:
        # 无特殊能力要求，始终可见
        return True

    elif affordance == ToolRuntimeAffordance.ANY_PROCESS:
        # 需要任意进程能力，策略的进程后端不是 None 时可见
        return policy.process_backend != ProcessBackendKind.

    elif affordance == ToolRuntimeAffordance.LOCAL_SHELL:
        # 需要本地 shell 能力，仅当策略的进程后端为 LocalHost 时可见
        return policy.process_backend == ProcessBackendKind.LocalHost

    elif affordance == ToolRuntimeAffordance.HOST_FILESYSTEM:
        # 需要宿主机文件系统能力，仅当策略的文件系统后端为 HostWorkspace 时可见
        return policy.filesystem_backend == FilesystemBackendKind.HostWorkspace

    elif affordance == ToolRuntimeAffordance.DIRECT_NETWORK:
        # 需要直接网络能力，仅当策略的网络模式为 Direct 或 DirectLogged 时可见
        return policy.network_mode in (NetworkMode.Direct, NetworkMode.DirectLogged)

    else:
        # 未预期的能力类型，保守起见返回 False
        return False
