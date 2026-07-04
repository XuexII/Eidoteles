# 大语言模型桥接适配器——将 `LlmProvider` 包装为 `ironclaw_engine::LlmBackend`。

from engine import (
    ActionDef, EngineError, LlmBackend, LlmCallConfig, LlmOutput, LlmResponse, ThreadMessage,
    TokenUsage,
)
from rust_decimal import Decimal
from rust_decimal.prelude import ToPrimitive

from llm import (
    ChatMessage, LlmProvider, Role, ToolCall, ToolCompletionRequest, ToolDefinition,
    clean_response, recover_tool_calls_from_content, sanitize_tool_messages,
)

import re
import json
from decimal import Decimal
from typing import Optional, List, Tuple, Any

# ── 常量 ─────────────────────────────────────────────────────

EMPTY_CLEANED_RESPONSE_FALLBACK = "我不确定如何回应那个。"

# Python 关键字列表（用于看起来像 Python 的启发式检查）
_PY_KEYWORDS = {
    "import", "from", "def", "class", "if", "for", "while", "return",
    "print", "FINAL", "try", "with", "pass", "raise", "yield", "lambda",
    "elif", "else", "async", "await", "global", "nonlocal", "assert",
    "break", "continue", "del", "not", "and", "or", "is", "in",
}


# ── 成本计算 ─────────────────────────────────────────────────

def cost_usd_from(
        provider: Any,  # LlmProvider
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int,
        cache_creation_input_tokens: int,
) -> float:
    """计算单次完成响应的美元成本，遵循提供者的提示缓存定价。
    镜像 `CostGuard::record_llm_call` 中的公式，以便引擎 v2 的
    `Thread::total_cost_usd` 匹配 `max_budget_usd` / v1 每日预算执行器计算的值：

    * 未缓存的输入 token 按 `cost_per_token().0` 定价；
    * 缓存读取 token 按 `cache_read_discount()` 折扣（Anthropic 10 倍折扣，OpenAI 2 倍）；
    * 缓存写入 token 乘以 `cache_write_multiplier()`（Anthropic 5 分钟 TTL 1.25 倍，1 小时 2 倍）；
    * 输出 token 按 `cost_per_token().1` 定价

    对于报告 `cost_per_token() == (0, 0)` 的订阅计费提供者（例如通过 ChatGPT OAuth 的 OpenAI Codex）返回 0.0
    """
    input_rate, output_rate = provider.cost_per_token()

    # `input_tokens` 是提供者报告的总数。缓存 token 已经计入该总数，
    # 因此未缓存的余数是减去两个桶后剩下的部分
    cached_total = cache_read_input_tokens + cache_creation_input_tokens
    uncached_input = max(0, input_tokens - cached_total)

    # 防止提供者报告零折扣 — 将零视为"无折扣"而不是尝试除以零
    discount = provider.cache_read_discount()
    effective_discount = max(discount, Decimal('1'))

    cache_read_cost = input_rate * Decimal(str(cache_read_input_tokens)) / effective_discount
    cache_write_cost = (
            input_rate * Decimal(str(cache_creation_input_tokens)) * provider.cache_write_multiplier()
    )
    cost = (
            input_rate * Decimal(str(uncached_input))
            + cache_read_cost
            + cache_write_cost
            + output_rate * Decimal(str(output_tokens))
    )

    return float(cost)


# ── 模板引用解析 ─────────────────────────────────────────────

def resolve_template_refs(value: str, tool_results: List[Tuple[str, Any]]) -> str:
    """无正则表达式的轻量扫描 `{{<call_id>.<field>}}` 模式。
    迭代解析引用。如果遇到无法解析的引用，解析停止并保留较早的成功替换（部分解析）。
    如果未找到 `{{` 标记，则返回原始字符串不变
    """
    if "{{" not in value:
        return value

    result = value
    search_from = 0
    # 迭代解析所有 `{{..}}` 模式（限制迭代次数以防止无限循环）
    for _ in range(50):
        rel_start = result.find("{{", search_from)
        if rel_start == -1:
            break
        start = search_from + rel_start
        rel_end = result.find("}}", start)
        if rel_end == -1:
            break
        end = rel_end
        ref_str = result[start + 2:end]  # e.g. "chatcmpl-tool-9816a462feb22da1.project_id"

        resolved = None
        dot_pos = ref_str.rfind('.')
        if dot_pos != -1:
            call_id = ref_str[:dot_pos]
            field = ref_str[dot_pos + 1:]
            for tid, json_val in tool_results:
                if tid == call_id:
                    if isinstance(json_val, dict) and field in json_val:
                        val = json_val[field]
                        resolved = val if isinstance(val, str) else str(val)
                    break

        if resolved is not None:
            result = result[:start] + resolved + result[end + 2:]
            search_from = start + len(resolved)
        else:
            # 无法解析 — 跳过此 `{{` 以避免在同一模式上无限循环
            search_from = start + 2

    return result


