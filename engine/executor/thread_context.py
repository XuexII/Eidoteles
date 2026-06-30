import uuid
from typing import Optional

from ..gate import GateController
from ..traits.effect import ThreadExecutionContext
from ..types.conversation import ConversationId
from ..types.step import StepId
from ..types.thread import Thread


def thread_execution_context(
        thread: Thread,
        step_id: StepId,
        current_call_id: Optional[str] = None,
        gate_controller: GateController = None,
) -> ThreadExecutionContext:
    """从当前线程状态构建执行上下文

    `gate_controller` 是必需的：调用者传入他们在构造时获得的控制器，
    以便执行器可以在 `Approval` 门控上内联暂停。
    不暂停的代码路径提供 `CancellingGateController`
    """
    # 从元数据中提取可选字段
    source_channel = thread.metadata.get("source_channel") if isinstance(thread.metadata, dict) else None
    if source_channel is not None and not isinstance(source_channel, str):
        source_channel = None

    user_timezone_str = thread.metadata.get("user_timezone") if isinstance(thread.metadata, dict) else None
    user_timezone = None
    if isinstance(user_timezone_str, str):
        user_timezone = ValidTimezone.parse(user_timezone_str)

    conversation_scope_str = thread.metadata.get("conversation_scope") if isinstance(thread.metadata, dict) else None
    conversation_scope = None
    if isinstance(conversation_scope_str, str):
        try:
            conversation_scope = uuid.UUID(conversation_scope_str)
        except (ValueError, AttributeError):
            pass

    conversation_id_str = thread.metadata.get("conversation_id") if isinstance(thread.metadata, dict) else None
    conversation_id = None
    if isinstance(conversation_id_str, str):
        try:
            conversation_id = ConversationId(uuid.UUID(conversation_id_str))
        except (ValueError, AttributeError):
            pass

    return ThreadExecutionContext(
        thread_id=thread.id,
        thread_type=thread.thread_type,
        project_id=thread.project_id,
        user_id=thread.user_id,
        step_id=step_id,
        current_call_id=current_call_id,
        source_channel=source_channel,
        user_timezone=user_timezone,
        thread_goal=thread.goal,
        available_actions_snapshot=None,
        available_action_inventory_snapshot=None,
        conversation_scope=conversation_scope,
        gate_controller=gate_controller,
        call_approval_granted=False,
        conversation_id=conversation_id,
    )
