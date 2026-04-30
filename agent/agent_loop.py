import logging
from pydantic import BaseModel, Field, ConfigDict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Dict, Any, Union

from agent.session import Session
from llm import LlmProvider
from db import Database
from workspace import Workspace
from tools import ToolRegistry
import asyncio
from transcription import TranscriptionMiddleware
from document_extraction import DocumentExtractionMiddleware
from channels import ChannelManager, IncomingMessage, OutgoingResponse, AttachmentKind
from agent.submission import SubmissionParser, Submission, Command
from config import AgentConfig
from context import ContextManager
from agent.scheduler import Scheduler
from agent.router import Router
from agent.session_manager import SessionManager

logger = logging.getLogger(__name__)

class AgentDeps(BaseModel):
    owner_id: str
    store: Optional[Database] = None
    llm: LlmProvider
    workspace: Optional[Workspace] = None
    tools: ToolRegistry

    # 用于语音消息的音频转录中间件。
    transcription: Optional[TranscriptionMiddleware] = None
    # # 用于PDF、DOCX、PPTX等文档的文本提取中间件。
    document_extraction: Optional[DocumentExtractionMiddleware] = None


class Agent(BaseModel):
    config: AgentConfig
    deps: AgentDeps
    # 通道: 接受和发送消息
    channels: ChannelManager
    context_manager: ContextManager
    scheduler: Scheduler
    router: Router
    session_manager: SessionManager

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
    def workspace(self) -> Optional[Workspace]:
        """
        获取工作区（如果存在）。
        """
        return self.deps.workspace

    @property
    def tools(self) -> ToolRegistry:
        """
        获取工具注册表。
        """
        return self.deps.tools

    async def store_extracted_documents(self, message: IncomingMessage):
        """
        将提取的文档文本存储在工作区内存中，以便将来搜索/调用。
        """
        pass

    async def handle_message(self, message: IncomingMessage) -> Union[Optional[str]]:
        """
        处理单条消息
        """
        logger.info(f"处理消息: {message.id}")
        # 在调试级别记录敏感详细信息以用于故障排查。
        logging.info(f"消息信息: {message.model_dump(include={'message_id', 'user_id', 'channel', 'thread_id'})}")

        # 内部消息（如任务监控通知）已经是渲染好的文本，应直接转发给用户，而不进入正常的用户输入管道（LLM/工具循环）。
        # is_internal 字段和 into_internal() 设置器均为 私有，因此外部渠道无法伪造此标志。
        if message.is_internal:
            logger.info(f"转发内部消息: {message.model_dump(include={'message_id', 'channel'})}")
            return message.content

        # 为此轮对话设置消息工具上下文（当前渠道和目标）
        # 对于 Signal，使用元数据中的 signal_target（group:ID 或手机号），否则回退使用 user_id
        target = message.routing_target() or message.user_id
        # 为消息工具设置默认频道和目标
        await self.tools.set_message_tool_context(message.channel, target)

        # 解析消息的提交类型
        command: Command = SubmissionParser.parse(message.content)
        submission = command.submission
        logging.info(f"[agent_loop] 解析消息的提交类型: {submission}")

        # 钩子：BeforeInbound — 允许钩子修改或拒绝用户输入
        # if submission == Submission.UserInput:
        #     content = Command.content
        #     # 即将被处理的入站用户消息
        #     event = HookEvent.Inbound(user_id=message.user_id, channel=message.channel,
        #                               content=content, thread_id=message.thread_id)
        #     try:
        #         outcome = await self.hooks.run(event)
        #         if isinstance(outcome, HookOutcome.Continue) and outcome.modified is not None:
        #             submission = Submission.UserInput(content=outcome.modified)
        #     except HookError.Rejected as e:
        #         return f"[消息被拒绝 {e.reason}]"
        #     except HookError as e:
        #         return f"[消息被钩子策略阻止: {e}]"

        # 如果会话线程是历史线程且不在内存中，则从数据库加载
        if external_thread_id := message.conversation_scope():
            logger.info(f"正在从数据库加载会话线程: {message.model_dump(include={'message_id', 'thread_id'})}")
            rejection = await self.maybe_hydrate_thread(message, external_thread_id)
            if rejection:
                return f"Error: {rejection}"

        # 解析会话和线程
        logger.info(f"正在解析会话和线程: {message.model_dump(include={'message_id'})}")
        session, thread_id = await self.session_manager.resolve_thread(message.user_id, message.channel,
                                                                       message.conversation_scope())
        logger.info(f"解析会话和线程成功: {message.model_dump(include={'message_id'})}")

        # 它检查当前对话线程是否正处于 等待用户提供认证令牌 的状态（例如配置 MCP 扩展时需要用户输入 token）。
        # 如果有，无论用户发送了什么消息，都会被直接路由到认证流程或取消，而不会进入正常的聊天记录、工具调用等逻辑。


        logger.info(f"收到来自 {message.user_id} 在 {message.channel} 上的消息（{len(message.content)} 个字符）")
        # 根据submission的类型进行处理
        if submission == Submission.UserInput:
            result = await self.process_user_input(message, session, thread_id, submission.content)
        else:
            logger.error(f"暂不支持的指令")

    async def process_user_input(self,
                                 message: IncomingMessage,
                                 session: Session,
                                 thread_id: str,
                                 content: str):
        logger.info(f"处理用户输入: message_id: {message.id}")

        # 首先检查线程状态，在 I/O 操作期间不持有锁。

        # 用户输入的安全验证。

        # 扫描入站消息中的密钥（API 密钥、令牌）。
        # 在此处捕获它们可以防止大语言模型将其回显，
        # 否则会触发外发泄漏检测器并造成错误循环。

        # 直接处理以 / 开头的显式命令
        # 其余所有内容都通过正常的智能体循环（带工具）处理
        temp_message = message
        temp_message.content = content

        if intent := self.router.route_command(temp_message):
            # 直接处理以 / 开头的显式命令
            return await self.handle_job_or_command(intent, message)

        # 自然语言将通过智能体循环处理
        # 作业工具（create_job、list_jobs 等）位于工具注册表中
        #
        # 在添加新轮次之前，如果需要则自动压缩会话




    async def run(self):
        """
        运行Agent主循环
        """

        # 启动消息接受通道。返回的是asyncio.Queue()
        message_stream = await self.channels.start_all()

        # 6. 主消息循环
        logger.info(f"Agent {self.config.name} ready and listening")

        while True:

            # TODO 创建接受ctrl+c的异步任务
            wait_shutdown_task = None
            # 创建消息读取的任务。
            get_msg_task = asyncio.create_task(message_stream.get())

            # 同时等待，任意一个完成即返回（对应 tokio::select!）
            done, pending = await asyncio.wait(
                [get_msg_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # 如果是关闭任务，则退出循环

            # 否则获取message
            message = get_msg_task.result()
            if message is None:
                logger.info("所有通道流已结束，正在关闭...")
                break

            # 将转录中间件应用于音频附件
            if self.deps.transcription:
                await self.deps.transcription.process(message)

            # 应用文档提取中间件
            if self.deps.document_extraction:
                await self.deps.document_extraction.process(message)

            # 存储提取的文档
            await self.store_extracted_documents(message)

            # 判断是否为内部消息(如心跳任务等)，避免重复处理
            if (
                    not message.is_internal  # 非内部消息
                    # and self._is_user_input(message.content)  # 是用户输入
                    # and routine_engine_for_loop is not None  # let Some(ref engine)
            ):
                continue

            # 处理消息
            response, error = await self.handle_message(message)




