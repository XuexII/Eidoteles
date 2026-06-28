#! 代码库中使用的共享工具函数。

from typing import List, Any, Dict


def floor_char_boundary(s: str, pos: int) -> int:
    """
    查找 pos 处或之前最大的有效 UTF-8 字符边界。

    这是 str::floor_char_boundary（仅夜间版可用）的替代实现。
    当按字节位置截断字符串时使用，以避免在多字节字符上发生 panic。

    对应 Rust:
    pub fn floor_char_boundary(s: &str, pos: usize) -> usize

    参数:
        s: 输入字符串。
        pos: 期望的字节位置。

    返回:
        int: 调整后位于有效 UTF-8 字符边界的字节位置。
    """
    # 如果位置超出字符串长度，返回字符串长度
    # 对应 Rust: if pos >= s.len() { return s.len(); }
    if pos >= len(s):
        return len(s)

    # 向后查找有效的 UTF-8 字符边界（Python 字符串索引自动保证边界安全）
    # 对应 Rust: let mut i = pos; while i > 0 && !s.is_char_boundary(i) { i -= 1; }
    # Python 中字符串索引始终在字符边界上，因此直接返回 pos 即可
    # 但为了处理可能的字节偏移量情况，使用 encode 转为字节后处理
    encoded = s.encode('utf-8')
    if pos >= len(encoded):
        return len(s)

    # 向后搜索直到找到有效的 UTF-8 起始字节
    i = pos
    while i > 0:
        byte = encoded[i]
        # UTF-8 起始字节不以 0b10xxxxxx 开头
        if (byte & 0xC0) != 0x80:
            break
        i -= 1

    # 将字节偏移量转换回字符索引
    return len(encoded[:i].decode('utf-8'))


def ensure_ends_with_user_message(messages: List["ChatMessage"]) -> None:
    """
    确保消息列表中最后一条消息是用户角色的消息。

    NEAR AI 拒绝不以用户消息结尾的对话；
    Claude 4.6 拒绝助手预填充。在任何 LLM 完成请求之前调用此函数以满足两者的要求。

    对应 Rust:
    pub fn ensure_ends_with_user_message(messages: &mut Vec<ChatMessage>)

    参数:
        messages: 要检查并可能修改的消息列表。
    """
    # 检查最后一条消息是否为用户角色
    # 对应 Rust: if !matches!(messages.last(), Some(m) if m.role == Role::User) { ... }
    if not messages or messages[-1].role != Role.USER:
        # 追加一条通用的继续消息
        # 对应 Rust: messages.push(ChatMessage::user("Continue."));
        messages.append(ChatMessage.user("Continue."))


def llm_signals_completion(response: str) -> bool:
    """
    检查 LLM 响应是否明确表示作业/任务已完成。

    使用短语级别的匹配，避免裸词如 "done" 或 "complete"
    在非完成上下文中（例如 "not done yet"、"the download is incomplete"）产生误报。

    对应 Rust:
    pub fn llm_signals_completion(response: &str) -> bool

    参数:
        response: LLM 的响应文本。

    返回:
        bool: 如果响应明确表示完成则返回 True。
    """
    lower = response.lower()

    # 正面短语的超集（来自 worker/job.rs 和 worker/container.rs）
    # 对应 Rust: let positive_phrases = [ ... ];
    positive_phrases = [
        "job is complete",
        "job is done",
        "job is finished",
        "task is complete",
        "task is done",
        "task is finished",
        "work is complete",
        "work is done",
        "work is finished",
        "successfully completed",
        "have completed the job",
        "have completed the task",
        "have finished the job",
        "have finished the task",
        "all steps are complete",
        "all steps are done",
        "i have completed",
        "i've completed",
        "all done",
        "all tasks complete",
    ]

    # 负面短语
    # 对应 Rust: let negative_phrases = [ ... ];
    negative_phrases = [
        "not complete",
        "not done",
        "not finished",
        "incomplete",
        "unfinished",
        "isn't done",
        "isn't complete",
        "isn't finished",
        "not yet done",
        "not yet complete",
        "not yet finished",
    ]

    # 如果包含任何负面短语，则提前返回 False
    # 对应 Rust: let has_negative = negative_phrases.iter().any(|p| lower.contains(p));
    #           if has_negative { return false; }
    if any(p in lower for p in negative_phrases):
        return False

    # 检查是否包含任何正面短语
    # 对应 Rust: positive_phrases.iter().any(|p| lower.contains(p))
    return any(p in lower for p in positive_phrases)


def canonicalize_json_value(value: Any) -> Any:
    """
    递归排序 JSON 对象的键以进行确定性比较/哈希。

    数组保持顺序，对象按键排序，标量值原样传递。

    对应 Rust:
    pub fn canonicalize_json_value(value: Value) -> Value

    参数:
        value: 要规范化的 JSON 值（dict、list 或标量）。

    返回:
        规范化后的 JSON 值，对象键已排序。
    """
    # 对应 Rust: Value::Array(items) => { ... }
    if isinstance(value, list):
        # 递归规范化数组中的每个元素，保持数组顺序
        return [canonicalize_json_value(item) for item in value]

    # 对应 Rust: Value::Object(obj) => { ... }
    elif isinstance(value, dict):
        # 收集并排序对象键
        # 对应 Rust: let mut keys: Vec<String> = obj.keys().cloned().collect(); keys.sort();
        sorted_keys = sorted(value.keys())
        # 构建按键排序的新字典
        # 对应 Rust: let mut canonical = Map::new(); for key in keys { canonical.insert(key, canonicalize_json_value(value.clone())); }
        canonical: Dict[str, Any] = {}
        for key in sorted_keys:
            canonical[key] = canonicalize_json_value(value[key])
        return canonical

    # 对应 Rust: other => other
    else:
        # 标量值原样返回
        return value
