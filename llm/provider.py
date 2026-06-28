# ! LLM 提供商 trait 和类型。

from __future__ import annotations
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
import json
import logging

# 日志记录器
logger = logging.getLogger(__name__)


# 假设 Decimal 已从某处导入
# from decimal import Decimal
# 或使用第三方库: from rust_decimal import Decimal


class Role(str, Enum):
    """
    对话中的角色。
    """
    System = "system"
    User = "user"
    Assistant = "assistant"
    Tool = "tool"


class FinishReason(str, Enum):
    """完成原因。"""
    Stop = "stop"
    Length = "length"
    ToolUse = "tool_use"
    ContentFilter = "content_filter"
    Unknown = "unknown"


class UnsupportedParam(str, Enum):
    """
    表示可能并非所有 LLM 提供商都支持的请求参数。

    此类型化枚举替换代码库中各处字符串类型的参数名称，
    为参数处理提供类型安全和单点维护。
    """
    TEMPERATURE = "temperature"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCES = "stop_sequences"


@dataclass
class ContentPart:
    """
    多模态消息内容的一部分（OpenAI Chat Completions 格式）。
    """
    pass


@dataclass
class TextPart(ContentPart):
    """文本内容部分。"""
    type: str = "text"
    text: str = ""


@dataclass
class ImageUrlPart(ContentPart):
    """图片 URL 内容部分（支持 data: URLs 用于内联 base64 图片）。"""
    type: str = "image_url"
    image_url: "ImageUrl" = None


@dataclass
class ImageUrl:
    """
    多模态内容的图片 URL 引用。
    """
    url: str
    detail: Optional[str] = None

    def normalized_openai_detail(self) -> str:
        """
        规范化 OpenAI 图像细节提示，默认缺失或无效值返回 "auto"。
        """
        return normalize_openai_image_detail(self.detail)

    def decode_data_url(self) -> Optional[Tuple[str, str]]:
        """
        将内联 base64 data: URL 解码为 (media_type, base64_data) 部分。
        """
        if not self.url.startswith("data:"):
            return None
        rest = self.url[5:]
        if ";base64," not in rest:
            return None
        media_type, data = rest.split(";base64,", 1)
        return media_type, data


def normalize_openai_image_detail(detail: Optional[str]) -> str:
    """
    规范化 OpenAI 图像细节提示，缺失时默认为 "auto"。
    """
    if detail is not None:
        detail = detail.strip().lower()
        if detail and detail in ("auto", "low", "high"):
            return detail
    return "auto"


@dataclass
class ChatMessage:
    """
    对话中的一条消息。
    """
    role: Role
    content: str = ""

    # 多模态内容部分（图片等）。
    # 当非空时，提供商会将内容序列化为部件数组（将 content 作为文本部件包含），而不是纯字符串。
    content_parts: List[ContentPart] = field(default_factory=list)

    # 工具调用 ID，如果这是工具结果消息。
    tool_call_id: Optional[str] = None

    # 工具结果的工具名称。
    name: Optional[str] = None

    # 助手发出的工具调用（OpenAI 协议要求这些出现在工具结果消息之前的助手消息上）。
    tool_calls: Optional[List[ToolCall]] = None

    # 提供商发出的推理产物（DeepSeek 的 reasoning_content、Gemini 的 thought_signature 部件、
    # OpenRouter 的 reasoning_details），从前一个响应中捕获。需要在下一次请求时原样回传 ——
    # 当先前的助手消息包含推理内容而被丢弃时，DeepSeek 思考模式和 Gemini 2.5+
    # 都会以 HTTP 400 拒绝下一回合（#3201, #3225）。
    reasoning: Optional[str] = None

    @classmethod
    def system(cls, content: str) -> "ChatMessage":
        """创建系统消息。"""
        return cls(role=Role.System, content=content)

    @classmethod
    def user(cls, content: str) -> "ChatMessage":
        """创建用户消息。"""
        return cls(role=Role.User, content=content)

    @classmethod
    def user_with_parts(cls, content: str, parts: List[ContentPart]) -> "ChatMessage":
        """
        创建带有多个内容部分（如图片）的用户消息。

        文本 content 作为主要文本与各部分一起包含。
        """
        return cls(role=Role.User, content=content, content_parts=parts)

    @classmethod
    def assistant(cls, content: str) -> "ChatMessage":
        """创建助手消息。"""
        return cls(role=Role.Assistant, content=content)

    @classmethod
    def assistant_with_tool_calls(cls, content: Optional[str], tool_calls: List[ToolCall]) -> "ChatMessage":
        """
        创建包含工具调用的助手消息。

        根据 OpenAI 协议，包含 tool_calls 的助手消息必须在对话中位于相应的工具结果消息之前。
        """
        return cls(
            role=Role.Assistant,
            content=content or "",
            tool_calls=tool_calls if tool_calls else None,
        )

    def with_reasoning(self, reasoning: Optional[str]) -> "ChatMessage":
        """
        将提供商发出的推理产物附加到助手消息。

        对于 DeepSeek (reasoning_content)、Gemini 2.5+ (thought_signature) 和
        OpenRouter (reasoning_details) 的思考模式工具调用是必需的。
        当先前的助手消息包含推理内容而未被回传时，提供商会以 HTTP 400 拒绝下一回合。
        参见 #3201, #3225。

        空/仅空白字符的推理会被丢弃（视为 None），
        以避免发送 reasoning_content: "" 触发严格模式验证器。
        """
        if reasoning and reasoning.strip():
            self.reasoning = reasoning
        return self

    @classmethod
    def tool_result(cls, tool_call_id: str, name: str, content: str) -> "ChatMessage":
        """创建工具结果消息。"""
        return cls(
            role=Role.TOOL,
            content=content,
            tool_call_id=tool_call_id,
            name=name,
        )


