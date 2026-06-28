#! 提供商转录产物的共享语法。
# !
# ! 某些提供商无法保留原生的工具结果角色，需要文本兼容性表示。
# ! 将该表示集中管理，以便生产者、响应清理和最终回复准入不会产生不一致。


# 对应 Rust: pub const LEGACY_TOOL_EVENT_PREFIX: &str = "Previous tool event: ";
LEGACY_TOOL_EVENT_PREFIX = "Previous tool event: "

# 对应 Rust: pub const LEGACY_TOOL_EVENT_SUFFIX: &str = " was invoked.";
LEGACY_TOOL_EVENT_SUFFIX = " was invoked."

# 对应 Rust: pub const LEGACY_TOOL_RESULT_PREFIX: &str = "Previous tool result from ";
LEGACY_TOOL_RESULT_PREFIX = "Previous tool result from "

# 对应 Rust: pub const TOOL_RESULT_OBSERVATION_PREFIX: &str = "Tool result from ";
TOOL_RESULT_OBSERVATION_PREFIX = "Tool result from "


def format_tool_result_observation(tool_name: str, result: str) -> str:
    """
    格式化工具结果观察字符串。

    对应 Rust:
    pub fn format_tool_result_observation(tool_name: &str, result: &str) -> String

    参数:
        tool_name: 工具名称。
        result: 工具执行结果。

    返回:
        格式化后的观察字符串。
    """
    # 对应 Rust: if result.is_empty() { format!("{TOOL_RESULT_OBSERVATION_PREFIX}{tool_name}:") }
    if not result:
        return f"{TOOL_RESULT_OBSERVATION_PREFIX}{tool_name}:"
    # 对应 Rust: else { format!("{TOOL_RESULT_OBSERVATION_PREFIX}{tool_name}: {result}") }
    return f"{TOOL_RESULT_OBSERVATION_PREFIX}{tool_name}: {result}"


def _is_transcript_tool_name(name: str) -> bool:
    """
    检查给定的工具名称是否为转录工具名称。

    转录工具名称包含 "__"，且所有字符为 ASCII 字母数字、下划线、短划线或点。

    对应 Rust:
    fn is_transcript_tool_name(name: &str) -> bool

    参数:
        name: 要检查的工具名称。

    返回:
        bool: 如果是转录工具名称则返回 True。
    """
    # 对应 Rust: name.contains("__") && name.chars().all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.'))
    if "__" not in name:
        return False
    return all(ch.isascii() and (ch.isalnum() or ch in ('_', '-', '.')) for ch in name)


def _is_legacy_tool_event_line(line: str) -> bool:
    """
    检查一行是否为遗留工具事件行。

    对应 Rust:
    fn is_legacy_tool_event_line(line: &str) -> bool

    参数:
        line: 要检查的行。

    返回:
        bool: 如果是遗留工具事件行则返回 True。
    """
    # 对应 Rust: let Some(tool_name) = line.strip_prefix(LEGACY_TOOL_EVENT_PREFIX).and_then(|rest| rest.strip_suffix(LEGACY_TOOL_EVENT_SUFFIX)) else { return false; };
    if not line.startswith(LEGACY_TOOL_EVENT_PREFIX):
        return False
    if not line.endswith(LEGACY_TOOL_EVENT_SUFFIX):
        return False
    tool_name = line[len(LEGACY_TOOL_EVENT_PREFIX):-len(LEGACY_TOOL_EVENT_SUFFIX)]
    # 对应 Rust: is_transcript_tool_name(tool_name)
    return _is_transcript_tool_name(tool_name)


