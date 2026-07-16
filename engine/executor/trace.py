# 执行轨迹分析。
#
# 从已完成的 `Thread` 构建内存中的 `ExecutionTrace`，并运行回顾性分析器，标记常见失败模式。
# 由自我改进任务使用，并在调试日志中呈现。
#
# **没有独立的引擎轨迹文件。** 整个系统的实时轨迹记录由 `ironclaw_llm` 中的 `RecordingLlm` 处理
# （`crates/ironclaw_llm/src/recording.rs`），由 `IRONCLAW_RECORD_TRACE` 控制。
# 由于引擎的 `LlmBackend` 连接到同一个提供商链，引擎的大语言模型交互会被该单一记录器捕获——无需引擎端的环境变量，也不会产生第二个 JSON 文件。


import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List

from ..types.event import ThreadEvent
from ..types.thread import Thread, ThreadId, ThreadState

logger = logging.getLogger(__name__)


# ── 追踪数据结构 ─────────────────────────────────────────────

class IssueSeverity(Enum):
    """问题严重程度"""
    Error = "Error"
    Warning = "Warning"
    Info = "Info"


@dataclass
class MessageRecord:
    """追踪中带有角色标记的消息"""
    role: str
    content_length: int
    content_preview: str
    full_content: str
    action_name: Optional[str] = None
    action_call_id: Optional[str] = None


@dataclass
class DocRecord:
    """追踪中的单个文档记录"""
    doc_type: str
    title: str
    content: str


@dataclass
class TraceIssue:
    """回溯分析器检测到的问题"""
    severity: IssueSeverity
    category: str
    description: str
    step: Optional[int] = None


@dataclass
class ExecutionTrace:
    """单个线程的完整执行追踪"""
    thread_id: ThreadId
    goal: str
    final_state: ThreadState
    step_count: int
    total_tokens: int
    messages: List[MessageRecord]
    events: List[ThreadEvent]
    issues: List[TraceIssue]
    timestamp: datetime


# ── 构建追踪 ─────────────────────────────────────────────────

def build_trace(thread: Thread) -> ExecutionTrace:
    """从已完成的线程构建追踪"""
    messages = []
    for m in thread.messages:
        content = m.content
        preview = content[:300]
        messages.append(MessageRecord(
            role=str(m.role),
            content_length=len(content),
            content_preview=f"{preview}..." if len(content) > 300 else preview,
            full_content=content,
            action_name=m.action_name if hasattr(m, 'action_name') else None,
            action_call_id=m.action_call_id if hasattr(m, 'action_call_id') else None,
        ))

    issues = analyze_trace(thread)

    return ExecutionTrace(
        thread_id=thread.id,
        goal=thread.goal,
        final_state=thread.state,
        step_count=thread.step_count,
        total_tokens=thread.total_tokens_used,
        messages=messages,
        events=list(thread.events),
        issues=issues,
        timestamp=datetime.now(timezone.utc),
    )


def log_trace_summary(trace: ExecutionTrace) -> None:
    """将追踪摘要打印到日志"""
    logger.debug(
        f"=== 引擎 V2 追踪摘要 ===\n"
        f"  thread_id={trace.thread_id}\n"
        f"  goal={trace.goal}\n"
        f"  state={trace.final_state}\n"
        f"  steps={trace.step_count}\n"
        f"  tokens={trace.total_tokens}\n"
        f"  messages={len(trace.messages)}\n"
        f"  events={len(trace.events)}\n"
        f"  issues={len(trace.issues)}"
    )

    for issue in trace.issues:
        if issue.severity == IssueSeverity.Error:
            logger.debug(f"  问题: [{issue.category}] step={issue.step}: {issue.description}")
        elif issue.severity == IssueSeverity.Warning:
            logger.debug(f"  警告: [{issue.category}] step={issue.step}: {issue.description}")
        else:
            logger.debug(f"  备注: [{issue.category}] step={issue.step}: {issue.description}")


# ── 回溯分析 ─────────────────────────────────────────────────