def resolve_template_refs_in_json(value: Any, tool_results: List[Tuple[str, Any]]) -> Any:
    """遍历 JSON 值并解析任何在字符串值中找到的 `{{call_id.field}}` 模板引用"""
    if isinstance(value, str):
        resolved = resolve_template_refs(value, tool_results)
        return resolved
    elif isinstance(value, dict):
        return {k: resolve_template_refs_in_json(v, tool_results) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_template_refs_in_json(v, tool_results) for v in value]
    else:
        return value


def build_tool_result_index(messages: List[Any]) -> List[Tuple[str, Any]]:
    """从对话中的工具结果消息构建 (call_id -> 解析的 JSON) 查找表"""
    index = []
    for m in messages:
        if m.role == MessageRole.ActionResult:
            call_id = getattr(m, 'action_call_id', None)
            if call_id is None:
                continue
            try:
                parsed = json.loads(m.content)
            except (json.JSONDecodeError, TypeError):
                parsed = m.content
            index.append((call_id, parsed))
    return index


def json_has_template_refs(value: Any) -> bool:
    """如果 JSON 中的任何字符串值包含 `{{` 模板引用，则返回 True"""
    if isinstance(value, str):
        return "{{" in value
    elif isinstance(value, dict):
        return any(json_has_template_refs(v) for v in value.values())
    elif isinstance(value, list):
        return any(json_has_template_refs(v) for v in value)
    else:
        return False


# ── 转换辅助函数 ─────────────────────────────────────────────

def thread_msg_to_chat(msg: Any) -> Any:
    """将 ThreadMessage 转换为 ChatMessage"""
    role_map = {
        MessageRole.System: Role.System,
        MessageRole.User: Role.User,
        MessageRole.Assistant: Role.Assistant,
        MessageRole.ActionResult: Role.Tool,
    }
    role = role_map.get(msg.role, Role.User)

    chat = ChatMessage(
        role=role,
        content=msg.content,
        content_parts=[],
        tool_call_id=getattr(msg, 'action_call_id', None),
        name=getattr(msg, 'action_name', None),
        tool_calls=None,
        reasoning=None,
    )

    if hasattr(msg, 'action_calls') and msg.action_calls is not None:
        chat.tool_calls = [
            ToolCall(
                id=c.id,
                name=c.action_name,
                arguments=c.parameters,
                reasoning=None,
                signature=None,
                arguments_parse_error=None,
            )
            for c in msg.action_calls
        ]

    return chat


def action_def_to_tool_def(action: Any) -> Any:
    """将 ActionDef 转换为 ToolDefinition"""
    has_discovery_hint = (
                                 hasattr(action, 'discovery_summary') and action.discovery_summary() is not None
                         ) or (
                                 hasattr(action,
                                         'discovery_schema') and action.discovery_schema() != action.parameters_schema
                         )

    if has_discovery_hint:
        description = (
            f"{action.description} (调用 tool_info(name=\"{action.discovery_name()}\", "
            f"detail=\"summary\") 获取规则/示例，或 detail=\"schema\" 获取完整发现模式)"
        )
    else:
        description = action.description

    return ToolDefinition(
        name=action.name,
        description=description,
        parameters=action.parameters_schema,
    )


# ── 代码块提取 ───────────────────────────────────────────────

