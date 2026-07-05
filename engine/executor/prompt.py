import logging
import os
from typing import List, Optional

from ironclaw_common import PlatformInfo
from ..traits.store import Store
from ..types.capability import (
    ActionDef,
    CapabilityStatus,
    CapabilitySummary,
    CapabilitySummaryKind,
    ModelToolSurface
)
from ..types.memory import MemoryDoc
from ..types.message import MessageRole, ThreadMessage
from ..types.project import ProjectId

logger = logging.getLogger(__file__)

# ── 提示词常量 ───────────────────────────────────────────────

# 工具列表之前的主要指令块
# "../../prompts/codeact_preamble.md"
CODEACT_PREAMBLE = """你是一个 AI 助手，拥有 Python REPL 环境。

## 执行模式

- 在 ```repl 代码块中编写并运行 Python 来解决任务
- 将工具作为常规 Python 函数调用（例如 `web_search("query")`）
- 使用 `FINAL(answer)` 返回最终答案
- 使用 `llm_query(prompt, context)` 进行子代理调用
- 通过 `context` 变量访问线程上下文
- 除非绝对必要，否则避免在最终输出中使用 Markdown 格式
"""

# 在动态元数据部分之后追加的策略/结束块
# "../../prompts/codeact_postamble.md"
CODEACT_POSTAMBLE = """
## 策略

- 在采取行动之前先阅读上下文：检查 `context` 变量中的目标、相关知识和活跃技能
- 一次只调用一个工具；等待结果后再继续
- 如果工具失败，分析错误并尝试替代方案
- 在编写代码之前，使用 `tool_info(name="<tool>", detail="schema")` 获取工具的模式信息
- 使用 `memory_write` 保存应跨线程持久化的有价值的发现、经验教训或规范
- 当你有了最终答案时，使用 `FINAL(answer)` — 不要只是在文本中描述它

## 安全约束

- 永远不要尝试访问主机文件系统或执行操作系统命令
- 在沙箱外永远不要进行网络请求（使用 `web_fetch`、`http` 等工具）
- 使用 `require_approval(reason)` 处理破坏性操作
"""

# 设置 IRONCLAW_DISABLE_CODEACT 时使用的仅结构化工具前言
STRUCTURED_TOOL_PREAMBLE = """你是 IronClaw，一个个人 AI 助手。

## 执行模式

对每个动作使用提供者的结构化 tool_calls 接口。
不要输出 Python、repl、py 或其他可执行的围栏代码块。
不要将工具作为 Python 函数调用。
不要在助手文本中编写工具调用。永远不要输出 `[[call_tool ...]]`、`<tool_call>`、`<function_call>`、JSON 工具调用块或函数式调用，如 `tool_name(...)`。
只有提供者级别的 `tool_calls` 字段可以调用工具。如果你需要工具，返回一个结构化的工具调用，而不是描述或打印调用。
当不需要动作时，用纯文本回答。
"""

# 设置 IRONCLAW_DISABLE_CODEACT 时使用的仅结构化工具后言
STRUCTURED_TOOL_POSTAMBLE = """
## 策略

当你需要数据、持久化、外部效果或系统状态时使用结构化工具调用。
工具结果可用后，继续执行另一个结构化工具调用或返回最终的纯文本答案。
某些集成在面向用户的最终文本中使用字面 UI 块，如 `[[choice_set]]...[[/choice_set]]`。这些仅是 UI 标记；不要发明其他括号控制块，特别是 `[[call_tool ...]]`。
"""

# 引擎拥有的 CodeAct 系统提示标记
CODEACT_SYSTEM_PROMPT_MARKER = "<!-- ironclaw:codeact-system-prompt -->\n"
CODEACT_LEGACY_OPENING = "你是一个拥有 Python REPL 环境的 AI 助手。"
CODEACT_STRATEGY_HEADING = "\n## 策略\n"
CODEACT_CAPABILITIES_HEADING = "\n## 可用能力（后台状态）\n"
CODEACT_BACKGROUND_CAPABILITIES_HEADING = "\n## 能力\n"
CODEACT_ENABLED_TOOLS_HEADING = "\n## 已启用的工具\n"
CODEACT_ACTIVATABLE_INTEGRATIONS_HEADING = "\n## 可激活的集成\n"
PRIOR_KNOWLEDGE_HEADING = "\n\n## 先前知识（来自已完成的线程）\n"
ACTIVE_SKILLS_HEADING = "\n\n## 活跃技能\n"
MISSING_SKILLS_PREFIX = "\n\n用户明确请求了未安装或未找到的斜杠技能："

