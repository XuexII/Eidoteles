#! 推理/思考模型检测工具。
# !
# ! ## 默认：假设原生思考
# !
# ! 大多数现代 LLM 要么具有内置思考能力（Qwen3、DeepSeek-R1、GLM-5），
# ! 要么在没有 `<think>/<final>` 提示注入的情况下也能正常工作（GPT-4o、Claude、Llama）。
# ! 将 `<think>/<final>` 标签注入到原生思考模型的系统提示中会导致响应异常：
# ! 模型将推理放入其原生的 `reasoning` 字段，而只在 `content` 中放入 `<think>` 标签，
# ! 响应清理器会将其剥离为空。
# !
# ! 因此，我们**默认不注入** `<think>/<final>` 标签。
# ! 只有在 `REQUIRES_THINK_FINAL_PATTERNS` 中明确列出的模型才会获得严格的标签格式。
# ! 这是安全的默认设置，因为：
# !
# ! - 对于本可以使用该格式的模型跳过注入 = 结构化程度稍低但仍可工作的响应
# ! - 向原生思考模型注入 = 异常/空响应
# !
# ! 这也处理了模型别名，如 NEAR AI 的 `"auto"`，它在服务端解析为
# ! `Qwen/Qwen3.5-122B-A10B` 等模型。由于 `"auto"` 不匹配任何模式，
# ! 它会落入安全的默认设置。


# 需要显式 <think>/<final> 提示注入的模型。
#
# 这些是被证明能受益于结构化思考标签，且**不**具备原生思考支持的模型。
# 该列表故意为空/最小化 —— 安全的默认设置是跳过注入。
# 对应 Rust:
# const REQUIRES_THINK_FINAL_PATTERNS: &[&str] = &[ ... ];
REQUIRES_THINK_FINAL_PATTERNS = [
    # 当前为空：尚未识别出需要 <think>/<final> 注入才能正常工作的模型。
    # 仅当特定模型被证明需要时才在此处添加模式。
]

# 已知支持 OpenAI Responses API reasoning 字段的模型。
#
# 向不支持的模型（例如 gpt-4o）发送 reasoning 对象会导致 API 拒绝请求，而不是忽略该参数。
# 对应 Rust:
# const OPENAI_REASONING_PATTERNS: &[&str] = &["o1", "o3", "o4", "/reasoning/", "gpt-5", "gpt-4.1"];
OPENAI_REASONING_PATTERNS = [
    "o1",
    "o3",
    "o4",
    "/reasoning/",
    "gpt-5",
    "gpt-4.1",
]

# 使用 adaptive 思考模式的 Anthropic 模型（4.6+/4.7+）。
#
# 这些模型接受 `{type: "adaptive"}`，不需要固定的 `budget_tokens` 上限。
# 对应 Rust:
# const ANTHROPIC_ADAPTIVE_THINKING_PATTERNS: &[&str] = &["claude-opus-4-6", "claude-sonnet-4-6", "claude-opus-4-7"];
ANTHROPIC_ADAPTIVE_THINKING_PATTERNS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
]

# 使用 enabled 思考模式的 Anthropic 模型（3.7、4.0–4.4 系列）。
#
# 这些模型接受 `{type: "enabled", budget_tokens: N}`。
# 注意：**不**包括 4.5+ 模型 —— 这些模型需要 adaptive 思考。
# 对应 Rust:
# const ANTHROPIC_ENABLED_THINKING_PATTERNS: &[&str] = &[ ... ];
ANTHROPIC_ENABLED_THINKING_PATTERNS = [
    "claude-3-7",
    # 4.0–4.4 系列：匹配特定版本前缀以避免泄漏到 4.5+
    "claude-4-0",
    "claude-4-1",
    "claude-4-2",
    "claude-4-3",
    "claude-4-4",
    "claude-sonnet-4-0",
    "claude-sonnet-4-1",
    "claude-sonnet-4-2",
    "claude-sonnet-4-3",
    "claude-sonnet-4-4",
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-opus-4-2",
    "claude-opus-4-3",
    "claude-opus-4-4",
]

