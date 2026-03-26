import logging

from agent.context_monitor import ContextMonitor
from agent.heartbeat import spawn_heartbeat
from agent.routine_engine import RoutineEngine, spawn_cron_ticker
from agent.self_repair import DefaultSelfRepair, RepairResult, SelfRepair
from agent.session_manager import SessionManager
from agent.submission import Submission, SubmissionParser, SubmissionResult
from agent import Router, Scheduler, HeartbeatConfig as AgentHeartbeatConfig
from channels import ChannelManager, IncomingMessage, OutgoingResponse, AttachmentKind
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
from typing import Optional, Union
import asyncio
from datetime import datetime, timezone


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

    def set_routine_engine_slot(self, slot: Optional[RoutineEngineSlot]):
        """设置例行引擎槽位，用于将引擎暴露给网关。"""
        self.routine_engine_slot = slot

    @property
    def scheduler(self) -> Scheduler:
        """
        获取调度器（用于外部连接，例如 CreateJobTool 等）。
        """
        return self.scheduler

    @property
    def store(self) -> Optional[Database]:
        """
        获取数据库存储（如果存在）。
        """
        return self.deps.store

    @property
    def llm(self) -> LlmProvider:
        """
        获取主 LLM 提供者。
        """
        return self.deps.llm

    @property
    def cheap_llm(self) -> LlmProvider:
        """
        获取便宜/快速的 LLM 提供者，如果未设置则回退到主 LLM 提供者。
        """
        return self.deps.cheap_llm

    @property
    def safety(self) -> SafetyLayer:
        """
        获取安全层。
        """
        return self.deps.safety

    @property
    def tools(self) -> ToolRegistry:
        """
        获取工具注册表。
        """
        return self.deps.tools

    @property
    def workspace(self) -> Optional[Workspace]:
        """
        获取工作区（如果存在）。
        """
        return self.deps.workspace

    @property
    def hooks(self) -> HookRegistry:
        """
        获取钩子注册表。
        """
        return self.deps.hooks

    # crate::agent::cost_guard::CostGuard
    @property
    def cost_guard(self) -> CostGuard:
        """
        获取成本守卫。
        """
        return self.deps.cost_guard

    @property
    def skill_registry(self) -> Optional[SkillRegistry]:
        """
        获取技能注册表（如果存在）。
        """
        return self.deps.skill_registry

    # crate::skills::catalog::SkillCatalog
    @property
    def skill_catalog(self) -> Optional[SkillCatalog]:
        """
        获取技能目录（如果存在）。
        """
        return self.deps.skill_catalog

    # crate::skills::LoadedSkill
    def select_active_skills(self, message_content: str) -> LoadedSkill:
        """
        使用确定性预过滤为消息选择活动技能。
        """
        registry = self.skill_registry()
        if registry:
            return []

        # 获取读锁，处理可能的异常（Python 中锁一般不会中毒，但为模拟 Rust 的错误处理）
        try:
            guard = registry.read()
            available = guard.skills()
            skills_cfg = self.deps.skills_config

            # crate::skills::prefilter_skills
            #  调用预过滤函数
            selected = prefilter_skills(
                message_content,
                available,
                skills_cfg.max_active_skills,
                skills_cfg.max_context_tokens
            )
        except Exception as e:
            logging.error(f"技能注册表锁错误: {e}")
            return []

        if selected:
            skill_names = [skill.name() for skill in selected]
            logging.debug(
                f"为消息选择了 {len(selected)} 个技能: {', '.join(skill_names)}"
            )

        return selected

    async def _self_repair_task(self, repair, repair_interval, repair_channels, repair_owner_id):
        """
        修复卡在的job和损坏工具的进程
        """
        while True:
            await asyncio.sleep(repair_interval)

            # 检测卡住的作业
            stuck_jobs = await repair.detect_stuck_jobs()
            for job in stuck_jobs:
                logging.info(f"尝试修复卡住的作业 {job.job_id}")
                result = await repair.repair_stuck_job(job)
                notification = None

                if isinstance(result, RepairResult.Success):
                    logging.info(f"修复成功: {result.message}")
                    notification = f"Job {job.job_id} was stuck for {job.stuck_duration}s, recovery succeeded: {result.message}"
                elif isinstance(result, RepairResult.Failed):
                    logging.error(f"修复失败: {result.message}")
                    notification = f"Job {job.job_id} was stuck for {job.stuck_duration}s, recovery failed permanently: {result.message}"
                elif isinstance(result, RepairResult.ManualRequired):
                    logging.warning(f"需要人工干预: {result.message}")
                    notification = f"Job {job.job_id} 需要人工干预: {result.message}"
                elif isinstance(result, RepairResult.Retry):
                    logging.warning(f"需要重新尝试修复: {result.message}")
                else:
                    # 修复报错
                    logging.error("修复时报错: ")

                if notification:
                    response = OutgoingResponse.text(f"自修复: {notification}")
                    await repair_channels.broadcast_all(repair_owner_id, response)

            # 检测损坏的工具
            broken_tools = await repair.detect_broken_tools()
            for tool in broken_tools:
                logging.info(f"尝试修复损坏的工具: {tool.name}")
                result = await repair.repair_broken_tool(tool)
                if isinstance(result, RepairResult.Success):
                    response = OutgoingResponse.text(f"自修复: Tool '{tool.name}' 修复结果: {result.message}")
                    await repair_channels.broadcast_all(repair_owner_id, response)
                elif result.is_ok:  # TODO 实现result
                    logging.info(f"工具修复结果: {result}")
                else:
                    logging.error("工具修复时报错: ")

    async def _session_pruning_task(self, session_mgr, session_idle_timeout):
        """
        会话清理进程
        """
        while True:
            await asyncio.sleep(600)  # 每10分钟
            await session_mgr.prune_stale_sessions(session_idle_timeout)

    async def _notify_task(self, notify_queue, channel, notify_target, notify_user):
        """
        消息通知任务
        """
        while True:
            response = await notify_queue.get()
            # 首先尝试定向发送，如果失败则回退到使用所有频道广播。
            targeted_ok = False
            if channel and notify_target:
                targeted_ok = await channels.broadcast(channel, notify_target, response).is_ok()

            if not targeted_ok and notify_user:
                results = await channels.broadcast_all(notify_user, response)
                for (ch, result) in results:
                    if result == "erro":
                        logging.warning(f"未能向目标主机广播心跳信号 {ch}: {result}")

    async def _routine_task(self, notify_queue, channel, extension_manager):
        while True:
            response = await notify_queue.get()
            # 从元数据获取通知通道和用户
            notify_channel = response.metadata.get("notify_channel") if response.metadata else None
            fallback_user = await resolve_owner_scope_notification_user(
                response.metadata.get("notify_user") if response.metadata else None,
                response.metadata.get("owner_id") if response.metadata else None,
            )
            # 解析通知目标
            user = await resolve_routine_notification_target(
                extension_manager, response.metadata or {}
            )
            if not user:
                logging.warning("跳过无明确目标或所有者范围的例行通知")
                continue
            # 尝试定向发送
            targeted_ok = False
            if channel:
                targeted_ok = await channels.broadcast(channel, user, response).is_ok()
                if targeted_ok == "erro":
                    should_fallback = should_fallback_routine_notification(targeted_ok)
                    logging.warning(
                        f"未能向配置的通道发送例行通知: {e}, fallback={should_fallback}")
                    if not should_fallback:
                        continue

            if not targeted_ok and fallback_user:
                results = await channels.broadcast_all(user, response)
                for (ch, result) in results:
                    if result == "erro":
                        logging.warning(f"未能向目标主机广播心跳信号 {ch}: {result}")

    async def run(self):
        """
        运行agent主循环
        """
        # 1. 启动通道，获取消息流
        message_stream = await self.channels.start_all()

        # 2. 启动自修复任务
        repair = DefaultSelfRepair(
            self.context_manager,
            self.config.stuck_threshold,
            self.config.max_repair_attempts
        )
        repair_interval = self.config.repair_check_interval
        repair_channels = self.channels
        repair_owner_id = self.owner_id  # .to_string()

        # tokio::spawn(async move {loop {}}) tokio::spawn: 创建一个异步任务，loop: 无限循环函数
        repair_handle = asyncio.create_task(
            self._self_repair_task(repair, repair_interval, repair_channels, repair_owner_id))

        # 3. 启动会话清理任务
        session_mgr = self.session_manager
        session_idle_timeout = self.config.session_idle_timeout
        pruning_handle = asyncio.create_task(self._session_pruning_task(session_mgr, session_idle_timeout))

        # 4. 启动心跳任务（如果启用）
        hb_config = self.heartbeat_config
        heartbeat_task = None
        if hb_config and hb_config.enabled:
            workspace = self.workspace()
            if workspace:
                config = AgentHeartbeatConfig.default().with_interval(hb_config.interval_secs)
                config.quiet_hours_start = hb_config.quiet_hours_start
                config.quiet_hours_end = hb_config.quiet_hours_end
                config.timezone = hb_config.timezone or self.config.default_timezone

                # 解析通知用户和通道
                heartbeat_notify_user = resolve_owner_scope_notification_user(
                    hb_config.notify_user, self.owner_id
                )

                if (channel := hb_config.notify_channel) and (user := heartbeat_notify_user):
                    config = config.with_notify(user, channel)

                # 创建通知通道（用于发送心跳通知）
                # 创建队列，maxsize=16 相当于 Rust 的缓冲区大小
                notify_queue: asyncio.Queue[OutgoingResponse] = asyncio.Queue(maxsize=16)
                # 启动通知转发器
                notify_channel = hb_config.notify_channel
                notify_target = await resolve_channel_notification_user(
                    self.deps.extension_manager, notify_channel, hb_config.notify_user, self.owner_id
                )
                notify_user = heartbeat_notify_user
                channels = self.channels
                asyncio.create_task(self._notify_task(notify_queue, channels, notify_target, notify_user))

                hygiene = self.hygiene_config.to_workspace_config() if self.hygiene_config else {}  # TODO 默认值的实现
                heartbeat_task = spawn_heartbeat(
                    config,
                    hygiene,
                    workspace,
                    self.cheap_llm(),
                    notify_queue,
                    self.store(),
                )
            else:
                logging.warning("已启用心跳功能，但没有可用工作区。")

        # 5. 启动例行引擎（如果启用）
        routine_engine_for_loop = None
        cron_task = None
        if self.routine_config and self.routine_config.enabled:
            if (store := self.store()) and (workspace := self.workspace()):
                #  设置通知通道（模式与心跳相同）
                notify_queue = asyncio.Queue(maxsize=32)
                engine = RoutineEngine(
                    self.routine_config,
                    store,
                    self.llm(),
                    workspace,
                    notify_queue,
                    self.scheduler,
                    self.tools(),
                    self.safety(),
                )

                # 注册例行工具
                self.deps.tools.register_routine_tools(store, engine)
                # 加载初始事件缓存
                await engine.refresh_event_cache()
                # 启动通知转发器（类似心跳模式）
                channels = self.channels
                extension_manager = self.deps.extension_manager
                asyncio.create_task(self._routine_task(notify_queue, channel, extension_manager))

                # 启动 cron ticker
                cron_interval = self.routine_config.cron_check_interval_secs
                cron_task = spawn_cron_ticker(engine, cron_interval)

                # 存储引擎引用以进行事件触发检查
                # 安全性：我们位于 run() 函数中，该函数接受自身作为参数，不存在其他引用。
                # 安全性：self 会被 run() 消耗，我们可以通过 local 将引擎偷偷带入下面的消息循环中使用。
                # 将引擎暴露给网关（如果有槽位）
                if self.routine_engine_slot:
                    slot = await self.routine_engine_slot.set(engine)  # TODO 跟Rust的实现不一致
                logging.debug(
                    f"Routines enabled: cron ticker every {cron_interval}s, max {self.routine_config.max_concurrent_routines} concurrent"
                )
                routine_engine_for_loop = engine
            else:
                logging.warning("例程已启用，但存储/工作区不可用")

        # 提取引擎引用以用于消息循环

        # 6. 主消息循环
        logging.debug(f"Agent {self.config.name} ready and listening")

        try:
            async for message in message_stream:
                # 处理中断信号（Ctrl+C） - Python 中可通过信号处理器停止循环，这里简单在循环外处理
                # 由于 async for 无法直接监听信号，我们可以在任务中检查取消状态，或者使用 asyncio.ensure_future 包装

                # 将转录中间件应用于音频附件
                if self.deps.transcription:
                    await self.deps.transcription.process(message)

                # 应用文档提取中间件
                if self.deps.document_extraction:
                    await self.deps.document_extraction.process(message)

                # 存储提取的文档
                await self.store_extracted_documents(message)

                # 事件触发例程会在用户输入进入正常的聊天/工具流程之前对其进行处理。
                # 这避免了主代理响应后，例程又对同一条入站消息触发的重复操作。
                if (not message.is_internal
                        and SubmissionParser.parse(message.content) == Submission.UserInput
                        and routine_engine_for_loop):
                    fired = await routine_engine_for_loop.check_event_triggers(message)
                    if fired > 0:
                        logging.debug(
                            f"Consumed inbound user message with matching event-triggered routine(s): "
                            f"channel={message.channel}, user={message.user_id}, fired={fired}"
                        )
                        continue  # 跳过正常处理

                # 处理消息
                response, error = await self.handle_message(message)
                if response:
                    # 钩子：BeforeOutbound
                    event = HookEvent.Outbound(user_id=message.user_id,
                                               channel=message.channel,
                                               content=response,
                                               thread_id=message.thread_id)

                    outcome = await self.hooks().run(event)
                    if outcome == "erro":
                        logging.warning(f"BeforeOutbound 钩子阻塞了响应：{outcome}")
                    elif isinstance(outcome, HookOutcome.Continue):
                        pass



        except:
            pass

    async def store_extracted_documents(self, message: IncomingMessage):
        """
        将提取的文档文本存储在工作区内存中，以便将来搜索/调用。
        :param message:
        :return:
        """
        if not self.workspace():
            return

        workspace = self.workspace()

        for attachment in message.attachments:
            if attachment.kind != AttachmentKind.Document:
                continue

            text = attachment.extracted_text

            if not text or text.startswith('['):
                # 跳过报错信息，如: "[Failed to..."
                continue

            # 清理文件名：去除路径分隔符以防止目录遍历
            raw_name = attachment.filename if attachment.filename else "unnamed_document"
            # 字符是 /、\ 或 \0，则替换为 _
            trans_table = str.maketrans({'/': '_', '\\': '_', '\0': '_'})
            filename = raw_name.translate(trans_table)
            # 删除开头的.
            filename = filename.lstrip('.')
            filename = filename if filename else "unnamed_document"

            # timezone.utc  时区
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            path = f"documents/{date}/{filename}"

            header = f"# {filename}\n\n> 由 **{message.user_id}** 通过 **{message.channel}** 于 {date} 上传\n> MIME: {attachment.mime_type} | Size: {attachment.size_bytes.unwrap_or(0)} bytes\n\n---\n\n"
            content = f"{header}{text}"

            try:
                await workspace.write(path, content)
                logging.info(f"已将提取的文档存储在工作区内存中: path={path}, text_len={len(text)}")

            except Exception as e:
                logging.warning(f"将提取的文档存储在工作区内存时失败")

    async def handle_message(self, message: IncomingMessage) -> Union[Optional[str], Error]:
        # 仅在信息级别记录日志以用于跟踪，同时避免暴露个人身份信息（用户ID可能是手机号）
        logging.info(f"Processing message: {message.id}")
        # 在调试级别记录敏感详细信息以用于故障排查。
        logging.debug(f"Message details: {message.model_dump(include={'message_id', 'user_id', 'channel', 'thread_id'})}")

        # 内部消息（如任务监控通知）已经是渲染好的文本，应直接转发给用户，而不进入正常的用户输入管道（LLM/工具循环）。
        # is_internal 字段和 into_internal() 设置器均为 私有，因此外部渠道无法伪造此标志。
        if message._is_internal:
            logging.debug(f"转发内部消息: {message.model_dump(include={'message_id', 'channel'})}")
            return message.content

        # 为此轮对话设置消息工具上下文（当前渠道和目标）
        # 对于 Signal，使用元数据中的 signal_target（group:ID 或手机号），否则回退使用 user_id
        target = message.routing_target() or message.user_id
        await self.tools.set_message_tool_context(message.channel, target)

        # 首先解析提交类型
        submission = SubmissionParser.parse(message.content)
        logging.debug(f"[agent_loop] Parsed submission: {type(submission).__name__}")
        # 钩子：BeforeInbound — 允许钩子修改或拒绝用户输入

        if isinstance(submission, Submission.UserInput):
            content = submission.content
            event = HookEvent.Inbound(user_id=message.user_id, channel=message.channel,
                                      content=content, thread_id=message.thread_id)
            try:
                outcome = await self.hooks.run(event)
                if isinstance(outcome, HookOutcome.Continue) and outcome.modified is not None:
                    submission = UserInput(content=outcome.modified)
            except HookError.Rejected as e:
                return f"[消息被拒绝 {e.reason}]"
            except HookError as e:
                return f"[消息被钩子策略阻止: {e}]"
            # 继续执行，故障开放模式下的错误已在注册表中记录

        # 如果会话线程是历史线程且不在内存中，则从数据库加载
        if external_thread_id := message.conversation_scope():
            logging.debug(f"正在从数据库加载会话线程: {message.model_dump(include={'message_id', 'thread_id'})}")
            rejection = await self.maybe_hydrate_thread(message, external_thread_id)
            if rejection:
                return f"Error: {rejection}"

        # 解析会话和线程
        logging.debug(f"正在解析会话和线程: {message.model_dump(include={'message_id'})}")
        session, thread_id = await self.session_manager.resolve_thread(message.user_id, message.channel, message.conversation_scope())
        logging.debug(f"解析会话和线程成功: {message.model_dump(include={'message_id'})}")

        # 认证模式拦截：如果会话线程正在等待令牌，则将消息直接路由到凭证存储
        # 不会触及日志、对话轮次、历史记录或压缩
        sess = await session.lock()  # 获取锁
        thread = sess.threads.get(thread_id)
        pending_auth = thread.pending_auth if thread is not None else None
        if pending_auth:
            if pending_auth.is_expired():
                # TTL 已超时 — 清除过期的认证模式
                logging.warning(f"认证模式在 TTL 后已过期，正在清除: extension: {pending_auth.extension_name}")
                sess = await session.lock()
                if thread := sess.threads.get_mut(thread_id):
                    thread.pending_auth = None
                # 如果这是一条用户消息（可能是粘贴的令牌）返回显式错误，而不是将其转发给 LLM/历史记录。
                if isinstance(submission, Submission.UserInput):
                    return f"{pending_auth.extension_name} 的身份验证已过期，请重试"
        # 控制类提交（中断、撤销等）会回退到常规处理流程
        elif isinstance(submission, Submission.UserInput):
            return await self.process_auth_token(message, pending_auth, submission.content, session, thread_id)
        # 任何控制提交（中断、撤销等）都会取消认证模式
        else:
            sess = await session.lock()
            if thread := sess.threads.get_mut(thread_id):
                thread.pending_auth = None
            # 回退到常规处理流程

        logging.debug(f"收到来自 {message.user_id} 在 {message.channel} 上的消息（{len(message.content)} 个字符）")

        # 根据submission的类型进行处理
        result = None
        if isinstance(submission, Submission.UserInput):
            result = await self.process_user_input(message, session, thread_id, submission.content)
        elif isinstance(submission, Submission.SystemCommand):
            logging.debug(f"[agent_loop] SystemCommand: command={submission.command}, channel={message.channel}")
            # 授权检查（包括重启渠道检查）在 handle_system_command 中强制执行
            result = await self.handle_system_command(submission.command, submission.args, message.channel)
        elif isinstance(submission, Submission.Undo):
            result = await self.process_undo(session, thread_id)
        elif isinstance(submission, Submission.Redo):
            result = await self.process_redo(session, thread_id)
        elif isinstance(submission, Submission.Interrupt):
            result = await self.process_interrupt(session, thread_id)
        elif isinstance(submission, Submission.Compact):
            result = await self.process_compact(session, thread_id)
        elif isinstance(submission, Submission.Clear):
            result = await self.process_clear(session, thread_id)
        elif isinstance(submission, Submission.NewThread):
            result = await self.process_new_thread(message)
        elif isinstance(submission, Submission.Heartbeat):
            result = await self.process_heartbeat()
        elif isinstance(submission, Submission.Summarize):
            result = await self.process_summarize(session, thread_id)
        elif isinstance(submission, Submission.Suggest):
            result = await self.process_suggest(session, thread_id)
        elif isinstance(submission, Submission.JobStatus):
            result = await self.process_job_status(message.user_id, submission.job_id)
        elif isinstance(submission, Submission.JobCancel):
            result = await self.process_job_cancel(message.user_id, submission.job_id)
        elif isinstance(submission, Submission.Quit):
            return None
        elif isinstance(submission, Submission.SwitchThread):
            result = await self.process_switch_thread(message, target)
        elif isinstance(submission, Submission.Resume):
            result = await self.process_resume(session, thread_id, submission.checkpoint_id)
        elif isinstance(submission, Submission.ExecApproval):
            result = await self.process_approval(message, session, thread_id, submission.request_id, submission.approved, submission.always)
        elif isinstance(submission, Submission.ApprovalResponse):
            result = await self.process_approval(message, session, thread_id, None, submission.approved, submission.always)

        # 将 SubmissionResult 转换为响应字符串
        if isinstance(result, SubmissionResult.Response):
            # 抑制静默回复（例如来自群聊的“无话可说”响应）
            # llm.is_silent_reply
            if is_silent_reply(result.content):
                logging.debug("抑制静默回复")
                return None
            return result.content
        if isinstance(result, SubmissionResult.Ok):
            return result.message
        if isinstance(result, SubmissionResult.Error):
            return f"Error: {result.message}"
        if isinstance(result, SubmissionResult.Interrupted):
            return "Interrupted."  # 已中断
        if isinstance(result, SubmissionResult.NeedApproval):
            # ApprovalNeeded 状态已在 thread_ops.rs 中返回此结果之前发送。
            # 空字符串表示调用方跳过 respond()（避免重复发送文本）
            return ""