# CodeAct 前言覆盖的知名标题
PREAMBLE_OVERLAY_TITLE = "prompt:codeact_preamble"

# 提示覆盖文档的知名标签
PROMPT_OVERLAY_TAG = "prompt_overlay"

# 提示覆盖文档的最大大小（字符数）
MAX_PROMPT_OVERLAY_CHARS = 4000


def codeact_disabled() -> bool:
    """通过环境变量检查 CodeAct（第 1 层 Python 执行）是否被禁用"""
    val = os.environ.get("IRONCLAW_DISABLE_CODEACT", "")
    return val in ("true", "1")


def is_codeact_system_prompt(content: str) -> bool:
    """检查内容是否为 CodeAct 系统提示"""
    return content.startswith(CODEACT_SYSTEM_PROMPT_MARKER) or is_legacy_codeact_system_prompt(content)


def is_legacy_codeact_system_prompt(content: str) -> bool:
    """检查内容是否为旧版 CodeAct 系统提示"""
    return (content.startswith(CODEACT_LEGACY_OPENING)
            and "```repl" in content
            and (CODEACT_STRATEGY_HEADING in content
                 or CODEACT_CAPABILITIES_HEADING in content))


def codeact_system_prompt_suffix(existing_content: str) -> Optional[str]:
    """提取 CodeAct 系统提示的后缀部分"""
    append_markers = [
        PRIOR_KNOWLEDGE_HEADING,
        ACTIVE_SKILLS_HEADING,
        MISSING_SKILLS_PREFIX,
    ]

    suffix_start = None
    for marker in append_markers:
        idx = existing_content.find(marker)
        if idx != -1:
            if suffix_start is None or idx < suffix_start:
                suffix_start = idx

    if suffix_start is None:
        postamble_idx = existing_content.rfind(CODEACT_POSTAMBLE)
        if postamble_idx != -1:
            suffix_start = postamble_idx + len(CODEACT_POSTAMBLE)

    if suffix_start is not None:
        return existing_content[suffix_start:]
    return None


def refresh_codeact_system_prompt(existing_content: str, system_prompt: str) -> str:
    """刷新 CodeAct 系统提示：保留后缀，替换前缀"""
    if not is_codeact_system_prompt(existing_content):
        return system_prompt

    suffix = codeact_system_prompt_suffix(existing_content) or ""

    if not suffix:
        return system_prompt
    else:
        return system_prompt + suffix


def upsert_codeact_system_prompt(
        messages: List[ThreadMessage],
        system_prompt: str,
) -> bool:
    """在消息列表中插入或更新 CodeAct 系统提示。返回是否发生了更改"""
    # 查找现有的 CodeAct 系统消息
    for message in messages:
        if message.role == MessageRole.System and is_codeact_system_prompt(message.content):
            refreshed = refresh_codeact_system_prompt(message.content, system_prompt)
            if message.content == refreshed:
                return False
            message.content = refreshed
            return True

    # 如果已存在其他系统消息，不插入
    for message in messages:
        if message.role == MessageRole.System:
            return False

    # 前置系统提示
    messages.insert(0, ThreadMessage.system(system_prompt))
    return True


def capability_status_label(status: CapabilityStatus) -> str:
    """返回能力状态的标签"""
    labels = {
        CapabilityStatus.Ready: "就绪",
        CapabilityStatus.ReadyScoped: "就绪（已限定范围）",
        CapabilityStatus.NeedsAuth: "需要认证",
        CapabilityStatus.NeedsSetup: "需要设置",
        CapabilityStatus.Inactive: "未激活",
        CapabilityStatus.Latent: "潜在",
        CapabilityStatus.Error: "错误",
        CapabilityStatus.AvailableNotInstalled: "可用但未安装",
    }
    return labels.get(status, "未知")


def capability_kind_label(kind: CapabilitySummaryKind) -> str:
    """返回能力类型的标签"""
    labels = {
        CapabilitySummaryKind.Channel: "频道",
        CapabilitySummaryKind.Provider: "提供者",
        CapabilitySummaryKind.Runtime: "运行时",
    }
    return labels.get(kind, "未知")


