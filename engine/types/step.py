"""
Step——线程内的执行单元

每个Step代表一次完整的LLM调用及其相关的工具/代码执行
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List

from metrics import TokenUsage

logger = logging.getLogger(__name__)


# ── 步骤状态 ─────────────────────────────────────────────────

class StepStatus(str, Enum):
    """步骤在其生命周期中的状态"""
    Pending = "Pending"
    LlmCalling = "LlmCalling"
    Executing = "Executing"
    Completed = "Completed"
    Failed = "Failed"


# ── 执行层 ───────────────────────────────────────────────────

class ExecutionTier(str, Enum):
    """哪个执行层处理步骤的代码/动作

    Monty 是唯一的 CodeAct/RLM 执行器。WASM 和 Docker 用于
    第三方工具隔离和线程沙箱（阶段 8），不用于运行 LLM 生成的 Python
    """
    # 结构化工具调用（来自 LLM 的 JSON 动作调用）
    Structured = "Structured"
    # 通过 Monty 执行的嵌入式 Python（CodeAct/RLM 模式）
    Scripting = "Scripting"


def generate_step_id() -> str:
    return str(uuid.uuid4())


@dataclass(kw_only=True)
class Step:
    """线程中的单个执行步骤"""
    _id: str = field(default_factory=generate_step_id, init=False)
    thread_id: str
    # 步骤序列号，从0开始递增
    sequence: int
    # 当前状态（Running/Completed/Failed等）
    status: StepStatus = StepStatus.Pending
    # 执行层级
    tier: ExecutionTier = ExecutionTier.Structured
    llm_response: Optional[LlmResponse] = None
    # 该step中所有action的结果
    action_results: List[ActionResult] = field(default_factory=list)
    tokens_used: TokenUsage = field(default_factory=TokenUsage)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None

    @property
    def id(self):
        return self._id


# TODO 放在合适的位置
# ── LLM 响应类型 ─────────────────────────────────────────────
class LlmResponse:
    """LLM 的响应：文本、动作调用或可执行代码"""
    pass


@dataclass
class LlmTextResponse(LlmResponse):
    """最终文本响应"""
    content: str


@dataclass
class LlmActionCallsResponse(LlmResponse):
    """一个或多个动作调用（附带可选的推理文本）"""
    calls: List[ActionCall]
    content: Optional[str] = None


@dataclass
class LlmCodeResponse(LlmResponse):
    """可执行的 Python 代码（CodeAct）。工具调用以函数调用的形式
    在代码中发生；运行时在每个调用处暂停并委托给 EffectExecutor
    """
    code: str
    content: Optional[str] = None


@dataclass
class ActionCall:
    """LLM 执行能力动作的请求"""
    # 唯一调用标识符（在结果中回显）
    id: str
    # 动作名称（例如 "web_fetch"、"create_issue"）
    action_name: str
    # 动作参数（JSON 格式）
    parameters: dict


@dataclass
class ActionResult:
    """执行能力动作的结果"""
    # 此结果对应的调用 ID
    call_id: str
    # 被执行的动作
    action_name: str
    # 输出值
    output: dict
    # 此结果是否表示错误
    is_error: bool
    # 动作执行耗时（毫秒）
    duration: int


# ── 代码执行失败分类 ─────────────────────────────────────────

class CodeExecutionFailure(str, Enum):
    """代码执行失败的分类

    由 instrumentation 层使用，用于区分 Monty VM 限制、
    LLM 逻辑错误、工具调度失败和资源耗尽。
    这些数据可以为运行时替代方案提供明智的决策
    """
    # Python 解析错误 — LLM 生成了无效语法
    SyntaxError = "syntax_error"
    # Python 运行时错误（NameError、TypeError、ValueError 等）—
    # LLM 逻辑错误或使用了不支持的特性
    RuntimeError = "runtime_error"
    # 名称查找失败 — 函数/变量不在作用域内且不是已知工具
    NameLookup = "name_lookup"
    # Monty VM 发生 panic（catch_unwind 捕获到）。表示 Monty 错误，
    # 而非用户代码问题
    VmPanic = "vm_panic"
    # 达到资源限制（超时、内存或分配上限）
    ResourceLimit = "resource_limit"
    # 代码内的工具调用返回了错误
    ToolError = "tool_error"
    # 尝试了 OS 操作（被沙箱阻止）
    OsDenied = "os_denied"