# 已知具有原生思考能力的模型模式（用于 has_native_thinking 的遗留常量）。
# 对应 Rust 中 has_native_thinking 函数内的常量:
# const NATIVE_THINKING_PATTERNS: &[&str] = &[ ... ];
_NATIVE_THINKING_PATTERNS = [
    "qwen3",
    "qwq",
    "deepseek-r1",
    "deepseek-reasoner",
    "glm-z1",
    "glm-4-plus",
    "glm-5",
    "nanbeige",
    "step-3.5",
    "minimax-m2",
]


def requires_think_final_tags(model: str) -> bool:
    """
    检查模型是否需要显式的 `<think>/<final>` 提示注入。

    仅对已知需要结构化思考标签的允许列表中的模型返回 True。
    所有其他模型 —— 包括未知名称、`"auto"` 等别名以及原生思考模型 —— 返回 False，
    并使用直接回答的提示格式。

    对应 Rust:
    pub fn requires_think_final_tags(model: &str) -> bool

    参数:
        model: 模型名称。

    返回:
        bool: 如果模型需要 <think>/<final> 标签注入则返回 True。
    """
    # 对应 Rust: let lower = model.to_ascii_lowercase();
    lower = model.lower()
    # 对应 Rust: REQUIRES_THINK_FINAL_PATTERNS.iter().any(|p| lower.contains(p))
    return any(p in lower for p in REQUIRES_THINK_FINAL_PATTERNS)


def has_native_thinking(model: str) -> bool:
    """
    遗留辅助函数 —— 对已知的原生思考模型返回 True。

    保留用于需要知道模型是否具有原生思考的调用点
    （例如响应解析启发式），但不再用于提示注入决策。
    请改用 requires_think_final_tags。

    对应 Rust:
    pub fn has_native_thinking(model: &str) -> bool

    参数:
        model: 模型名称。

    返回:
        bool: 如果模型具有原生思考能力则返回 True。
    """
    # 对应 Rust: let lower = model.to_ascii_lowercase();
    lower = model.lower()
    # 对应 Rust: NATIVE_THINKING_PATTERNS.iter().any(|p| lower.contains(p))
    return any(p in lower for p in _NATIVE_THINKING_PATTERNS)


def supports_openai_reasoning(model: str) -> bool:
    """
    当模型已知支持 Responses API 的 reasoning 字段时返回 True。

    对应 Rust:
    pub fn supports_openai_reasoning(model: &str) -> bool

    参数:
        model: 模型名称。

    返回:
        bool: 如果模型支持 OpenAI reasoning 字段则返回 True。
    """
    # 对应 Rust: let lower = model.to_ascii_lowercase();
    lower = model.lower()
    # 对应 Rust: OPENAI_REASONING_PATTERNS.iter().any(|p| lower.contains(p))
    return any(p in lower for p in OPENAI_REASONING_PATTERNS)


def supports_anthropic_adaptive_thinking(model: str) -> bool:
    """
    如果模型符合 Anthropic 的 adaptive 思考模式条件，则返回 True。

    对应 Rust:
    pub fn supports_anthropic_adaptive_thinking(model: &str) -> bool

    参数:
        model: 模型名称。

    返回:
        bool: 如果模型支持 adaptive 思考则返回 True。
    """
    # 对应 Rust: let lower = model.to_ascii_lowercase();
    lower = model.lower()
    # 对应 Rust: ANTHROPIC_ADAPTIVE_THINKING_PATTERNS.iter().any(|p| lower.contains(p))
    return any(p in lower for p in ANTHROPIC_ADAPTIVE_THINKING_PATTERNS)


def supports_anthropic_enabled_thinking(model: str) -> bool:
    """
    如果模型符合 Anthropic 的 enabled 思考模式条件，则返回 True。

    对应 Rust:
    pub fn supports_anthropic_enabled_thinking(model: &str) -> bool

    参数:
        model: 模型名称。

    返回:
        bool: 如果模型支持 enabled 思考则返回 True。
    """
    # 对应 Rust: let lower = model.to_ascii_lowercase();
    lower = model.lower()
    # 对应 Rust: ANTHROPIC_ENABLED_THINKING_PATTERNS.iter().any(|p| lower.contains(p))
    return any(p in lower for p in ANTHROPIC_ENABLED_THINKING_PATTERNS)
