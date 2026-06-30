# 效果执行器特质。
#
# 引擎通过此特质将实际的动作执行委托给宿主。
# 主 crate 通过包装 `ToolRegistry` 和 `SafetyLayer` 来实现它——引擎本身对具体工具一无所知。

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List

from ironclaw_common import ValidTimezone
from ..gate import GateController
from ..types.capability import ActionDef, ActionInventory, CapabilityLease, CapabilitySummary
from ..types.conversation import ConversationId
from ..types.project import ProjectId
from ..types.step import ActionResult, StepId
from ..types.thread import ThreadId, ThreadType


@dataclass
class ThreadExecutionContext:
    """请求效果的线程的上下文信息

    传递给执行器，以便其可以做出上下文相关的决策
    （例如后台线程与前台线程中的不同工具行为）
    """
    thread_id: ThreadId
    thread_type: ThreadType
    project_id: ProjectId
    user_id: str
    step_id: StepId
    # 当前调用 ID
    current_call_id: Optional[str] = None
    # 此线程对话来源的频道（例如 "gateway"、"repl"）。
    # 由 mission_create 用于将 `notify_channels` 默认为当前频道
    source_channel: Optional[str] = None
    # 用户已验证的 IANA 时区（例如 "America/New_York"）。
    # 由 mission_create 用于默认 cron 时区，并暴露给 CodeAct 脚本
    user_timezone: Optional[ValidTimezone] = None
    # 执行线程的原始目标。
    # 主机适配器使用此来区分即时的一次性前台请求和显式的任务/例程设置
    thread_goal: Optional[str] = None
    # 当前步骤可见的可调用动作快照。
    # 由编排器在执行路径需要按需发现对等（例如 `tool_info`）时填充
    available_actions_snapshot: Optional[List[ActionDef]] = None
    # 当前步骤可见的完整动作清单快照
    available_action_inventory_snapshot: Optional[ActionInventory] = None
    # 主机频道在引擎分配 `thread_id` 之前提供的原始对话作用域标识符。
    # 让主机的效果执行器可以按主机注册时相同的键查找每个对话的状态
    # （例如调用者提供的工具目录），而不会与在主机将状态重新绑定到引擎
    # `thread_id` 之前就开始运行的引擎任务竞争
    conversation_scope: Optional[uuid.UUID] = None
    # 主机提供的回调，让执行器可以在 `Approval` 门控上内联暂停，
    # 而不是展开回编排器。
    #
    # 必需。不暂停的代码路径（解决方案后重放、后台任务写入、测试）
    # 提供 `CancellingGateController`，它将任何意外的门控显示为类型化拒绝，
    # 而不是历史上的 "execution paused by gate" RuntimeError 泄露
    gate_controller: GateController = None
    # 当主机已经为*此特定调用*（通过 `current_call_id` 匹配）收集了用户批准
    # 且执行器正在内联重试时设置为 `true`。主机的 `EffectExecutor` 实现
    # 使用此来跳过 `ApprovalRequirement::Always` / `AskEachTime` 门控，
    # 否则会在重试时重新触发 — 镜像传递 `approval_already_granted=true`
    # 的旧版 `execute_resolved_pending_action` 路径
    #
    # 一次性：限定为单次重试调用。在任何不属于内联重试的上下文中重置为 `false`
    call_approval_granted: bool = False
    # 发起此线程的对话（如果有）。携带到 `GatePauseRequest` 中，
    # 以便主机可以将门控匹配到发起 UI 表面，即使同一用户有多个并发对话
    # （例如两个浏览器标签页）。对于没有面向用户对话的后台任务线程为 `None`
    conversation_id: Optional[ConversationId] = None

    def clone(self) -> "ThreadExecutionContext":
        """创建上下文的一个浅拷贝"""
        return ThreadExecutionContext(
            thread_id=self.thread_id,
            thread_type=self.thread_type,
            project_id=self.project_id,
            user_id=self.user_id,
            step_id=self.step_id,
            current_call_id=self.current_call_id,
            source_channel=self.source_channel,
            user_timezone=self.user_timezone,
            thread_goal=self.thread_goal,
            available_actions_snapshot=list(
                self.available_actions_snapshot) if self.available_actions_snapshot else None,
            available_action_inventory_snapshot=self.available_action_inventory_snapshot,
            conversation_scope=self.conversation_scope,
            gate_controller=self.gate_controller,
            call_approval_granted=self.call_approval_granted,
            conversation_id=self.conversation_id,
        )

    def __repr__(self) -> str:
        """调试表示：门控控制器是不透明的，其余字段显示摘要"""
        actions_count = len(self.available_actions_snapshot) if self.available_actions_snapshot else 0
        return (
            f"ThreadExecutionContext("
            f"thread_id={self.thread_id}, "
            f"thread_type={self.thread_type}, "
            f"project_id={self.project_id}, "
            f"user_id={self.user_id}, "
            f"step_id={self.step_id}, "
            f"current_call_id={self.current_call_id}, "
            f"source_channel={self.source_channel}, "
            f"user_timezone={self.user_timezone}, "
            f"thread_goal={self.thread_goal}, "
            f"available_actions_snapshot={actions_count}, "
            f"available_action_inventory_snapshot={self.available_action_inventory_snapshot is not None}, "
            f"conversation_scope={self.conversation_scope}, "
            f"gate_controller=<GateController>, "
            f"call_approval_granted={self.call_approval_granted}, "
            f"conversation_id={self.conversation_id}"
            f")"
        )


class EffectExecutor(ABC):
    """能力动作执行的抽象

    主 crate 通过包装其 `ToolRegistry`、`SafetyLayer` 和工具执行管道来实现此接口。
    引擎调用 `execute_action` 并获取结果 — 所有安全、清理和实际工具调用
    都在主机中发生
    """

    @abstractmethod
    async def execute_action(
            self,
            action_name: str,
            parameters: dict,
            lease: CapabilityLease,
            context: ThreadExecutionContext,
    ) -> ActionResult:
        """执行能力动作

        执行器负责：
        1. 查找实际的工具实现
        2. 验证参数
        3. 应用安全检查（清理、泄露检测）
        4. 执行工具
        5. 返回结果
        """
        ...

    @abstractmethod
    async def available_actions(
            self,
            leases: List[CapabilityLease],
            context: ThreadExecutionContext,
    ) -> List[ActionDef]:
        """列出给定当前活跃租约集合的可用动作

        用于构建发送给 LLM 的动作定义
        """
        ...

    async def available_action_inventory(
            self,
            leases: List[CapabilityLease],
            context: ThreadExecutionContext,
    ) -> ActionInventory:
        """列出当前活跃租约集合的完整动作清单

        默认实现镜像 `available_actions()`
        """
        actions = await self.available_actions(leases, context)
        return ActionInventory(
            inline=actions,
            discoverable=[],
        )

    @abstractmethod
    async def available_capabilities(
            self,
            leases: List[CapabilityLease],
            context: ThreadExecutionContext,
    ) -> List[CapabilitySummary]:
        """列出给定当前运行时状态的能力后台摘要"""
        ...