@dataclass
class CompletionRequest:
    """聊天完成请求。"""
    messages: List[ChatMessage]
    model: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stop_sequences: Optional[List[str]] = None
    metadata: Dict[str, str] = field(default_factory=dict)

    def with_model(self, model: str) -> "CompletionRequest":
        """设置模型覆盖。"""
        self.model = model
        return self

    def with_max_tokens(self, max_tokens: int) -> "CompletionRequest":
        """设置最大令牌数。"""
        self.max_tokens = max_tokens
        return self

    def with_temperature(self, temperature: float) -> "CompletionRequest":
        """设置温度。"""
        self.temperature = temperature
        return self

    def take_model_override(self) -> Optional[str]:
        """
        取出按请求的模型覆盖，将 "default" 和空白字符串等哨兵值规范化为 None。

        对应 Rust:
        pub fn take_model_override(&mut self) -> Option<String>
        """
        model = self.model
        self.model = None
        normalized = normalized_model_override(model)
        return normalized


@dataclass
class CompletionResponse:
    """聊天完成的响应。"""
    content: str
    input_tokens: int
    output_tokens: int
    finish_reason: FinishReason
    reasoning: Optional[str] = None
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class ToolDefinition:
    """LLM 的工具定义。"""
    name: str
    description: str
    parameters: Any = field(default_factory=dict)  # serde_json::Value，Python 中常用 dict


@dataclass
class ToolCall:
    """
    LLM 请求的工具调用。

    对应 Rust:
    pub struct ToolCall { ... }
    """
    id: str = ""
    name: str = ""
    arguments: Any = field(default_factory=dict)  # serde_json::Value

    # 选择此工具的可选推理说明 —— 由提供商提供，或从共享响应内容中派生作为后备。
    reasoning: Optional[str] = None

    # 提供商为每个工具调用发出的加密签名（Gemini 的 thought_signature、
    # Anthropic 的推理签名）。需要在下一次请求时原样回传 ——
    # 当先前工具调用的签名被丢弃时，Gemini 2.5+ 会以 HTTP 400 拒绝工具循环回合。
    # 参见 #3225。
    signature: Optional[str] = None

    # 模型工具调用参数 JSON 的提供商解析失败信息。
    # 当线路载荷不是有效 JSON 且提供商回退到空对象时为 Some(reason)；
    # 解析成功时为 None。
    arguments_parse_error: Optional[str] = None


def generate_tool_call_id(seed_a: int, seed_b: int) -> str:
    """
    生成一个满足所有提供商的工具调用 ID。

    Mistral 要求正好 9 个字母数字字符 ([a-zA-Z0-9]{9})。
    其他提供商接受任何非空字符串。默认情况下，我们生成一个从两个种子值派生的
    9 字符 base-62 字符串，这样 ID 既是确定性的（用于重放历史记录），又与提供商兼容。

    对应 Rust:
    pub fn generate_tool_call_id(seed_a: usize, seed_b: usize) -> String
    """
    # 将两个种子混合为单个 u64，使用类似哈希的组合方式
    combined = (seed_a * 6364136223846793005 + seed_b) & 0xFFFFFFFFFFFFFFFF

    BASE62_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    buf = []
    val = combined
    for _ in range(9):
        digit = val % 62
        buf.append(BASE62_CHARS[digit])
        val //= 62

    # 反转以匹配 Rust 代码的迭代顺序，并填充到 9 位
    while len(buf) < 9:
        buf.append('0')
    return ''.join(reversed(buf[-9:]))


