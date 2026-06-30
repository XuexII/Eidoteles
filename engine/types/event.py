# 事件溯源类型。
#
# 线程中的每个重要操作都被记录为一个事件。
# 这支持重放、调试、反思和基于追踪的测试。

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from engine.types.capability import LeaseId
from engine.types.step import StepId, TokenUsage, CodeExecutionFailure
from engine.types.thread import ThreadId, ThreadState


def summarize_params(action_name: str, params: Dict[str, Any]) -> Optional[str]:
    """
    生成工具参数的简短人类可读摘要以供显示。

    对于 `http`：显示剥离了查询字符串/用户信息/片段的 URL（签名 URL 和
    查询字符串密钥不得泄漏到调试 SSE 中）。对于 `web_search`：显示查询。
    对于 `shell`：显示经过编辑的命令。对于其他工具：显示第一个字符串参数，
    已截断。对于空或无法识别的参数返回 `None`。
    """
    summary = None

    if action_name in ("http", "web_fetch"):
        url = params.get("url")
        if isinstance(url, str):
            summary = _truncate(_strip_url_sensitive_parts(url), 80)

    elif action_name in ("web_search", "llm_context"):
        query = params.get("query")
        if isinstance(query, str):
            summary = _truncate(query, 60)

    elif action_name == "memory_search":
        query = params.get("query")
        if isinstance(query, str):
            summary = _truncate(query, 60)

    elif action_name == "memory_write":
        target = params.get("target")
        if isinstance(target, str):
            summary = target

    elif action_name == "memory_read":
        path = params.get("path")
        if isinstance(path, str):
            summary = path

    elif action_name == "shell":
        command = params.get("command")
        if isinstance(command, str):
            summary = _truncate(_redact_shell_command_for_display(command), 60)

    elif action_name == "message":
        content = params.get("content")
        if isinstance(content, str):
            summary = _truncate(content, 40)

    else:
        # 通用：显示键名看起来不敏感的第一个字符串值。
        # 之前的回退无条件返回第一个字符串，对于 MCP / 未知工具
        # 可能会将 `token`、`api_key`、`password` 等暴露到
        # 调试面板 SSE 和 `ActionExecuted` 事件中。
        if isinstance(params, dict):
            for key, value in params.items():
                if not _is_sensitive_param_key(key) and isinstance(value, str):
                    summary = _truncate(value, 50)
                    break

    return summary if (summary is not None and summary != "") else None


def _strip_url_sensitive_parts(url: str) -> str:
    """
    从 URL 中剥离查询字符串、片段和用户信息，以便调试摘要
    永远不会暴露查询字符串密钥、签名 URL 令牌或
    `user:password@host` 凭证。

    保守解析：识别 `scheme://` URL，在 `?` / `#` 上分割，
    并从授权部分丢弃 `user[:pass]@`。非 URL 字符串原样传递——
    调用者随后截断。
    """
    scheme_end = url.find("://")
    if scheme_end == -1:
        return url

    scheme = url[:scheme_end + 3]
    rest = url[scheme_end + 3:]

    # 首先切掉查询和片段
    end: str
    q_pos = rest.find('?')
    h_pos = rest.find('#')

    if q_pos != -1 and (h_pos == -1 or q_pos < h_pos):
        end = rest[:q_pos] + "?…"
    elif h_pos != -1 and (q_pos == -1 or h_pos < q_pos):
        end = rest[:h_pos] + "#…"
    else:
        end = rest

    # 从授权部分（如果有路径，则在第一个 `/` 之前）丢弃 `user[:pass]@`
    slash_pos = end.find('/')
    if slash_pos != -1:
        authority = end[:slash_pos]
        path_and_rest = end[slash_pos:]
    else:
        authority = end
        path_and_rest = ""

    at_pos = authority.rfind('@')
    if at_pos != -1:
        authority_clean = authority[at_pos + 1:]
    else:
        authority_clean = authority

    return f"{scheme}{authority_clean}{path_and_rest}"