def is_activatable_integration(capability: CapabilitySummary) -> bool:
    """检查能力是否为可激活的集成

    NeedsAuth 故意不在此处：在 #3133 之后，已安装但未认证的提供者工具
    可以直接调用（引擎的认证预检查在执行时引发 Authentication 门控），
    因此它们位于常规动作清单中，而非单独的"需要设置"部分
    """
    return (capability.kind in (CapabilitySummaryKind.Provider, CapabilitySummaryKind.Channel)
            and capability.status in (
                CapabilityStatus.NeedsSetup,
                CapabilityStatus.Inactive,
                CapabilityStatus.Latent,
                CapabilityStatus.AvailableNotInstalled,
            ))


def render_background_capability(capability: CapabilitySummary) -> str:
    """渲染后台能力条目"""
    line = f"- `{capability.name}` [{capability_kind_label(capability.kind)}] — {capability_status_label(capability.status)}"

    if capability.display_name and capability.display_name != capability.name:
        line += f" ({capability.display_name})"
    if capability.routing_hint:
        line += f"。{capability.routing_hint}"
    if capability.description:
        line += f"。{capability.description}"
    line += "\n"
    return line


def compact_prompt_description(description: str) -> str:
    """压缩描述：将多个空白字符替换为单个空格"""
    return " ".join(description.split())


def render_enabled_tool(action: ActionDef) -> str:
    """渲染已启用的工具条目"""
    return f"- `{action.discovery_name}` — {compact_prompt_description(action.description)}\n"


def format_action_preview(actions: List[str]) -> str:
    """格式化动作预览列表，最多显示 3 个"""
    MAX_PREVIEW = 3

    rendered = [f"`{action}`" for action in actions[:MAX_PREVIEW]]
    if len(actions) > MAX_PREVIEW:
        rendered.append(f"+{len(actions) - MAX_PREVIEW} 更多")
    return "、".join(rendered)


def render_activatable_integration(capability: CapabilitySummary) -> str:
    """渲染可激活的集成条目"""
    line = f"- `{capability.name}` [{capability_kind_label(capability.kind)}]"

    if capability.display_name and capability.display_name != capability.name:
        line += f" ({capability.display_name})"
    if capability.description:
        line += f" — {capability.description}"
    if capability.action_preview:
        line += f"。解锁：{format_action_preview(capability.action_preview)}"
    line += "\n"
    return line


def extract_prompt_overlay(docs: List[MemoryDoc]) -> Optional[str]:
    """从预获取的系统记忆文档列表中提取提示覆盖"""
    overlay = None
    for doc in docs:
        if doc.title == PREAMBLE_OVERLAY_TITLE and PROMPT_OVERLAY_TAG in doc.tags:
            overlay = doc
            break

    if overlay is None:
        return None

    content = overlay.content[:MAX_PROMPT_OVERLAY_CHARS]
    if not content:
        return None
    return content


async def load_prompt_overlay(store: Store, project_id: ProjectId) -> Optional[str]:
    """从 Store 加载提示覆盖（如果该项目存在）"""
    try:
        docs = await store.list_shared_memory_docs(project_id)
        return extract_prompt_overlay(docs)
    except Exception:
        return None


async def build_codeact_system_prompt(
        capabilities: List[CapabilitySummary],
        compact_actions: List[ActionDef],
        store: Optional[Store] = None,
        project_id: Optional[ProjectId] = None,
        platform: Optional[PlatformInfo] = None,
) -> str:
    """为 CodeAct/RLM 执行构建系统提示

    提示指示 LLM：
    - 在 ```repl 围栏代码块中编写 Python 代码
    - 将工具作为常规 Python 函数调用
    - 使用 llm_query(prompt, context) 进行子代理调用
    - 使用 FINAL(answer) 返回最终答案
    - 通过 `context` 变量访问线程上下文

    如果提供了 Store，检查运行时提示覆盖（带有标签 "prompt_overlay" 和
    标题 "prompt:codeact_preamble" 的 MemoryDoc），并在编译的前言后追加
    其内容。这使自我改进任务能够在运行时演进系统提示
    """
    overlay = None
    if store is not None and project_id is not None:
        overlay = await load_prompt_overlay(store, project_id)

    return build_codeact_system_prompt_inner(
        codeact_disabled(),
        capabilities,
        compact_actions,
        overlay,
        platform,
    )


