import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from uuid import UUID

from fastapi import APIRouter, Request, HTTPException, Depends, Header, status
from pydantic import BaseModel
from fastapi.responses import JSONResponse

from channels.channel import Channel, IncomingMessage, MessageStream, OutgoingResponse, StatusUpdate
from config import HttpConfig

logger = logging.getLogger(__name__)


@dataclass
class HttpChannelState:
    user_id: str
    # 发送IncomingMessage
    tx: Optional[asyncio.Queue[IncomingMessage]] = None
    # 等待响应的 future 字典，键为消息 ID
    pending_responses: Dict[str, asyncio.Future] = field(default_factory=dict)
    # Webhook 认证密钥（可选，可通过 SIGHUP 热更新）
    webhook_secret: Optional[str] = None

    # 限流状态
    # rate_limit

    async def update_secret(self, new_secret: Optional[str]):
        """
        在不重启监听器的情况下原地更新 Webhook 密钥。
        在收到 SIGHUP 信号时调用，用于热替换凭证。
        """
        self.webhook_secret = new_secret


# 限制json请求体最大为15MB
MAX_BODY_BYTES = 15 * 1024 * 1024
# 等待响应的最大请求数。
MAX_PENDING_RESPONSES = 100

class AttachmentData(BaseModel):
    # MIME type (e.g. "image/png", "application/pdf").
    mime_type: str
    # 文件路径
    filename: Optional[str] = None
    # base64编码后的数据
    data_base64: Optional[str] = None
    # url链接
    url: Optional[None] = None


class WebhookRequest(BaseModel):
    """
    用于发送方范围路由的可选调用方或客户端标识符。
    通道所有者/存储范围由服务器配置固定。
    """

    # user_id：可选的发送方标识，缺失时为 None
    user_id: Optional[str] = None

    # content：消息内容，必填（无默认值，必须是 str 类型）
    content: str

    # thread_id：可选的线程 ID，缺失时为 None
    thread_id: Optional[str] = None

    # secret：已弃用的认证字段，缺失时为 None
    #    文档说明：应迁移到 X-Hub-Signature-256 头部
    secret: Optional[str] = None

    # wait_for_response：是否等待同步响应，缺失时默认 false
    wait_for_response: bool = False

    # attachments：附件列表，缺失时默认空列表 []
    attachments: List[AttachmentData] = []


class WebhookResponse(BaseModel):
    message_id: str
    status: str
    # 响应内容（仅当 wait_for_response 为 true 时）。
    response: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    channel: str


async def health_handler() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        channel="http"
    )


async def webhook_handler(state: HttpChannelState, request: Request):
    # 限制请求速率

    body = await request.body()
    # Content-Type 检查

    # 获取 webhook 密钥

    # 签名验证（优先 X-Hub-Signature-256）

    # 验证通过后重新解析请求体
    try:
        req = WebhookRequest.model_validate_json(body)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=WebhookResponse(
                message_id=str(UUID(int=0)),
                status="error",
                response=f"Invalid JSON: {e}"
            ).model_dump()
        )

    return await process_authenticated_request(state, req)

async def process_authenticated_request(state: HttpChannelState, req: WebhookRequest):
    # ---------- 1. 规范化 user_id ----------
    raw_user_id = req.user_id  # Optional[str]
    normalized_user_id: Optional[str] = None
    if raw_user_id is not None:
        trimmed = raw_user_id.strip()
        if trimmed != "":
            normalized_user_id = trimmed  # 去除空白后非空

    sender_id = normalized_user_id if normalized_user_id is not None else state.user_id

    wait_for_response = req.wait_for_response

    msg = IncomingMessage(
        channel="http",
        user_id=state.user_id,
        owner_id=state.user_id,
        sender_id=sender_id,
        content=req.content,
        metadata={"wait_for_response": wait_for_response}
    )

    return await process_message(state, msg, wait_for_response)

