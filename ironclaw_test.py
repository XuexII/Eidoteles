

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
# ----------初始化: init_tools----------
safety = SafetyLayer(**self.config.safety)
tools = ToolRegistry().with_engine_version(engine_version)
# 注册内置工具
tools.register_builtin_tools()
# 注册 获取工具详情的工具
tools.register_tool_info()
# 查看所有工具列表和版本的 工具
tools.register_system_tools()

embeddings = None
workspace =  None
builder = None
credential_registry = SharedCredentialRegistry()
http_interceptor = None
workspace_resolver = None

hooks = HookRegistry()
agent_session_manager = AgentSessionManager().with_hooks(hooks)

settings_store: Optional[SettingsStore] = None
settings_cache: Optional[CachedSettingsStore] = None
ownership_cache = OwnershipCache()

# ----------初始化: init_extensions----------
mcp_session_manager = McpSessionManager()
mcp_process_manager = McpProcessManager()
wasm_tool_runtime = None
extension_manager = ExtensionManager()
catalog = RegistryCatalog.load_or_embedded()
catalog_entries = catalog.discovery_entries()
# WASM 工具
dev_loaded_tool_names = []


skill_registry = None
skill_catalog = None
tools.register_skill_tools(skill_registry, skill_catalog)
context_manager = ContextManager(self.config.agent.max_parallel_jobs)
cost_guard = CostGuard(
            CostGuardConfig(
                max_cost_per_day_cents=self.config.agent.max_cost_per_day_cents,
                max_actions_per_hour=self.config.agent.max_actions_per_hour,
                max_cost_per_user_per_day_cents=self.config.agent.max_cost_per_user_per_day_cents,
            )
        )

components = AppComponents(
            config=self.config,
            db=self.db,
            secrets_store=self.secrets_store,
            llm=llm,
            cheap_llm=cheap_llm,
            llm_reload=llm_reload,
            safety=safety,
            tools=tools,
            embeddings=embeddings,
            workspace=workspace,
            settings_store=settings_store,
            settings_cache=settings_cache,
            extension_manager=extension_manager,
            mcp_session_manager=mcp_session_manager,
            mcp_process_manager=mcp_process_manager,
            wasm_tool_runtime=wasm_tool_runtime,
            log_broadcaster=self.log_broadcaster,
            context_manager=context_manager,
            hooks=hooks,
            agent_session_manager=agent_session_manager,
            skill_registry=skill_registry,
            skill_catalog=skill_catalog,
            cost_guard=cost_guard,
            recording_handle=recording_handle,
            http_interceptor=http_interceptor,
            session=self.session,
            catalog_entries=catalog_entries,
            dev_loaded_tool_names=dev_loaded_tool_names,
            builder=builder,
            ownership_cache=ownership_cache,
        )

deps = AgentDeps(
        owner_id=config.owner_id,
        settings_store=components.settings_store,
        store=components.db,
        llm=components.llm,
        cheap_llm=components.cheap_llm,
        safety=components.safety,
        tools=components.tools,
        workspace=components.workspace,
        extension_manager=components.extension_manager,
        skill_registry=components.skill_registry,
        skill_catalog=components.skill_catalog,
        skills_config=config.skills,
        hooks=components.hooks,
        auth_manager=auth_manager,
        cost_guard=components.cost_guard,
        sse_tx=sse_manager,
        http_interceptor=http_interceptor,
        transcription=(
            ironclaw_llm.transcription.TranscriptionMiddleware(config.transcription.create_provider())
            if config.transcription.create_provider() is not None
            else None
        ),
        document_extraction=ironclaw.document_extraction.DocumentExtractionMiddleware(),
        sandbox_readiness=(
            ironclaw.agent.routine_engine.SandboxReadiness.DisabledByConfig
            if not config.sandbox.enabled or docker_status == ironclaw.sandbox.DockerStatus.Disabled
            else ironclaw.agent.routine_engine.SandboxReadiness.Available
            if docker_status.is_ok()
            else ironclaw.agent.routine_engine.SandboxReadiness.DockerUnavailable
        ),
        builder=components.builder,
        llm_backend=config.llm.backend,
        tenant_rates=ironclaw.tenant.TenantRateRegistry(
            config.agent.max_llm_concurrent_per_user or 4,
            config.agent.max_jobs_concurrent_per_user or 3
        ),
        # 在配置加载时由Config::with_runtime_overrides解析。
        # 调度器通过tool_definitions_visible_under(policy)路由面向模型的工具列表，
        # 以便在模型调用之前隐藏配置文件不可能的功能
        # （例如在多租户托管下的提供者主机shell）。（#3045 PR 4 + PR 5）。
        runtime_policy=config.runtime.effective_policy if config.runtime.effective_policy else None
    )

# 创建代理
agent = Agent(
    config.agent,
    deps,
    channels,
    config.heartbeat,
    config.hygiene,
    config.routines,
    components.context_manager,
    session_manager
)


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

effect_adapter = EffectBridgeAdapter(
            agent.tools,
            agent.safety,
            agent.hooks,
        ).with_global_auto_approve(agent.config().auto_approve_tools)
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