# 预编译正则表达式（模块级，避免每次调用重新编译）
_SHELL_REDACT_PATTERNS: List[re.Pattern] = [
    # (1) 认证承载标志后的引号值：
    #     -H "Authorization: Bearer …"   -> -H "<REDACTED>"
    #     --header 'X-Api-Key: …'        -> --header '<REDACTED>'
    #     -u 'user:pass'                 -> -u '<REDACTED>'
    re.compile(
        r'(?i)(-H|--header|-u|--user|--token|--api-?key|--password|--auth|--bearer)(\s+|=)(["\'])[^"\']*(["\'])',
    ),
    # (2) 相同标志后的非引号值（在空白处停止）
    re.compile(
        r'(?i)(-H|--header|-u|--user|--token|--api-?key|--password|--auth|--bearer)(\s+|=)([^\s"\']\S*)',
    ),
    # (3) 单个 `-H '…'` 参数内的 Authorization 风格头部
    re.compile(
        r'(?i)(Authorization|X-Api-Key|X-Auth-Token|Bearer)\s*:\s*\S+',
    ),
    # (4) 命令中任意位置的 URL 查询字符串
    re.compile(
        r'([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s"\'?#]*)\?[^\s"\']*',
    ),
]


def _redact_shell_command_for_display(cmd: str) -> str:
    """
    在到达调试表面之前，编辑 shell 命令内部的认证承载参数值和 URL 查询字符串。

    涵盖代理编写的 `curl` / `wget` / `http` 调用中常见的密钥泄漏形式：

    * `-H`/`--header`、`-u`/`--user`、`--token`、`--api-key`、`--password`、
      `--auth`、`--bearer` 后的引号和非引号值。
    * 单个 `-H '…'` 参数内的 `Authorization:` / `X-Api-Key:` 风格头部。
    * 命令中任何位置嵌入的 URL 查询字符串。

    任何被剥离的内容都替换为 `<REDACTED>`（引号值）或尾随的 `?…`（URL 查询），
    以便读者可以看到某些内容已被移除。调用者仍会截断到显示宽度。
    """
    out = cmd
    out = _SHELL_REDACT_PATTERNS[0].sub(r'\1\2\3<REDACTED>\4', out)
    out = _SHELL_REDACT_PATTERNS[1].sub(r'\1\2<REDACTED>', out)
    out = _SHELL_REDACT_PATTERNS[2].sub(r'\1: <REDACTED>', out)
    out = _SHELL_REDACT_PATTERNS[3].sub(r'\1?…', out)
    return out


_SENSITIVE_PARAM_KEYS = [
    "token", "secret", "password", "passwd", "api_key", "apikey",
    "auth", "credential", "bearer",
]


def _is_sensitive_param_key(key: str) -> bool:
    """
    如果参数键名看起来携带密钥，则返回 `True`。
    由 [`summarize_params`] 用于在通用回退中跳过其值不应出现在调试表面中的键。

    引擎 crate 无法查询主机的 `Tool::sensitive_params()`（该 trait 位于主
    `ironclaw` crate 中），因此此拒绝列表是对未知/MCP 工具的最佳努力防御。
    已知工具在上面获得每工具提取（url/query/command/等），永远不会命中此路径。
    """
    lower = key.lower()
    if lower == "key" or lower.endswith("_key"):
        return True
    return any(needle in lower for needle in _SENSITIVE_PARAM_KEYS)


def _truncate(s: str, max_len: int) -> str:
    """将字符串截断到指定长度，在安全边界处添加省略号。"""
    if len(s) <= max_len:
        return s
    # 找到安全的 UTF-8 边界
    end = min(max_len, len(s))
    while end > 0:
        try:
            s[:end].encode('utf-8')
            break
        except UnicodeEncodeError:
            end -= 1
    return f"{s[:end]}..."


# ── 事件 ──────────────────────────────────────────────────

@dataclass(frozen=True)
class EventId:
    """强类型事件标识符。"""
    value: uuid.UUID = field(default_factory=uuid.uuid4)

    def __str__(self) -> str:
        return str(self.value)


class EventKind:
    """发生的事件的具体类型。"""
    pass


@dataclass
class EventKindStateChanged(EventKind):
    """线程状态变更。"""
    from_state: ThreadState
    to: ThreadState
    reason: Optional[str] = None


