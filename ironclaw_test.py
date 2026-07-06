

"""
message处理步骤:
1. 接受用户消息;
2. 处理因需要用户干预(批准、授权)而暂停的任务;
3. 检查用户的输入是否合法，比如是否有操作系统文件的内容等等;
    3.1 此处可以考虑设计成`拒识模块`
4. 加载/创建 Project;
5. 处理并保存消息附件(如图片);
6. 向用户发送状态——"正在处理"
7. 获取/创建用户 conversation
8. 为线程绑定每次执行的上下文——用于在 gate 暂停期间保持状态的机制，确保 gate 解析时能够找到正确的上下文信息
9. 处理用户消息

"""

# ----------初始化----------
# 初始化: llm

# 初始化工具
AppBuilder.init_tools()

# ----------初始化V2----------
# 初始化: LlmBridgeAdapter
from bridge.llm_adapter import LlmBridgeAdapter
llm_adapter = LlmBridgeAdapter(
            agent.llm,
            agent.cheap_llm,
        )

# 初始化: EffectBridgeAdapter
# 作用: 统一管理工具批准、执行、速率等
from bridge.effect_adapter import EffectBridgeAdapter
from tools.registry import ToolRegistry
from safety import SafetyLayer

tools = ToolRegistry()
safety = SafetyLayer()
effect_adapter = EffectBridgeAdapter(
            tools,
            safety,
            agent.hooks,
        )
# 初始化: HybridStore
# 作用: 基于工作区的持久化；知识文档使用 frontmatter+markdown 以提升人类可读性。
from bridge.store_adapter import HybridStore
store = HybridStore(workspace=agent.workspace)
store.load_state_from_workspace()
store.cleanup_terminal_state(timedelta(minutes=5))
store.generate_engine_readme()

from engine.capability import LeaseManager, PolicyEngine, CapabilityRegistry
leases = LeaseManager()
policy = PolicyEngine()
capabilities = CapabilityRegistry()

# 初始化: ThreadManager
# 作用: 负责创建任务单、分配任务、跟踪进度
from engine.runtime.manager import ThreadManager
thread_manager = ThreadManager(
            llm_adapter,
            effect_adapter,
            store,
            capabilities,
            leases,
            policy,
        )



# ----------Step1: 接受用户消息----------
from  channels.channel import IncomingMessage

message = IncomingMessage(
    channel="gateway",
    user_id="",
    content="你是谁",
    thread_id=None
)

# ----------Step2: 处理因需要用户干预(批准、授权)而暂停的任务----------
from gate.store import PendingGateStore
from gate.persistence import FileGatePersistence
pending_gates = PendingGateStore(
            FileGatePersistence.with_default_path()
        )


# ----------Step7: 获取/创建用户 conversation----------
from engine.runtime.conversation import ConversationManager

# 7.1 初始化ConversationManager
    # 初始化需要用到的参数 ThreadManager 和 Store
conversation_manager = ConversationManager(thread_manager, store)

scope = message.conversation_scope
channel_key = (
    f"{message.channel}:{scope}" if scope is not None else message.channel
)

# "New Chat"按钮
conv_id = conversation_manager.get_or_create_conversation(
            channel_key, message.user_id
        )

# 7.2 处理用户消息
effective_content = message.content

from engine.types.project import Project
project = Project(user_id=message.user_id, name="default", description="Default project")
project_id = project.id

from engine.types.thread import ThreadConfig
thread_config = ThreadConfig()

scope_uuid = None
extra_metadata = {
                "conversation_scope": str(scope_uuid),
            }

conversation_manager.handle_user_message(
                conv_id,
                effective_content,
                project_id,
                message.user_id,
                thread_config,
                None,
                extra_metadata,
            )

# 执行流程:
#   1. 创建新的Thread
#   2. 线程类型授予显式能力租约
#   3. 添加来自先前线程的对话历史
#   4. 执行线程
thread_id = await conversation_manager.thread_manager.spawn_thread_with_history(
                goal=effective_content,  # 使用消息作为目标
                title=None,
                thread_type=ThreadType.Foreground,
                project_id=project_id,
                config=thread_config,
                parent_id=None,
                user_id=message.user_id,
                initial_messages=[],
                initial_metadata=extra_metadata,
            )
# 执行线程流程:
from engine.types.thread import Thread
thread = Thread(effective_content, ThreadType.Foreground, project_id, message.user_id, thread_config)
conversation_manager.thread_manager.start_thread(thread, message.user_id, False)


from engine.executor.loop_engine import ExecutionLoop

exec_loop = ExecutionLoop(
            thread=thread,
            llm=thread_manager.llm,
            effects=thread_manager.effects,
            leases=thread_manager.leases,
            policy=thread_manager.policy,
            signal_rx=signal_queue,
            user_id=message.user_id,
            gate_controller=gate_controller,
capabilities=thread_manager.capabilities,
event_queue=thread_manager.event_queue,
store=thread_manager.store,
        )
exec_loop.run()