@dataclass
class ToolResult:
    """工具执行的结果，发送回 LLM。"""
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class ToolCompletionRequest:
    """带工具使用的完成请求。"""
    messages: List[ChatMessage]
    tools: List[ToolDefinition]
    model: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stop_sequences: Optional[List[str]] = None
    tool_choice: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def new(cls, messages: List[ChatMessage], tools: List[ToolDefinition]) -> "ToolCompletionRequest":
        """创建新的工具完成请求。"""
        return cls(messages=messages, tools=tools)

    @classmethod
    def from_completion_request(cls, request: CompletionRequest,
                                tools: List[ToolDefinition]) -> "ToolCompletionRequest":
        """从通用完成信封创建工具感知请求。"""
        return cls(
            messages=request.messages,
            tools=tools,
            model=request.model,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            stop_sequences=request.stop_sequences,
            metadata=request.metadata,
        )

    def with_model(self, model: str) -> "ToolCompletionRequest":
        self.model = model
        return self

    def with_max_tokens(self, max_tokens: int) -> "ToolCompletionRequest":
        self.max_tokens = max_tokens
        return self

    def with_temperature(self, temperature: float) -> "ToolCompletionRequest":
        self.temperature = temperature
        return self

    def with_stop_sequences(self, stop_sequences: List[str]) -> "ToolCompletionRequest":
        self.stop_sequences = stop_sequences
        return self

    def with_tool_choice(self, choice: str) -> "ToolCompletionRequest":
        self.tool_choice = choice
        return self

    def take_model_override(self) -> Optional[str]:
        """
        取出按请求的模型覆盖，将 "default" 和空白字符串等哨兵值规范化为 None。
        """
        model = self.model
        self.model = None
        normalized = normalized_model_override(model)
        return normalized


@dataclass
class ToolCompletionResponse:
    """带潜在工具调用的完成的响应。"""
    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: FinishReason = FinishReason.UNKNOWN
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning: Optional[str] = None


@dataclass
class ModelMetadata:
    """提供商 API 返回的模型元数据。"""
    id: str
    context_length: Optional[int] = None


# ---------- 辅助函数 ----------

def normalized_model_override(model: Optional[str]) -> Optional[str]:
    """
    规范化请求的模型覆盖。

    "default" 被视为哨兵值，表示 "使用提供商的活跃模型"，
    匹配网关 API 并保护像 Anthropic 这样拒绝将字面字符串作为未知模型 ID 的提供商。

    对应 Rust:
    pub fn normalized_model_override(model: Option<&str>) -> Option<&str>
    """
    if model is None:
        return None
    model = model.strip()
    if not model or model.lower() == "default":
        return None
    return model


def sanitize_tool_messages(messages: List[ChatMessage]) -> None:
    """
    清理消息列表以确保 tool_use / tool_result 的完整性。

    LLM API（尤其是 Anthropic）要求每个 tool_result 引用一个存在于
    紧随其前的助手消息的 tool_calls 中的 tool_call_id。
    孤立的 tool_results 会导致 HTTP 400 错误。

    此函数：
    1. 跟踪所有由助手消息发出的 tool_call_ids。
    2. 将孤立的 tool_result 消息（其 tool_call_id 没有匹配的助手 tool_call）
       重写为用户消息，以便在不违反协议的情况下保留内容。

    在将消息发送到任何 LLM 提供商之前调用此函数。

    对应 Rust:
    pub fn sanitize_tool_messages(messages: &mut [ChatMessage])
    """
    # 从包含 tool_calls 的助手消息中收集所有 tool_call_ids
    known_ids = set()
    for msg in messages:
        if msg.role == Role.Assistant and msg.tool_calls:
            for tc in msg.tool_calls:
                known_ids.add(tc.id)

    # 将孤立的 tool_result 消息重写为用户消息
    for msg in messages:
        if msg.role != Role.TOOL:
            continue

        is_orphaned = (
                msg.tool_call_id is None
                or msg.tool_call_id not in known_ids
        )

        if is_orphaned:
            tool_name = msg.name or "unknown"
            logger.debug(
                "将孤立的 tool_result 重写为用户消息: tool_call_id=%s, tool_name=%s",
                msg.tool_call_id,
                tool_name,
            )
            msg.role = Role.User
            msg.content = f"[Tool `{tool_name}` returned: {msg.content}]"
            msg.tool_call_id = None
            msg.name = None