def extract_code_block(text: str) -> Optional[str]:
    """从 LLM 响应中的围栏代码块提取 Python 代码

    按顺序尝试这些标记：```repl、```python、```py，然后是裸 ```
    （如果内容看起来像 Python）。收集响应中的所有代码块并将它们连接起来
    （模型有时将代码分割到多个块中，中间有解释文本）
    """
    all_code = []

    for marker in ["```repl", "```python", "```py", "```"]:
        search_from = 0
        while True:
            start = text.find(marker, search_from)
            if start == -1:
                break

            after_marker = start + len(marker)

            # 对于裸 ```，如果是 ```someotherlang 则跳过
            if marker == "```":
                remaining = text[after_marker:]
                lang_chars = []
                for c in remaining:
                    if c.isalnum() or c in ('-', '_'):
                        lang_chars.append(c)
                    else:
                        break
                lang = ''.join(lang_chars)
                if lang and lang not in ("repl", "python", "py"):
                    search_from = after_marker
                    continue

            # 跳过标记后的下一行
            newline_pos = text.find('\n', after_marker)
            code_start = newline_pos + 1 if newline_pos != -1 else after_marker

            # 找到闭合的 ```
            end = text.find("```", code_start)
            if end == -1:
                break

            code = text[code_start:end].strip()
            if code:
                # 对于裸 ``` 块（无显式语言标签），仅接受看起来像 Python 的内容。
                # 没有此保护，代理的示例 markdown 块（列表、表格、纯散文）会被错误分类为代码，
                # 并在 Monty 解析器中以 SyntaxError 爆炸 — LLM 随后必须从中恢复
                if marker == "```" and not looks_like_python(code):
                    search_from = end + 3
                    continue
                all_code.append(code)
            search_from = end + 3

        # 如果使用特定标记找到代码，使用它（不继续到裸标记）
        if all_code:
            break

    if not all_code:
        return None

    return "\n\n".join(all_code)


def text_response_from_cleaned_text(cleaned_text: str) -> Any:
    """从清理后的文本构建 LLM 响应"""
    if codeact_disabled():
        if not cleaned_text.strip():
            return LlmResponse.Text(EMPTY_CLEANED_RESPONSE_FALLBACK)
        return LlmResponse.Text(cleaned_text)

    code = extract_code_block(cleaned_text)
    if code is not None:
        return LlmResponse.Code(code=code, content=cleaned_text)
    elif not cleaned_text.strip():
        return LlmResponse.Text(EMPTY_CLEANED_RESPONSE_FALLBACK)
    else:
        return LlmResponse.Text(cleaned_text)


# ── Python 启发式检查 ────────────────────────────────────────

def has_identifier_call(line: str) -> bool:
    """当 `line` 包含标识符式函数调用（标识符或属性路径后紧跟 `(`）时返回 True

    避免 `trimmed.contains('(')` 对 markdown 链接如 `[text](url)` 和散文如
    "See (docs)" 产生的误报 — 两者在 `(` 之前都没有字母数字/下划线字符
    """
    for i, c in enumerate(line):
        if c == '(' and i > 0:
            prev = line[i - 1]
            if prev.isalnum() or prev == '_':
                return True
    return False


def looks_like_python(code: str) -> bool:
    """启发式检查裸 ``` 块是否包含 Python 而不是 markdown/散文/其他语言

    接受：赋值（`x =`）、函数调用（`name(`）、Python 关键字、
    或注释（`#`）

    拒绝：以 `-`、`*`、`|`、`>`、数字后跟 `.`（markdown 列表、表格、块引用、标题、编号列表）开头的行，
    裸散文等
    """
    for line in code.split('\n')[:5]:
        trimmed = line.strip()
        if not trimmed:
            continue
        # 注释是有效的 Python
        if trimmed.startswith('#'):
            return True
        # Markdown 标记不是 Python
        if trimmed[0] in ('-', '*', '|', '>'):
            return False
        # Markdown 编号列表 "1. foo" 不是 Python
        if trimmed[0].isdigit() and '. ' in trimmed:
            return False
        # 函数调用
        if has_identifier_call(trimmed):
            return True
        # 赋值：`name = ...`（但不是散文中的 `==` 比较）
        if '=' in trimmed:
            return True
        # 第一个词匹配 Python 关键字
        first_word = ''.join(c for c in trimmed if c.isalnum() or c == '_').split('_')[
            0] if '_' in trimmed else ''.join(c for c in trimmed if c.isalnum() or c == '_')
        # 提取第一个词
        first_word = ''
        for c in trimmed:
            if c.isalnum() or c == '_':
                first_word += c
            else:
                break
        if first_word in _PY_KEYWORDS:
            return True
    return False


# ── LlmBridgeAdapter ────────────────────────────────────────