def analyze_trace(thread: Thread) -> List[TraceIssue]:
    """分析已完成的线程以发现常见问题"""
    issues = []

    # 1. 检查线程是否失败
    if thread.state == ThreadState.Failed:
        issues.append(TraceIssue(
            severity=IssueSeverity.Error,
            category="thread_failure",
            description="线程以 Failed 状态结束",
            step=None,
        ))

    # 2. 检查空响应（没有 FINAL，没有有用的输出）
    has_assistant_response = any(
        m.role == MessageRole.Assistant and m.content
        for m in thread.messages
    )
    if not has_assistant_response:
        issues.append(TraceIssue(
            severity=IssueSeverity.Warning,
            category="no_response",
            description="线程中没有助手消息 — 模型可能未生成输出",
            step=None,
        ))

    # 3. 检查工具错误
    tool_errors = [
        e for e in thread.events
        if hasattr(e, 'kind') and isinstance(e.kind, EventKind) and e.kind.type == "ActionFailed"
    ]
    for event in tool_errors:
        if hasattr(event.kind, 'action_name') and hasattr(event.kind, 'error'):
            issues.append(TraceIssue(
                severity=IssueSeverity.Warning,
                category="tool_error",
                description=f"工具 '{event.kind.action_name}' 失败: {event.kind.error}",
                step=None,
            ))

    # 4. 通过结构化的 CodeExecutionFailed 事件检查代码执行错误。
    # 这些携带分类的失败类别，精确告诉我们发生了哪种错误
    # （语法、运行时、名称查找、VM 恐慌、资源限制、工具错误、OS 拒绝、门控暂停）
    code_failures = [
        e for e in thread.events
        if hasattr(e, 'kind') and isinstance(e.kind, EventKind) and e.kind.type == "CodeExecutionFailed"
    ]
    for event in code_failures:
        category = event.kind.category if hasattr(event.kind, 'category') else "unknown"
        error = event.kind.error if hasattr(event.kind, 'error') else ""
        preview = error[:200]

        severity = IssueSeverity.Error if category in ("VmPanic", "ResourceLimit") else IssueSeverity.Warning
        issues.append(TraceIssue(
            severity=severity,
            category=f"code_{category}",
            description=f"代码执行失败 ({category}): {preview}",
            step=None,
        ))

    # 回退：也检查消息级别的模式，以向后兼容在 CodeExecutionFailed instrumentation
    # 添加之前（PR #2483）运行的线程。注意：来自混合时代的线程（某些步骤有 instrumentation，
    # 某些没有）只会在存在任何结构化事件时报告结构化事件，静默跳过未 instrumentation 步骤的消息级别错误
    if not code_failures:
        error_patterns = [
            "NameError", "SyntaxError", "TypeError", "NotImplementedError",
            "ValueError", "AttributeError", "IndexError", "KeyError",
            "ModuleNotFoundError", "RuntimeError",
        ]
        for i, msg in enumerate(thread.messages):
            is_code_output = (
                    msg.role == MessageRole.UserProvenance
                    and (msg.content.startswith("[stdout]")
                         or msg.content.startswith("[stderr]")
                         or msg.content.startswith("[code ")
                         or msg.content.startswith("Traceback"))
            )
            if is_code_output and any(p in msg.content for p in error_patterns):
                preview = msg.content[:200]
                issues.append(TraceIssue(
                    severity=IssueSeverity.Warning,
                    category="code_error",
                    description=f"消息 {i} 中的代码执行错误: {preview}",
                    step=None,
                ))

    # 5. 检查 ActionResult 消息上的空 call_id（导致 LLM API 拒绝）
    for i, msg in enumerate(thread.messages):
        if msg.role == MessageRole.ActionResult:
            call_id = msg.action_call_id if hasattr(msg, 'action_call_id') else None
            if not call_id:
                action_name = msg.action_name if hasattr(msg, 'action_name') else "unknown"
                issues.append(TraceIssue(
                    severity=IssueSeverity.Error,
                    category="empty_call_id",
                    description=f"ActionResult 消息 {i}（工具 '{action_name}'）具有空的 call_id — 将导致 LLM API 拒绝",
                    step=None,
                ))

    # 6. 检查模型忽略工具结果（幻觉风险）。
    # 在 Tier 0（结构化）中，结果显示为 ActionResult 消息。
    # 在 Tier 1（CodeAct）中，结果显示为带有 "[tool result]" 前缀的 User 消息
    has_tool_results = any(
        m.role == MessageRole.ActionResult for m in thread.messages
    )
    has_tool_output_in_context = any(
        m.role == MessageRole.UserProvenance and (" result]" in m.content or " error]" in m.content)
        for m in thread.messages
    )
    if has_tool_results and not has_tool_output_in_context:
        issues.append(TraceIssue(
            severity=IssueSeverity.Warning,
            category="missing_tool_output",
            description="工具结果存在但消息中没有工具输出 — 模型可能看不到工具结果",
            step=None,
        ))

    # 7. 检查过度迭代
    if thread.step_count > 10:
        issues.append(TraceIssue(
            severity=IssueSeverity.Warning,
            category="excessive_steps",
            description=f"线程花费了 {thread.step_count} 步 — 可能陷入循环",
            step=None,
        ))

    # 8. 检查没有 FINAL 的文本响应（模型从记忆回答）
    has_action_executed = any(
        hasattr(e, 'kind') and isinstance(e.kind, EventKind) and e.kind.type == "ActionExecuted"
        for e in thread.events
    )
    if not has_action_executed and thread.step_count == 1 and has_assistant_response:
        issues.append(TraceIssue(
            severity=IssueSeverity.Info,
            category="no_tools_used",
            description="模型在一步中回答而未使用任何工具 — 可能从训练数据回答",
            step=1,
        ))

    # 9. 检查 LLM 未生成代码块
    code_steps = sum(
        1 for e in thread.events
        if hasattr(e, 'kind') and isinstance(e.kind, EventKind) and e.kind.type == "StepStarted"
    )
    text_responses_without_code = sum(
        1 for m in thread.messages
        if m.role == MessageRole.Assistant
        and "```" not in m.content
        and "FINAL(" not in m.content
    )
    if text_responses_without_code > 0 and code_steps > 0:
        issues.append(TraceIssue(
            severity=IssueSeverity.Info,
            category="mixed_mode",
            description=f"{text_responses_without_code} 条文本响应没有代码块 — 模型可能未遵循 CodeAct 提示",
            step=None,
        ))

    # 10. 从 StateChanged → Failed 事件中提取失败原因
    for event in thread.events:
        if (hasattr(event, 'kind')
                and isinstance(event.kind, EventKind)
                and event.kind.type == "StateChanged"):
            if (event.kind.to == ThreadState.Failed
                    and hasattr(event.kind, 'reason')
                    and event.kind.reason):
                reason = event.kind.reason
                if "LLM" in reason or "Provider" in reason:
                    issues.append(TraceIssue(
                        severity=IssueSeverity.Error,
                        category="llm_error",
                        description=f"LLM 提供者错误: {_truncate(reason, 300)}",
                        step=None,
                    ))
                elif "orchestrator" in reason:
                    issues.append(TraceIssue(
                        severity=IssueSeverity.Error,
                        category="orchestrator_error",
                        description=f"编排器错误: {_truncate(reason, 300)}",
                        step=None,
                    ))

    return issues


def _truncate(s: str, max_chars: int) -> str:
    """截断字符串到指定字符数"""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "..."