@dataclass
class EventKindStepStarted(EventKind):
    """步骤开始。"""
    step_id: StepId


@dataclass
class EventKindStepCompleted(EventKind):
    """步骤完成。"""
    step_id: StepId
    tokens: TokenUsage


@dataclass
class EventKindStepFailed(EventKind):
    """步骤失败。"""
    step_id: StepId
    error: str


@dataclass
class EventKindActionExecuted(EventKind):
    """操作执行成功。"""
    step_id: StepId
    action_name: str
    call_id: str
    duration_ms: int
    # 参数的简短人类可读摘要（例如，http 工具的 URL）
    params_summary: Optional[str] = None


@dataclass
class EventKindActionFailed(EventKind):
    """操作执行失败。"""
    step_id: StepId
    action_name: str
    call_id: str
    error: str
    duration_ms: int = 0
    # 参数的简短人类可读摘要
    params_summary: Optional[str] = None


@dataclass
class EventKindLeaseGranted(EventKind):
    """租约已授予。"""
    lease_id: LeaseId
    capability_name: str


@dataclass
class EventKindLeaseRevoked(EventKind):
    """租约已撤销。"""
    lease_id: LeaseId
    reason: str


@dataclass
class EventKindLeaseExpired(EventKind):
    """租约已过期。"""
    lease_id: LeaseId


@dataclass
class EventKindMessageAdded(EventKind):
    """消息已添加。"""
    role: str
    content_preview: str


@dataclass
class EventKindChildSpawned(EventKind):
    """子线程已生成。"""
    child_id: ThreadId
    goal: str


@dataclass
class EventKindChildCompleted(EventKind):
    """子线程已完成。"""
    child_id: ThreadId


@dataclass
class EventKindApprovalRequested(EventKind):
    """已请求批准。"""
    action_name: str
    call_id: str
    parameters: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    allow_always: Optional[bool] = None
    gate_name: Optional[str] = None
    params_summary: Optional[str] = None


@dataclass
class EventKindApprovalReceived(EventKind):
    """已收到批准。"""
    call_id: str
    approved: bool


@dataclass
class EventKindSelfImprovementStarted(EventKind):
    """自我改进已开始。"""
    pass


@dataclass
class EventKindSelfImprovementComplete(EventKind):
    """自我改进已完成。"""
    prompt_updated: bool
    patterns_added: int


@dataclass
class EventKindSelfImprovementFailed(EventKind):
    """自我改进失败。"""
    error: str


@dataclass
class EventKindSkillActivated(EventKind):
    """技能已激活。"""
    skill_names: List[str]


@dataclass
class EventKindCodeExecutionFailed(EventKind):
    """
    当代码（REPL）执行尝试失败时发出。启用代码执行失败模式的聚合分析，
    以确定运行时（Monty）、LLM 还是工具分派是失败的主要来源。
    """
    step_id: StepId
    # 分类的失败类别
    category: CodeExecutionFailure
    # 错误消息文本（截断到 500 字符）
    error: str
    # 被执行的 Python 代码的哈希，用于去重/关联
    code_hash: Optional[str] = None
    # 代码执行尝试的持续时间（毫秒）
    duration_ms: int = 0


@dataclass
class EventKindCodeExecuted(EventKind):
    """
    CodeAct 执行跟踪——原始代码 + stdout 保留给观察者（调试面板、跟踪重放）。
    上下文内聊天摘要太有损；此变体保留完整证据。
    """
    step_id: StepId
    code: str
    stdout: str
    return_value: Optional[Dict[str, Any]] = None
    duration_ms: int = 0


@dataclass
class EventKindOrchestratorRollback(EventKind):
    """编排器回滚。"""
    from_version: int
    to_version: int
    reason: str


@dataclass
class EventKindUnknown(EventKind):
    """
    未知事件类型——在滚动部署期间用于向前兼容的通用类型。
    反序列化由较新二进制文件写入的事件的较旧二进制文件将产生此变体而不是失败。
    """
    pass


@dataclass(kw_only=True)
class ThreadEvent:
    """线程执行历史中的记录事件。"""
    id: EventId = field(default_factory=EventId)
    thread_id: ThreadId
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    kind: EventKind
