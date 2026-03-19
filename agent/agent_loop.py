from agent.context_monitor import ContextMonitor
from agent.heartbeat import spawn_heartbeat
from agent.routine_engine import RoutineEngine, spawn_cron_ticker
from agent.self_repair import DefaultSelfRepair, RepairResult, SelfRepair
from agent.session_manager import SessionManager
from agent.submission import Submission, SubmissionParser, SubmissionResult
from agent import HeartbeatConfig as AgentHeartbeatConfig, Router, Scheduler
from channels import ChannelManager, IncomingMessage, OutgoingResponse
from config import AgentConfig, HeartbeatConfig, RoutineConfig, SkillsConfig
from context import ContextManager
from db import Database
from error import ChannelError, Error
from extensions import ExtensionManager
from hooks import HookRegistry
from llm import LlmProvider
from safety import SafetyLayer
from skills import SkillRegistry
from tools import ToolRegistry
from workspace import Workspace
from dataclasses import dataclass
from typing import Optional


# 代理的核心依赖。
# 将共享组件捆绑在一起以减少参数数量。

@dataclass
class AgentDeps:
    # 实例的已解析持久所有者作用域。
    owner_id: str
    store: Optional[Database]
    llm: LlmProvider
    # 用于轻量级任务的廉价/快速LLM（心跳、路由、评估）。
    # 如果为None，则回退到主`llm`。
    cheap_llm: Optional[LlmProvider]
    safety: SafetyLayer
    tools: ToolRegistry
    workspace: Optional[Workspace]
    extension_manager: Optional[ExtensionManager]
    skill_registry: Optional[SkillRegistry]  # Option<Arc<std::sync::RwLock<SkillRegistry>>>,
    skill_catalog: Optional[SkillCatalog]  # Option<Arc<crate::skills::catalog::SkillCatalog>>,
    skills_config: SkillsConfig
    hooks: HookRegistry
    # 成本执行护栏（每日预算、每小时速率限制）。
    cost_guard: CostGuard  # Arc<crate::agent::cost_guard::CostGuard>,
    # 用于向Web网关实时流式传输作业事件的SSE广播发送器。
    sse_tx: Optional[SseEvent]  # Option<tokio::sync::broadcast::Sender<crate::channels::web::types::SseEvent>>,
    # 用于跟踪记录/重放的HTTP拦截器。
    http_interceptor: Optional[HttpInterceptor]  # Option<Arc<dyn crate::llm::recording::HttpInterceptor>>,
    # 用于语音消息的音频转录中间件。
    transcription: Optional[TranscriptionMiddleware]  # Option<Arc<crate::transcription::TranscriptionMiddleware>>,
    # 用于PDF、DOCX、PPTX等文档的文本提取中间件。
    document_extraction: Optional[
        DocumentExtractionMiddleware]  # Option<Arc<crate::document_extraction::DocumentExtractionMiddleware>>,


class Agent:
    """
    协调所有组件的主代理
    """

    def __init__(
            self,
            config: AgentConfig,
            deps: AgentDeps,
            channels: ChannelManager,
            context_manager: ContextManager,
            scheduler: Scheduler,
            router: Router,
            session_manager: SessionManager,
            context_monitor: ContextMonitor,
            heartbeat_config: Optional[HeartbeatConfig],
            # Option<crate::config::HygieneConfig>,
            hygiene_config: Optional[HygieneConfig],
            routine_config: Optional[RoutineConfig],
            # 用于将例程引擎暴露给网关以进行手动触发的可选槽位
            # Option<Arc<tokio::sync::RwLock<Option<Arc<crate::agent::routine_engine::RoutineEngine>>>>>
            routine_engine_slot: Optional[RoutineEngine]
    ):

        self.config = config
        self.deps = deps
        self.channels = channels
        self.context_manager = context_manager
        self.scheduler = scheduler
        self.router = router
        self.session_manager = session_manager
        self.context_monitor = context_monitor
        self.heartbeat_config = heartbeat_config
        self.hygiene_config = hygiene_config
        self.routine_config = routine_config
        self.routine_engine_slot = routine_engine_slot

    @property
    def owner_id(self):
        if self.deps.workspace is not None:
            # 调试断言：确保工作空间的用户ID与所有者ID一致
            assert self.deps.workspace.user_id() == self.deps.owner_id, \
                "workspace.user_id() 必须与 deps.owner_id 一致"

        return self.deps.owner_id

    @classmethod
    def new(
            cls,
            config: AgentConfig,
            deps: AgentDeps,
            channels: ChannelManager,
            heartbeat_config: Optional[HeartbeatConfig],
            hygiene_config: Optional[HygieneConfig],
            routine_config: Optional[RoutineConfig],
            context_manager: ContextManager,
            session_manager: SessionManager,
    ):
        """
        创建新的代理
        可选择接收预先创建的ContextManager和SessionManager，用于与外部组件（任务工具、Web网关）共享；未提供时则创建新的实例
        """
        # 处理可选的上下文管理器：若未提供则新建一个
        context_manager = context_manager or ContextManager(config.max_parallel_jobs)
        scheduler = Scheduler(
            config.clone(),
            context_manager.clone(),
            deps.llm.clone(),
            deps.safety.clone(),
            deps.tools.clone(),
            deps.store.clone(),
            deps.hooks.clone(),
        )
        # 如果依赖中有 SSE 发送器，则设置到调度器中
        if deps.sse_tx is not None:
            scheduler.set_sse_sender(deps.sse_tx)

        # 如果依赖中有 HTTP 拦截器，则设置到调度器中
        if deps.http_interceptor is not None:
            scheduler.set_http_interceptor(deps.http_interceptor)

        router = Router()
        context_monitor = ContextMonitor()
        agent = cls(
            config,
            deps,
            channels,
            context_manager,
            scheduler,
            router,
            session_manager,
            context_monitor,
            heartbeat_config,
            hygiene_config,
            routine_config,
            routine_engine_slot=None
        )
        return agent