def strip_unsupported_completion_params(
        unsupported: set,
        req: CompletionRequest,
) -> None:
    """
    原地从 CompletionRequest 中去除不支持的参数。

    这是所有提供商用于移除它们不支持的参数的单一辅助函数，
    替换了重复的字符串类型逻辑。

    对应 Rust:
    pub(crate) fn strip_unsupported_completion_params(...)
    """
    if not unsupported:
        return
    if UnsupportedParam.TEMPERATURE in unsupported:
        req.temperature = None
    if UnsupportedParam.MAX_TOKENS in unsupported:
        req.max_tokens = None
    if UnsupportedParam.STOP_SEQUENCES in unsupported:
        req.stop_sequences = None


def strip_unsupported_tool_params(
        unsupported: set,
        req: ToolCompletionRequest,
) -> None:
    """
    原地从 ToolCompletionRequest 中去除不支持的参数。

    这是所有提供商用于从工具调用中移除它们不支持的参数的单一辅助函数，
    替换了重复的字符串类型逻辑。

    对应 Rust:
    pub(crate) fn strip_unsupported_tool_params(...)
    """
    if not unsupported:
        return
    if UnsupportedParam.TEMPERATURE in unsupported:
        req.temperature = None
    if UnsupportedParam.MAX_TOKENS in unsupported:
        req.max_tokens = None
    if UnsupportedParam.STOP_SEQUENCES in unsupported:
        req.stop_sequences = None


# ---------- 抽象基类 ----------

class LlmError(Exception):
    """LLM 错误基类。"""
    pass


class RequestFailedError(LlmError):
    """请求失败错误。"""

    def __init__(self, provider: str, reason: str):
        self.provider = provider
        self.reason = reason
        super().__init__(f"{provider}: {reason}")


class LlmProvider(ABC):
    """
    LLM 提供商的 trait。

    对应 Rust:
    #[async_trait]
    pub trait LlmProvider: Send + Sync { ... }
    """

    @abstractmethod
    def model_name(self) -> str:
        """获取模型名称。"""
        pass

    @abstractmethod
    def cost_per_token(self) -> Tuple["Decimal", "Decimal"]:
        """获取每个令牌的成本（输入，输出）。"""
        pass

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """完成聊天对话。"""
        pass

    @abstractmethod
    async def complete_with_tools(self, request: ToolCompletionRequest) -> ToolCompletionResponse:
        """带工具使用支持的完成。"""
        pass

    async def list_models(self) -> List[str]:
        """
        列出提供商提供的可用模型。
        默认实现返回空列表。
        """
        return []

    async def model_metadata(self) -> ModelMetadata:
        """
        获取当前模型的元数据（上下文长度等）。
        默认返回模型名称，没有大小信息。
        """
        return ModelMetadata(id=self.model_name())

    def effective_model_name(self, requested_model: Optional[str] = None) -> str:
        """
        解析对于给定请求应报告哪个模型。

        忽略按请求模型覆盖的提供商应重写此方法并返回 active_model_name()。

        对应 Rust:
        fn effective_model_name(&self, requested_model: Option<&str>) -> String
        """
        return normalized_model_override(requested_model) or self.active_model_name()

    def active_model_name(self) -> str:
        """
        获取当前活跃的模型名称。

        如果通过 set_model() 在运行时切换了模型，可能与 model_name() 不同。
        默认返回 model_name()。
        """
        return self.model_name()

    def set_model(self, model: str) -> None:
        """
        在运行时切换活跃模型。并非所有提供商都支持此操作。

        对应 Rust:
        fn set_model(&self, _model: &str) -> Result<(), LlmError>
        """
        raise RequestFailedError(
            provider="unknown",
            reason="此提供商不支持运行时模型切换",
        )

    def calculate_cost(self, input_tokens: int, output_tokens: int) -> "Decimal":
        """计算完成的成本。"""
        input_cost, output_cost = self.cost_per_token()
        return input_cost * input_tokens + output_cost * output_tokens

    def cache_write_multiplier(self) -> "Decimal":
        """
        缓存创建令牌的成本乘数（Anthropic 提示缓存）。

        默认返回 1.0（无附加费）。Anthropic 提供商对 5 分钟 TTL 返回 1.25，
        对 1 小时 TTL 返回 2.0。
        """
        return Decimal(1)

    def cache_read_discount(self) -> "Decimal":
        """
        缓存读取令牌的折扣除数。

        缓存读取成本 = input_rate / cache_read_discount()。
        默认返回 1（无折扣）。Anthropic 返回 10（90% 折扣），
        OpenAI 返回 2（50% 折扣）。
        """
        return Decimal(1)