def _is_tool_result_line(line: str, prefix: str) -> bool:
    """
    检查一行是否为工具结果行（使用指定的前缀）。

    对应 Rust:
    fn is_tool_result_line(line: &str, prefix: &str) -> bool

    参数:
        line: 要检查的行。
        prefix: 要匹配的前缀。

    返回:
        bool: 如果是工具结果行则返回 True。
    """
    # 对应 Rust: let Some(rest) = line.strip_prefix(prefix) else { return false; };
    if not line.startswith(prefix):
        return False
    rest = line[len(prefix):]
    # 对应 Rust: let Some((tool_name, _result)) = rest.split_once(':') else { return false; };
    parts = rest.split(':', 1)
    if len(parts) < 2:
        return False
    tool_name = parts[0]
    # 对应 Rust: is_transcript_tool_name(tool_name)
    return _is_transcript_tool_name(tool_name)


def is_provider_transcript_artifact_line(line: str) -> bool:
    """
    检查一行是否为提供商转录产物行。

    对应 Rust:
    pub fn is_provider_transcript_artifact_line(line: &str) -> bool

    参数:
        line: 要检查的行。

    返回:
        bool: 如果是提供商转录产物行则返回 True。
    """
    # 对应 Rust: let line = line.trim();
    line = line.strip()
    # 对应 Rust: is_legacy_tool_event_line(line) || is_tool_result_line(line, LEGACY_TOOL_RESULT_PREFIX) || is_tool_result_line(line, TOOL_RESULT_OBSERVATION_PREFIX)
    return (
            _is_legacy_tool_event_line(line)
            or _is_tool_result_line(line, LEGACY_TOOL_RESULT_PREFIX)
            or _is_tool_result_line(line, TOOL_RESULT_OBSERVATION_PREFIX)
    )


def strip_provider_transcript_artifact_lines(text: str) -> str:
    """
    从文本中剥离所有提供商转录产物行。

    对应 Rust:
    pub fn strip_provider_transcript_artifact_lines(text: &str) -> String

    参数:
        text: 要处理的文本。

    返回:
        剥离转录产物行后的文本。
    """
    # 如果没有任何转录产物行，直接返回原文本
    # 对应 Rust: if !text.lines().any(is_provider_transcript_artifact_line) { return text.to_string(); }
    if not any(is_provider_transcript_artifact_line(line) for line in text.split('\n')):
        return text

    # 记录原始文本是否以换行符结尾
    # 对应 Rust: let had_trailing_newline = text.ends_with('\n');
    had_trailing_newline = text.endswith('\n')

    # 过滤掉所有转录产物行
    # 对应 Rust: let mut stripped = text.lines().filter(|line| !is_provider_transcript_artifact_line(line)).collect::<Vec<_>>().join("\n");
    lines = text.split('\n')
    filtered_lines = [line for line in lines if not is_provider_transcript_artifact_line(line)]
    stripped = '\n'.join(filtered_lines)

    # 如果原始文本以换行符结尾且剥离后不为空，则保留尾部换行符
    # 对应 Rust: if had_trailing_newline && !stripped.is_empty() { stripped.push('\n'); }
    if had_trailing_newline and stripped:
        stripped += '\n'

    # 对应 Rust: stripped
    return stripped


def is_only_provider_transcript_artifact_lines(text: str) -> bool:
    """
    检查文本是否仅由提供商转录产物行组成。

    对应 Rust:
    pub fn is_only_provider_transcript_artifact_lines(text: &str) -> bool

    参数:
        text: 要检查的文本。

    返回:
        bool: 如果仅包含转录产物行则返回 True。
    """
    # 获取所有非空行
    # 对应 Rust: let mut meaningful_lines = text.lines().map(str::trim).filter(|line| !line.is_empty());
    meaningful_lines = [line.strip() for line in text.split('\n') if line.strip()]

    # 如果没有有效行，返回 False
    # 对应 Rust: let Some(first) = meaningful_lines.next() else { return false; };
    if not meaningful_lines:
        return False

    first = meaningful_lines[0]
    rest = meaningful_lines[1:]

    # 第一行必须是转录产物行，且所有后续行也必须是转录产物行
    # 对应 Rust: is_provider_transcript_artifact_line(first) && meaningful_lines.all(is_provider_transcript_artifact_line)
    return (
            is_provider_transcript_artifact_line(first)
            and all(is_provider_transcript_artifact_line(line) for line in rest)
    )