def build_codeact_system_prompt_with_docs(
        capabilities: List[CapabilitySummary],
        compact_actions: List[ActionDef],
        system_docs: List[MemoryDoc],
        platform: Optional[PlatformInfo] = None,
) -> str:
    """使用预获取的记忆文档构建系统提示

    当调用者已经拥有 `list_memory_docs` 的结果时（例如因为
    `load_orchestrator` 已获取），将文档传入此处以避免重复的 Store 查询
    """
    overlay = extract_prompt_overlay(system_docs)
    return build_codeact_system_prompt_inner(
        codeact_disabled(),
        capabilities,
        compact_actions,
        overlay,
        platform,
    )


def build_codeact_system_prompt_inner(
        disable_codeact: bool,
        capabilities: List[CapabilitySummary],
        compact_actions: List[ActionDef],
        overlay: Optional[str] = None,
        platform: Optional[PlatformInfo] = None,
) -> str:
    """共享的提示构建器，由异步和预获取文档变体共同使用

    `disable_codeact` 作为显式参数传入（而不是直接从环境变量读取），
    这样测试可以在没有进程级环境变量变更的情况下练习两个分支
    """
    logger.debug(f"引擎 v2 提示模式: codeact_disabled={disable_codeact}")

    if disable_codeact:
        # 前言
        preamble = STRUCTURED_TOOL_PREAMBLE
        # 后记
        postamble = STRUCTURED_TOOL_POSTAMBLE
    else:
        preamble = CODEACT_PREAMBLE
        postamble = CODEACT_POSTAMBLE

    prompt = CODEACT_SYSTEM_PROMPT_MARKER + preamble

    # 注入平台标识和运行时元数据
    if platform is not None:
        prompt += platform.to_prompt_section()

    # 如果可用，追加运行时提示覆盖
    if overlay is not None:
        prompt += "\n\n## 学到的规则（来自自我改进）\n\n"
        prompt += overlay

    # 分区能力和可激活集成
    activatable_integrations = []
    background_capabilities = []
    for capability in capabilities:
        if is_activatable_integration(capability):
            activatable_integrations.append(capability)
        else:
            background_capabilities.append(capability)

    if background_capabilities:
        prompt += CODEACT_BACKGROUND_CAPABILITIES_HEADING + "\n"
        for capability in background_capabilities:
            prompt += render_background_capability(capability)

    # 在禁用 CodeAct 模式下，"已启用的工具"列表被省略：
    # 紧凑动作被发送到提供者工具列表（参见 `LlmBridgeAdapter::complete`）
    # 并带有其完整模式，因此提示只会重复该表面，而 `tool_info` 模式查找
    # 指令将不适用。没有此保护，紧凑工具过去会在提示中显示为"可用"，
    # 但从未进入 `tool_calls`，使它们实际上无法访问（PR #3665 审查）
    if not disable_codeact:
        compact_tools = [
            action for action in compact_actions
            if action.model_tool_surface == ModelToolSurface.CompactToolInfo
        ]

        if compact_tools:
            prompt += CODEACT_ENABLED_TOOLS_HEADING + "\n"
            prompt += (
                "这些已启用的工具以紧凑形式显示。在调用之前，"
                "始终使用 `tool_info(name=\"<工具>\", detail=\"schema\")` 检查其模式。\n\n"
            )
            for action in compact_tools:
                prompt += render_enabled_tool(action)

    if activatable_integrations:
        prompt += CODEACT_ACTIVATABLE_INTEGRATIONS_HEADING + "\n"
        prompt += (
            "这些集成需要用户设置才能使其工具变为可调用。"
            "当用户要求连接/安装/启用其中某个时，直接调用 "
            "`tool_install(name=\"<名称>\")` — 不要列举替代方案或描述手动 UI 步骤。"
            "如果凭据缺失，引擎在执行时引发认证门控并在聊天中提示用户。"
            "安装前如需参数详情，在预览工具上调用 "
            "`tool_info(name=\"<工具>\", detail=\"summary\")`。\n\n"
        )
        for capability in activatable_integrations:
            prompt += render_activatable_integration(capability)

    prompt += postamble
    return prompt