class LlmBridgeAdapter(LlmBackend):
    """包装现有 `LlmProvider` 以实现引擎的 `LlmBackend` 接口"""

    def __init__(
            self,
            provider: LlmProvider,  # LlmProvider
            cheap_provider: Optional[LlmProvider] = None,  # LlmProvider
    ):
        self.provider = provider
        self.cheap_provider = cheap_provider

    def provider_for_depth(self, depth: int) -> Any:
        """根据递归深度选择合适的提供者"""
        if depth > 0 and self.cheap_provider is not None:
            return self.cheap_provider
        return self.provider

    def model_name(self) -> str:
        """获取模型名称"""
        return self.provider.model_name()

    async def complete(
            self,
            messages: List[Any],  # List[ThreadMessage]
            actions: List[Any],  # List[ActionDef]
            config: LlmCallConfig,
    ) -> LlmOutput:
        """调用 LLM 完成请求"""
        provider = self.provider_for_depth(config.depth)

        # 转换消息
        chat_messages = [thread_msg_to_chat(m) for m in messages]
        sanitize_tool_messages(chat_messages)

        # 转换动作为工具定义
        # 在禁用 CodeAct 模式下，模型没有 Python 逃逸出口，因此每个可调用动作必须
        # 通过提供者的结构化 `tool_calls` 接口可达
        if config.force_text:
            tools = []  # 强制文本时不提供工具
        elif codeact_disabled():
            tools = [action_def_to_tool_def(a) for a in actions]
        else:
            tools = [
                action_def_to_tool_def(a)
                for a in actions
                if a.emits_full_schema_tool()
            ]

        max_tokens = config.max_tokens or 4096
        temperature = config.temperature or 0.7

        if not tools:
            # 无工具：使用纯文本完成
            request = CompletionRequest(chat_messages)
            request.max_tokens = max_tokens
            request.temperature = temperature
            request.metadata = config.metadata
            if config.model is not None:
                request.model = config.model

            response = await provider.complete(request)

            cleaned_text = clean_response(response.content)
            llm_response = text_response_from_cleaned_text(cleaned_text)

            return LlmOutput(
                response=llm_response,
                usage=TokenUsage(
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cache_read_tokens=response.cache_read_input_tokens,
                    cache_write_tokens=response.cache_creation_input_tokens,
                    cost_usd=cost_usd_from(
                        provider,
                        response.input_tokens,
                        response.output_tokens,
                        response.cache_read_input_tokens,
                        response.cache_creation_input_tokens,
                    ),
                ),
            )

        # 有工具：使用工具完成
        request = ToolCompletionRequest(chat_messages, tools)
        request.max_tokens = max_tokens
        request.temperature = temperature
        request.tool_choice = "auto"
        request.metadata = config.metadata
        if config.model is not None:
            request.model = config.model

        response = await provider.complete_with_tools(request)

        # 转换响应 — 检查代码块（CodeAct/RLM 模式）
        if response.tool_calls:
            calls = [
                ActionCall(
                    id=tc.id,
                    action_name=tc.name,
                    parameters=tc.arguments,
                )
                for tc in response.tool_calls
            ]

            # 解析工具调用参数中的 `{{call_id.field}}` 模板引用。
            # 某些模型（例如 Qwen）在进行引用先前调用结果的并行工具调用时发出这些
            if any(json_has_template_refs(c.parameters) for c in calls):
                tool_results = build_tool_result_index(messages)
                if tool_results:
                    for call in calls:
                        if json_has_template_refs(call.parameters):
                            call.parameters = resolve_template_refs_in_json(
                                call.parameters, tool_results
                            )

            llm_response = LlmResponse.ActionCalls(
                calls=calls,
                content=response.content,
            )
        else:
            raw_text = response.content or ""
            cleaned_text = clean_response(raw_text)
            recovered_calls = recover_tool_calls_from_content(raw_text, tools)

            if recovered_calls:
                calls = [
                    ActionCall(
                        id=tc.id,
                        action_name=tc.name,
                        parameters=tc.arguments,
                    )
                    for tc in recovered_calls
                ]
                content = cleaned_text.strip() if cleaned_text.strip() else None
                llm_response = LlmResponse.ActionCalls(calls=calls, content=content)
            else:
                llm_response = text_response_from_cleaned_text(cleaned_text)

        return LlmOutput(
            response=llm_response,
            usage=TokenUsage(
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cache_read_tokens=response.cache_read_input_tokens,
                cache_write_tokens=response.cache_creation_input_tokens,
                cost_usd=cost_usd_from(
                    provider,
                    response.input_tokens,
                    response.output_tokens,
                    response.cache_read_input_tokens,
                    response.cache_creation_input_tokens,
                ),
            ),
        )