async def process_message(state: HttpChannelState, msg: IncomingMessage, wait_for_response: bool):
    """
    将已验证的消息推入内部管道，并按需等待同步响应。
    """
    msg_id = msg.id

    # 设置响应通道（如果正在等待）
    response_fut: Optional[asyncio.Future] = None
    if wait_for_response:
        # 检查待处理响应数量是否达到上限
        if len(state.pending_responses) >= MAX_PENDING_RESPONSES:
            return (
                status.HTTP_429_TOO_MANY_REQUESTS,
                WebhookResponse(
                    message_id=msg_id,
                    status="error",
                    response="Too many pending requests",
                ),
            )

        # 创建一个 Future 充当 oneshot 接收端，稍后会由业务处理方设置结果
        fut: asyncio.Future = asyncio.Future()
        state.pending_responses[msg_id] = fut
        response_fut = fut

    # 在持有读锁时克隆发送器，然后在异步发送前释放锁。
    # 这样可以避免在异步 I/O 期间阻塞其他 webhook 处理程序。
    if tx := state.tx:
        try:
            await tx.put(msg)
        except Exception as e:
            return (
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                WebhookResponse(
                    message_id=msg_id,
                    status="error",
                    response="Channel closed",
                ),
            )
    else:
        # 发送器尚未启动，拒绝请求
        return (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            WebhookResponse(
                message_id=msg_id,
                status="error",
                response="Channel not started",
            ),
        )

    # 等待同步响应（如果需要)
    response_content: Optional[str] = None

    if response_fut is not None:
        try:
            # 60 秒超时等待 Future 的结果
            result = await asyncio.wait_for(response_fut, timeout=60.0)
            response_content = result  # 正常获得内容
        except asyncio.TimeoutError:
            response_content = "Response timeout"  # 超时
        except asyncio.CancelledError:
            # Future 被 cancel，对应 Rust 中 oneshot sender 被 drop
            response_content = "Response cancelled"
        finally:
            # 无论成功、超时还是取消，都要清理映射条目
            state.pending_responses.pop(msg_id, None)

    return (
        status.HTTP_200_OK,
        WebhookResponse(
            message_id=msg_id,
            status="accepted",  # 表示已被接受并处理（或等待处理）
            response=response_content,
        ),
    )


@dataclass
class HttpChannel(Channel):
    config: HttpConfig
    state: Optional[HttpChannelState] = None

    def __post_init__(self):
        if self.state is None:
            self.state = HttpChannelState(user_id=self.config.user_id)

    def routes(self) -> APIRouter:
        """
        返回已应用状态的频道 axum 路由
        返回的 Router 共享与后续 start() 方法填充的相同的 Arc<HttpChannelState>
         start() 被调用之前，Webhook 处理程序将返回 503（“频道未启动”）
        """

        router = APIRouter()
        router.add_api_route("/health", health_handler, methods=["GET"])
        router.add_api_route("/webhook", webhook_handler, methods=["POST"])
        return router

    def addr(self):
        return self.config.host, self.config.port

    def name(self) -> str:
        """获取通道名称（例如 "cli"、"slack"、"telegram"、"http"）。"""
        return "http"

    async def start(self) -> MessageStream:
        """开始监听消息。

        返回一个传入消息的流。通道应在内部处理重连和错误恢复。
        """
        # 检查 webhook 密钥是否已配置
        # 创建容量为 256 的异步队列
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        # 将队列的发送端（put 方法）存入状态，供外部注入消息
        self.state.tx = queue
        logger.info(f"HTTP 通道准备就绪 ({self.config.host}:{self.config.port})")

        # 返回队列的异步迭代器
        async def _message_generator():
            while True:
                msg = await queue.get()
                # 可根据需要处理哨兵值结束流
                if msg is None:
                    break
                yield msg

        return _message_generator()

    async def respond(
            self,
            msg: IncomingMessage,
            response: OutgoingResponse,
    ) -> None:
        """将响应发送回用户。

        响应在原始消息的上下文中发送（相同的通道，以及适用时相同的线程）。

        抛出:
            ChannelError: 如果发送失败
        """
        pass

    async def send_status(
            self,
            status: StatusUpdate,
            metadata: Dict[str, Any],
    ) -> None:
        """发送状态更新（思考、工具执行等）。

        metadata 包含通道特定的路由信息（例如 Telegram 的 chat_id），
        用于将状态传递到正确的目的地。

        默认实现不执行任何操作（用于不支持状态的通道）。
        """
        pass

    async def broadcast(
            self,
            user_id: str,
            response: OutgoingResponse,
    ) -> None:
        """发送主动消息，无需事先的传入消息。

        用于警报、心跳通知以及其他代理发起的通信。
        user_id 帮助在通道内定位特定用户。

        默认实现不执行任何操作（用于不支持广播的通道）。
        """
        pass

    async def health_check(self) -> None:
        """检查通道是否健康。

        抛出:
            ChannelError: 如果通道不健康
        """
        pass
