from __future__ import annotations

from ironclaw_engine import (
    Capability, CapabilityRegistry, ConversationManager, EffectExecutor, LeaseManager,
    MissionManager, PolicyEngine, Project, Store, ThreadConfig, ThreadManager, ThreadOutcome,
)

from ironclaw_common import AppEvent
from ironclaw_engine.types import (is_shared_owner, shared_owner_id)

from agent import Agent, augment_with_attachments
from auth.extension import AuthManager
from bridge.effect_adapter import EffectBridgeAdapter
from bridge.engine_actions import mission_capability_actions
from bridge.llm_adapter import LlmBridgeAdapter
from bridge.store_adapter import HybridStore
from channels.web import GATEWAY_CHANNEL_NAME
from channels.web.sse import SseManager
from channels import (IncomingMessage, OutgoingResponse, StatusUpdate)
from db import Database
from error import Error
from extensions.naming import legacy_extension_alias
from gate.pending import (PendingGate, PendingGateKey)
from gate.store import PendingGateStore
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Set, Optional, Union, Any
import os
import regex as re
from datetime import datetime, timezone
from pathlib import Path
import uuid
import json
import asyncio
from engine import (ResumeKind)
from llm import user_signals_execution_intent

logger = logging.getLogger(__name__)


@dataclass
class BridgeRespondOutcome:
    text: str


class BridgeNoResponseOutcome:
    pass


class BridgePendingOutcome:
    pass


# 来自 v2 桥接处理程序的类型化结果。
#
# 替代了模糊的 `Option<String>`，其中 `None` 可能表示“网关已创建，轮次暂停”或“已完成，但无文本响应”。
# 现在每个变体都明确编码了处理程序的意图。
BridgeOutcome = BridgeRespondOutcome | BridgeNoResponseOutcome | BridgePendingOutcome


def is_engine_v2_enabled() -> bool:
    """通过 `ENGINE_V2=true` 环境变量检查引擎 v2 是否启用。"""
    value = os.environ.get("ENGINE_V2", "")
    return value == "true" or value == "1"


def engine_err(context: str, e: Any) -> Exception:
    """从引擎相关失败构建 `Error` 的简写。"""
    return Exception(f"engine v2 {context}: {e}")


def bridge_outcome_for_failed_thread(
        error: str,
        debug_detail: Optional[str],
        user_id: str,
        channel: str,
        sse_will_deliver_to_user: bool,
) -> BridgeOutcome:
    """
    为 `ThreadOutcome::Failed` 构建 `BridgeOutcome`。

    原始引擎故障可能包含 Python 回溯、内部文件路径和上游 HTTP 响应体
    （参见 #2546）。此辅助函数将原始错误保留在服务器端日志中，
    并根据错误的形状返回简短的、面向用户的摘要。

    `sse_will_deliver_to_user` 表示调用方已经通过每用户 SSE 流广播了
    `AppEvent::Error`，原始通道将其渲染给用户（目前：Web 网关）。
    在这种情况下，返回 `Respond(sanitized)` 会重复渲染同一个失败的回合——
    一次作为 SSE 错误卡片，再次作为 `GatewayChannel::respond()` 发出的
    正常 `response` 帧。设置时我们返回 `NoResponse`；否则返回
    `Respond(sanitized)`，以便没有 SSE 作为主要显示面的通道
    （telegram、relay、cli）仍然向用户传递经过清理的故障信息。

    提取为命名函数，以便清理流程（日志 + 映射到用户友好文本 +
    包装在 `BridgeOutcome` 中）可以通过单元测试端到端地执行，
    而无需启动完整的引擎。
    """
    # `warning` 仅携带 `debug_detail` 的大小，而非其内容——
    # 完整的 Python 回溯或上游 HTTP 响应体可能是数 KB，
    # 并会用已在 `debug` 级别可用的内部文本淹没更高级别的日志。
    # 需要完整详细信息的操作员可以设置 `RUST_LOG=ironclaw::bridge::router=debug`。
    # 聊天回复根据 `.claude/rules/error-handling.md` 保持清理状态，
    # 并且 `debug_detail` 故意不在 SSE `error` 事件上广播——
    # 该有效载荷会到达每个已认证的消费者。
    debug_detail_bytes = len(debug_detail) if debug_detail else None
    logger.warning(
        "engine v2: 线程失败；显示用户友好的摘要, user_id=%s, channel=%s, error=%s, debug_detail_bytes=%s",
        user_id,
        channel,
        error,
        debug_detail_bytes,
    )
    if debug_detail is not None:
        logger.debug(
            "engine v2: 线程失败调试详情, user_id=%s, channel=%s, detail=%s",
            user_id,
            channel,
            debug_detail,
        )
    if sse_will_deliver_to_user:
        return BridgeNoResponseOutcome()
    else:
        return BridgeRespondOutcome(user_facing_thread_failure(error))


PROJECT_ATTACHMENT_DIR: str = ".ironclaw/attachments"


@dataclass
class AttachmentIndexNote:
    """附件索引笔记。"""
    title: str
    content: str
    metadata: Dict[str, Any]
    tags: List[str]


def sanitize_attachment_segment(raw: str) -> str:
    """
    清理附件路径片段，仅保留 ASCII 字母数字、点、短划线和下划线，
    其他字符替换为下划线。
    """
    sanitized = re.sub(r'[^a-zA-Z0-9.\-_]', '_', raw)
    sanitized = sanitized.strip('.')
    if not sanitized:
        return "attachment"
    return sanitized


def fallback_attachment_filename(index: int, mime_type: str) -> str:
    """当没有原始文件名时，生成回退的附件文件名。"""
    ext = attachment_extension_for_mime(mime_type)
    return f"attachment-{index + 1}.{ext}"


def attachment_project_relative_path(
        message: IncomingMessage,
        project_id: ProjectId,
        attachment: IncomingAttachment,
        index: int,
) -> str:
    """生成附件在项目中的相对路径。"""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    owner = sanitize_attachment_segment(message.user_id)
    message_id = sanitize_attachment_segment(str(message.id))
    filename = (
        sanitize_attachment_segment(attachment.filename)
        if attachment.filename
        else fallback_attachment_filename(index, attachment.mime_type)
    )
    return f"{PROJECT_ATTACHMENT_DIR}/{owner}/{project_id}/{date}/{message_id}-{filename}"


def sanitize_filename_for_display(raw: str) -> str:
    """
    在嵌入用户提供的文件名之前，折叠任何可能破坏 markdown 标题/反引号范围的字符。
    附件文件名中的用户内容直接进入 `# Uploaded attachment: ...` 和笔记的 `title`，
    因此原始的换行符 / 反引号 / 奇怪的 ASCII 控制码会破坏代理可见的转录
    （并且对于标题，会破坏可搜索的内存文档行）。
    """
    out: List[str] = []
    for ch in raw:
        if ch in ('\n', '\r', '\t'):
            out.append(' ')
        elif ch == '`':
            out.append('\'')
        elif ch.isprintable() and not (ord(ch) < 32 and ch not in ('\n', '\r', '\t')):
            out.append(ch)
        # 控制字符被跳过
    trimmed = ''.join(out).strip()
    if not trimmed:
        return "attachment"
    # 限制长度，防止病态文件名淹没代理提示
    MAX_DISPLAY_LEN = 256
    if len(trimmed) <= MAX_DISPLAY_LEN:
        return trimmed
    else:
        return trimmed[:MAX_DISPLAY_LEN]


def attachment_index_note(
        message: IncomingMessage,
        attachment: IncomingAttachment,
        relative_path: str,
) -> AttachmentIndexNote:
    """为附件生成索引笔记。"""
    raw_filename = attachment.filename if attachment.filename else "attachment"
    filename = sanitize_filename_for_display(raw_filename)
    attachment_type = attachment.kind.value if hasattr(attachment.kind, 'value') else str(attachment.kind)

    size = attachment.size_bytes if attachment.size_bytes is not None else len(attachment.data)
    content = (
        f"# 上传的附件: {filename}\n\n"
        f"- 项目文件: `{relative_path}`\n"
        f"- 附件类型: `{attachment_type}`\n"
        f"- MIME 类型: `{attachment.mime_type}`\n"
        f"- 大小: `{size}` 字节\n"
        f"- 上传者: `{message.user_id}` 通过 `{message.channel}`\n"
    )

    if attachment.kind == AttachmentKind.Audio:
        if attachment.extracted_text:
            content += "\n## 转录文本\n\n"
            content += attachment.extracted_text
        else:
            content += "\n转录文本不可用。原始音频文件存储在上述项目文件路径中。"
    elif attachment.kind == AttachmentKind.Image:
        content += (
            "\n原始图像文件存储在上述项目文件路径中。"
            "如果需要，在后续的 shell 或技能命令中使用该文件路径。"
        )
    elif attachment.kind == AttachmentKind.Document:
        if attachment.extracted_text:
            content += "\n## 提取的文本\n\n"
            content += attachment.extracted_text
        else:
            content += "\n文本提取不可用。原始文档文件存储在上述项目文件路径中。"

    return AttachmentIndexNote(
        title=f"attachment:{filename}",
        content=content,
        metadata={
            "kind": "project_attachment",
            "attachment_type": attachment_type,
            "filename": filename,
            "mime_type": attachment.mime_type,
            "project_path": relative_path,
            "message_id": str(message.id),
        },
        tags=["attachment", "upload", attachment_type],
    )


async def persist_project_attachments(
        project_root: Path,
        message: IncomingMessage,
        project_id: ProjectId,
        attachments: List[IncomingAttachment],
) -> List[AttachmentIndexNote]:
    """将附件持久化到项目目录，并返回索引笔记列表。"""
    notes: List[AttachmentIndexNote] = []

    for index, attachment in enumerate(attachments):
        if not attachment.data or attachment.local_path is not None:
            continue

        relative_path = attachment_project_relative_path(
            message, project_id, attachment, index
        )
        absolute_path = project_root / Path(relative_path)
        parent = absolute_path.parent

        try:
            parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(
                "engine v2: 创建附件目录失败, path=%s, error=%s",
                parent,
                e,
            )
            continue

        try:
            absolute_path.write_bytes(attachment.data)
        except Exception as e:
            logger.warning(
                "engine v2: 持久化附件文件失败, path=%s, error=%s",
                absolute_path,
                e,
            )
            continue

        attachment.local_path = relative_path
        # 在 `data` 仍然填充时构建索引笔记，以便
        # `attachment_index_note` 中回退到 `data.len()` 时
        # 报告当 `size_bytes` 未预填充时的真实有效载荷大小。
        notes.append(attachment_index_note(message, attachment, relative_path))
        # 故意不清除 `attachment.data`。调用者
        # 立即将相同的列表提供给 `augment_with_attachments`，
        # 后者仅在 `att.data` 非空时发出多模态 `image_parts`。
        # 在此处清除缓冲区会静默地从 engine-v2 LLM 请求中
        # 丢弃每个上传的图像——文件在磁盘上，但模型永远不会看到字节。
        # `attachments` 列表是请求本地的，在引擎调度返回后即被丢弃，
        # 因此"存储清理"无论如何都是空操作。

    return notes


def resolve_project_root() -> Path:
    """解析项目根目录。"""
    base_dir = ironclaw_base_dir()
    return base_dir.parent if base_dir.parent else base_dir


async def save_attachment_index_notes(
        store: Store,
        project_id: ProjectId,
        user_id: str,
        thread_id: ThreadId,
        notes: List[AttachmentIndexNote],
) -> None:
    """将附件索引笔记保存到存储中。"""
    for note in notes:
        doc = MemoryDoc(
            project_id=project_id,
            user_id=user_id,
            doc_type=DocType.Note,
            title=note.title,
            content=note.content,
        )
        doc.metadata = note.metadata
        doc.tags = note.tags
        doc.source_thread_id = thread_id
        try:
            await store.save_memory_doc(doc)
        except Exception as e:
            logger.warning(
                "engine v2: 保存附件索引笔记失败, error=%s, title=%s",
                e,
                doc.title,
            )


def gate_display_parameters(pending: PendingGate) -> Dict[str, Any]:
    """
    获取门控的显示参数，优先使用 display_parameters，
    回退到原始 parameters。
    """
    if pending.display_parameters is not None:
        return pending.display_parameters
    return pending.parameters


async def resolve_extension_for_action(
        auth_manager: Optional[AuthManager],
        extension_manager: Optional[ExtensionManager],
        tools: ToolRegistry,
        action_name: str,
        parameters: Dict[str, Any],
        credential_fallback: str,
        user_id: str,
) -> ExtensionName:
    """
    解析工具操作的所有者扩展名称，当操作不由扩展支持时，
    回退到凭证名称。这是认证门控显示 + 提交路由逻辑的共享核心——
    相同的 `provider_extension_for_tool + unwrap_or_else(credential_name)` 模式
    之前在此文件中的三个不同位置触发，每个都有相同的回退理由：
    引擎的 `ResumeKind::Authentication` 仅携带 `credential_name`
    （例如 `google_oauth_token`），这对用户来说是不透明的，
    并且在反馈给 `submit_auth_token` 用于 WASM 工具支持的凭证时会失败，
    而所有者扩展名称（例如 `google-drive-tool`）才是面向用户的 UI
    和 `submit_auth_token` 实际需要的。对于内置工具、HTTP 和技能凭证，
    没有所有者扩展，回退是正确的选择。
    """
    if auth_manager is not None:
        # 解析器在用户影响的分支上强制执行身份验证，
        # 并直接返回类型化的 `ExtensionName`——无需包装。
        return await auth_manager.resolve_extension_name_for_auth_flow(
            action_name,
            parameters,
            credential_fallback,
            user_id,
        )

    # 无认证管理器（没有 SECRETS_MASTER_KEY 的托管实例，或裸测试夹具）：
    # 委托给与 auth-manager 路径使用的相同规范解析器，
    # 以便优先级中的 extension-manager 分支仍然运行，
    # 而不是回退到字符串形式的凭证名称。
    return await resolve_auth_flow_extension_name(
        action_name,
        parameters,
        credential_fallback,
        user_id,
        tools,
        extension_manager,
    )


async def resolve_auth_gate_extension_name(
        auth_manager: Optional[AuthManager],
        extension_manager: Optional[ExtensionManager],
        tools: ToolRegistry,
        pending: PendingGate,
) -> Optional[ExtensionName]:
    """
    解析拥有认证门控的已安装扩展标识符，用于在通道上显示该门控。

    仅对 `Authentication` 门控返回 `Some(ExtensionName)`——
    解析器委托给 [`resolve_extension_for_action`]。非认证门控变体
    (`Approval`、`External`) 没有扩展标识，返回 `None`。
    """
    if not isinstance(pending.resume_kind, ResumeKind.Authentication):
        return None

    credential_name = pending.resume_kind.credential_name
    return await resolve_extension_for_action(
        auth_manager,
        extension_manager,
        tools,
        pending.action_name,
        pending.parameters,
        credential_name,
        pending.user_id,
    )


async def send_pending_gate_status(
        agent: Agent,
        message: IncomingMessage,
        pending: PendingGate,
        extension_name: Optional[ExtensionName],
) -> None:
    """向通道发送待处理门控的状态更新。"""
    display_parameters = gate_display_parameters(pending)

    if isinstance(pending.resume_kind, ResumeKind.Approval):
        try:
            await agent.channels.send_status(
                message.channel,
                StatusUpdate.ApprovalNeeded(
                    request_id=str(pending.request_id),
                    tool_name=pending.action_name,
                    description=pending.description,
                    parameters=display_parameters,
                    allow_always=pending.resume_kind.allow_always,
                ),
                message.metadata,
            )
        except Exception:
            pass

    elif isinstance(pending.resume_kind, ResumeKind.Authentication):
        # `resolve_auth_gate_extension_name` 对于 Authentication 门控总是返回 `Some`；
        # 此处的 `None` 将是上游管道错误（错误的变体到达了此分支）。
        if extension_name is None:
            logger.warning(
                "认证门控到达 send_pending_gate_status 时没有已解析的扩展名称, gate=%s, request_id=%s",
                pending.gate_name,
                pending.request_id,
            )
            return
        try:
            await agent.channels.send_status(
                message.channel,
                StatusUpdate.AuthRequired(
                    extension_name=extension_name,
                    instructions=pending.resume_kind.instructions,
                    auth_url=pending.resume_kind.auth_url,
                    setup_url=None,
                    request_id=str(pending.request_id),
                ),
                message.metadata,
            )
        except Exception:
            pass

    elif isinstance(pending.resume_kind, ResumeKind.External):
        # 外部门控不需要状态更新
        pass


def resumed_action_result_message(
        call_id: str,
        action_name: str,
        output: Dict[str, Any],
) -> ThreadMessage:
    """
    从恢复的操作结果构建线程消息。
    """
    import json
    rendered = json.dumps(output, indent=2, ensure_ascii=False)
    return ThreadMessage.action_result(call_id, action_name, rendered)


def extract_external_tool_output(payload: Dict[str, Any], call_id: str) -> Any:
    """
    从 Responses API 的 `function_call_output` 解析负载中
    提取 `call_id` 对应的工具输出。处理程序构建的负载格式为
    `{"outputs": [{"call_id": ..., "output": <string|json>}]}`。
    回退到：
    - 当没有 `outputs` 数组时，返回原始负载（防御性路径，
      用于传递纯 JSON 值的调用者），
    - 当负载中根本不包含匹配的 call_id 时，返回 `None`（让 LLM 看到
      "调用者为此调用返回了空结果"，而不是重新运行工具）。
    """
    outputs = payload.get("outputs")
    if isinstance(outputs, list):
        for entry in outputs:
            entry_call_id = entry.get("call_id") if isinstance(entry, dict) else None
            if entry_call_id == call_id:
                out = entry.get("output") if isinstance(entry, dict) else None
                if out is not None:
                    return out
        # 没有匹配的 call_id：返回显式的 None 以便 LLM 看到
        # 明确的空结果，而不是（可能过时的）原始负载。
        return None

    # 完全没有 `outputs` 数组——将整个负载视为结果
    # （匹配历史上将原始值作为解析传递的 OAuth 回调）。
    return payload


def resolved_call_id_for_pending_action(
        thread: Thread,
        pending: PendingGate,
) -> Optional[str]:
    """
    解析与待处理门控对应的助手操作 `call_id`。

    当持久化的 `call_id` 和历史扫描都无法产生匹配时，返回 `None`。
    调用者必须将 `None` 视为真正的未命中，并合成一个新的 id，
    而不是将其折叠为空字符串——`ThreadMessage::action_result` 上的空
    `action_call_id` 会破坏引擎的调用/结果配对，
    并导致助手丢弃恢复的回复。
    """
    # 新的待处理门控在插入时持久化了确切的 call_id。
    # 仅在 call_id 存储之前创建的遗留行中从历史推断。
    if pending.call_id:
        return pending.call_id

    # 扫描用户可见的 `messages` 和 `internal_messages`
    # （编排器的工作转录）。在生产环境中，编排器通过
    # `sync_runtime_state` 将 ActionResult 消息写入 `internal_messages`，
    # 因此仅扫描 `messages` 会使已解析的 id 集合为空，
    # 回退将永远不会匹配。
    all_messages = list(thread.messages) + list(thread.internal_messages)

    resolved_ids: Set[str] = set()
    for message in all_messages:
        if message.role == MessageRole.ActionResult:
            if message.action_call_id:
                resolved_ids.add(message.action_call_id)

    # 倒序扫描以找到最近未解析的匹配
    for message in reversed(all_messages):
        if message.role != MessageRole.Assistant:
            continue
        if message.action_calls:
            for call in message.action_calls:
                if (
                        call.action_name == pending.action_name
                        and call.id not in resolved_ids
                ):
                    return call.id

    return None


def synthetic_action_call_id(action_name: str) -> str:
    """
    当无法恢复历史 id 时，合成一个新的操作调用 id。

    用作最后手段，以便恢复的 `ActionResult` 消息仍然携带非空相关器，
    引擎不会静默丢弃回复。
    """
    return f"synthetic-{action_name}-{uuid.uuid4()}"


async def resolved_or_synthetic_call_id_for_pending_action(
        state: EngineState,
        pending: PendingGate,
) -> str:
    """解析待处理操作的操作调用 id，或在无法解析时合成。"""
    try:
        thread = await state.store.load_thread(pending.thread_id)
    except Exception as e:
        raise engine_err("加载线程", e)

    if thread is None:
        raise engine_err("加载线程", "线程未找到")

    result = resolved_call_id_for_pending_action(thread, pending)
    if result is not None:
        return result

    logger.warning(
        "待处理门控没有历史 call_id；合成一个以保持 ActionResult 相关器非空, action=%s, thread_id=%s",
        pending.action_name,
        pending.thread_id,
    )
    return synthetic_action_call_id(pending.action_name)


def is_valid_credential_name(name: str) -> bool:
    """
    验证凭证标识符形状：非空、≤64 字符、仅 ASCII 字母数字或下划线。
    由认证回退解析器用于在对照注册表检查之前，
    拒绝任何在结构上不是凭证名称的内容。
    """
    return (
            len(name) > 0
            and len(name) <= 64
            and all(c.isascii() and (c.isalnum() or c == '_') for c in name)
    )


def parse_credential_name(text: str) -> Optional[str]:
    """
    从包含 `authentication_required` 信号的工具错误/模型响应中
    提取凭证名称。

    首先尝试结构化 JSON（http 工具发出带有 `credential_name` 字段的 JSON 对象），
    然后回退到用于自由格式错误的散文形状分割器。
    结果必须额外通过 [`is_valid_credential_name`] 才能被调用者使用。
    调用者仍必须对照凭证注册表验证名称——此函数仅规范化解析，
    不建立信任。
    """
    # 第一遍——全文 JSON。当生成者是已经序列化了结构化错误的工具时，
    # 这种方式廉价且明确。
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            name = value.get("credential_name")
            if isinstance(name, str) and is_valid_credential_name(name):
                return name
    except (json.JSONDecodeError, TypeError):
        pass

    # 第二遍——嵌入的 JSON。从第一个 `{` 切片到匹配的闭合 `}`，
    # 然后重试。我们不尝试稳健地处理嵌套对象；
    # http 工具发出的是扁平形状。
    start = text.find('{')
    if start != -1:
        end = text.rfind('}', start)
        if end != -1:
            try:
                value = json.loads(text[start:end + 1])
                if isinstance(value, dict):
                    name = value.get("credential_name")
                    if isinstance(name, str) and is_valid_credential_name(name):
                        return name
            except (json.JSONDecodeError, TypeError):
                pass

    # 第三遍——散文分割器。用于提到 `credential_name` 但没有适当 JSON 结构的
    # 自由格式文本的最后手段路径。保持窄范围并经过验证。
    # nth(1) 有意取 `credential_name` 的第一次出现；如果工具发出多个，
    # 只有第一个获胜——下游的注册表检查仍然控制是否采纳它。
    parts = text.split("credential_name")
    if len(parts) > 1:
        remainder = parts[1]
        # 在引号或反引号上分割，找到第一个非空且不含冒号和空格的片段
        segments = remainder.replace('"', '\x00').replace("'", '\x00').replace('`', '\x00').split('\x00')
        for seg in segments:
            seg = seg.strip()
            if seg and ':' not in seg and ' ' not in seg:
                if is_valid_credential_name(seg):
                    return seg

    return None


async def notify_pending_gate(
        agent: Agent,
        sse: Optional[SseManager],
        tools: ToolRegistry,
        auth_manager: Optional[AuthManager],
        extension_manager: Optional[ExtensionManager],
        message: IncomingMessage,
        pending: PendingGate,
) -> BridgeOutcome:
    """
    向所有界面通知待处理的门控：SSE 广播（如果 `sse` 已设置）
    加上通道级别的状态事件和面向用户的提示。

    将 `sse` 作为拥有的 `Optional[SseManager]` 接收，而不是从
    `EngineState` 借用，以便调用者可以从引擎状态读守卫中克隆 Arc，
    并在等待 broadcast + channel I/O 之前 `drop(guard)`。
    在这些等待中持有引擎状态守卫在生产稳态中是没问题的
    （外部锁在 init 后是只读的），但对于并发拆除状态的测试会出问题，
    并且对于任何未来的热重载路径都是脆弱的。
    `handle_with_engine` 的终端返回分支（auth + approval）都依赖于此
    drop 纪律在与用户通信之前释放守卫。

    通过通道级别的状态事件（批准卡片、认证提示等）重新通知用户待处理的门控。
    返回 `None`——卡片是唯一面向用户的信号；不发出文本响应。

    `_sse` 和 `tools` 参数被保留，以便调用者可以从引擎状态守卫中克隆 Arc，
    并在此 await 之前 `drop(guard)`，保持读锁范围紧凑。
    """
    extension_name = await resolve_auth_gate_extension_name(
        auth_manager, extension_manager, tools, pending
    )

    # 外部工具门控（Responses API 调用者执行的工具）投影到专用的
    # `AppEvent::ExternalToolCall`，以便 Responses API 累积器可以将它们显示为
    # `function_call` 项，而不重新渲染为批准卡片。OAuth/配对回调
    # （也使用 `ResumeKind::External` 但具有不同的 callback_id 前缀）
    # 继续通过标准的 `AppEvent::GateRequired` 路径。
    if isinstance(pending.resume_kind, ResumeKind.External):
        callback_id = pending.resume_kind.callback_id
        logger.debug(
            "GatePaused(External), gate=%s, callback=%s",
            pending.gate_name,
            callback_id,
        )
        if is_external_tool_callback_id(callback_id):
            if sse is not None:
                arguments = json.dumps(pending.parameters)
                event = AppEvent.ExternalToolCall(
                    request_id=str(pending.request_id),
                    call_id=pending.call_id,
                    name=pending.action_name,
                    arguments=arguments,
                    thread_id=pending.effective_wire_thread_id(),
                )
                # 投影豁免：桥接调度器，ResumeKind::External(ext_tool) → Responses API function_call 显示
                sse.broadcast_for_user(message.user_id, event)
            else:
                # 目前每个外部工具流都通过网关运行，网关始终连接 SSE——
                # 因此此分支意味着未来的通道在没有 SSE 等效扇出的情况下
                # 增长了外部工具显示面，调用者将永远不会知道线程已暂停。
                # 记录日志以便我们可以诊断，而不是静默挂起。
                logger.debug(
                    "外部工具门控已暂停但没有连接广播器；调用者将不会被通知, user_id=%s, callback=%s, request_id=%s",
                    message.user_id,
                    callback_id,
                    pending.request_id,
                )
            # 不运行 `send_pending_gate_status`——该路径用于批准卡片 UX，
            # 不适用于调用者执行的工具调用。
            return BridgeOutcome.Pending

    # 通过源通道发送批准/认证卡片。每个通道原生渲染此内容
    # （web → SSE 卡片，TUI → 小部件，relay → 按钮）。
    # 不返回文本响应，以避免在卡片旁边出现重复消息。
    if sse is not None:
        display_parameters = gate_display_parameters(pending)
        sse.broadcast_for_user(
            message.user_id,
            AppEvent.GateRequired(
                request_id=str(pending.request_id),
                gate_name=pending.gate_name,
                tool_name=pending.action_name,
                description=pending.description,
                parameters=json.dumps(display_parameters, indent=2),
                extension_name=extension_name,
                resume_kind=json.dumps(pending.resume_kind.to_dict()),
                thread_id=pending.effective_wire_thread_id(),
            ),
        )

    await send_pending_gate_status(agent, message, pending, extension_name)
    return BridgeOutcome.Pending


async def insert_and_notify_pending_gate(
        agent: Agent,
        state: EngineState,
        message: IncomingMessage,
        pending: PendingGate,
) -> BridgeOutcome:
    """插入待处理门控并通知所有界面。"""
    await state.pending_gates.insert(pending)
    return await notify_pending_gate(
        agent,
        state.sse,
        state.effect_adapter.tools(),
        state.auth_manager,
        state.extension_manager,
        message,
        pending,
    )


async def requeue_auth_pending_gate(
        agent: Agent,
        state: EngineState,
        message: IncomingMessage,
        pending: PendingGate,
        instructions: str,
        auth_url: Optional[str],
) -> BridgeOutcome:
    """
    为同一 `(user, thread)` 重新排队认证门控。

    此路径替换刚刚解析的门控。`resolve_gate()` 已经原子性地删除了旧门控，
    且 `PendingGateStore::insert()` 仍然强制每个 `(user_id, thread_id)` 最多一个活动门控，
    因此重试仍然受活动暂停线程数限制，而不会因无效令牌尝试而无限增长。
    """
    if not isinstance(pending.resume_kind, ResumeKind.Authentication):
        raise engine_err(
            "解析不匹配",
            f"期望认证门控，得到 {pending.resume_kind.kind_name()}",
        )

    credential_name = pending.resume_kind.credential_name

    next_pending = PendingGate(
        request_id=uuid.uuid4(),
        gate_name=pending.gate_name,
        user_id=pending.user_id,
        thread_id=pending.thread_id,
        scope_thread_id=pending.scope_thread_id,
        conversation_id=pending.conversation_id,
        source_channel=pending.source_channel,
        action_name=pending.action_name,
        call_id=pending.call_id,
        parameters=pending.parameters,
        display_parameters=pending.display_parameters,
        description=pending.description,
        resume_kind=ResumeKind.Authentication(
            credential_name=credential_name,
            instructions=instructions,
            auth_url=auth_url,
        ),
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        original_message=pending.original_message,
        resume_output=pending.resume_output,
        paused_lease=pending.paused_lease,
        approval_already_granted=pending.approval_already_granted,
    )

    return await insert_and_notify_pending_gate(agent, state, message, next_pending)


def pairing_pending_gate_from_auth(pending: PendingGate, extension_name: str) -> PendingGate:
    """从认证门控创建配对门控。"""
    return PendingGate(
        request_id=uuid.uuid4(),
        gate_name="pairing",
        user_id=pending.user_id,
        thread_id=pending.thread_id,
        scope_thread_id=pending.scope_thread_id,
        conversation_id=pending.conversation_id,
        source_channel=pending.source_channel,
        action_name=pending.action_name,
        call_id=pending.call_id,
        parameters=pending.parameters,
        display_parameters=pending.display_parameters,
        description=f"需要为 '{extension_name}' 进行配对。",
        resume_kind=ResumeKind.External(
            callback_id=f"pairing:{extension_name}",
        ),
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        original_message=pending.original_message,
        resume_output=pending.resume_output,
        paused_lease=pending.paused_lease,
        approval_already_granted=pending.approval_already_granted,
    )


async def requeue_pairing_pending_gate(
        state: EngineState,
        pending: PendingGate,
        extension_name: str,
) -> PendingGate:
    """重新排队配对门控。"""
    next_pending = pairing_pending_gate_from_auth(pending, extension_name)
    await state.pending_gates.insert(next_pending)
    return next_pending


async def persist_always_allow(
        agent: Agent,
        state: EngineState,
        pending: PendingGate,
) -> Optional[Dict[str, Any]]:
    """
    当用户点击"始终批准"时，将 `AlwaysAllow` 持久化到数据库。

    纵深防御：为实际待处理参数声明了 `ApprovalRequirement::Always` 的工具
    永远不会被持久化（UI 隐藏按钮，但精心构造的客户端可以发送它）。
    工具名称在用作设置键之前经过验证。

    返回先前存在的权限值（如果有），以便调用者可以通过
    [`revert_always_allow`] 在失败时恢复它。
    """
    return await persist_always_allow_with_store(
        agent.deps.settings_store, state, pending
    )


async def persist_always_allow_with_store(
        settings_store: Optional[SettingsStore],
        state: EngineState,
        pending: PendingGate,
) -> Optional[Dict[str, Any]]:
    """
    与 [`persist_always_allow`] 相同，但直接接收设置存储，
    而不是通过 `Agent` 访问。让网关 HTTP 快速路径
    (`try_resolve_inline_approval_gate`) 可以在没有 `Agent` 引用的情况下
    安装 AlwaysAllow 首选项，因为 agent-loop mpsc 正是该路径绕过的。
    """
    # 在将工具名称用作设置键之前验证它。拒绝包含点号或其他可能
    # 与点分路径设置命名空间冲突的字符的名称。
    if not is_valid_admin_tool_name(pending.action_name):
        logger.debug(
            "跳过 AlwaysAllow 持久化——无效的工具名称, tool=%s",
            pending.action_name,
        )
        return None

    # 纵深防御：跳过 `ApprovalRequirement::Always` 工具的持久化。
    # 使用实际的待处理参数，以便正确检测参数依赖的工具
    # （例如具有高风险命令的 shell）。
    tool = await state.effect_adapter.tools().get(pending.action_name)
    is_locked = False
    if tool is not None:
        is_locked = tool.requires_approval(pending.parameters) == ApprovalRequirement.Always

    if is_locked:
        logger.debug(
            "跳过 AlwaysAllow 持久化——工具声明了 ApprovalRequirement::Always, tool=%s",
            pending.action_name,
        )
        return None

    # 仅使用 CachedSettingsStore。原始 Database 回退绕过了缓存失效，
    # 导致 GET /api/settings/tools 在 5 分钟 TTL 过期之前提供过时数据。
    # 在生产环境中，设置存储在有数据库时始终可用；回退是死代码，
    # 在测试和边缘部署中积极破坏了缓存一致性。
    if settings_store is None:
        return None

    store = settings_store
    key = f"tool_permissions.{pending.action_name}"

    # 读取先前存在的值，以便在失败时恢复它，
    # 而不是盲目删除长期存在的用户首选项。
    try:
        prior = await store.get_setting(pending.user_id, key)
    except Exception as e:
        logger.debug(
            "resolve_gate: 读取先前权限失败，跳过持久化, tool=%s, error=%s",
            pending.action_name,
            e,
        )
        return None

    val = json.dumps("always_allow")

    # 调度豁免：引擎内部持久化镜像 v1 thread_ops 直写
    try:
        await store.set_setting(pending.user_id, key, val)
        logger.debug(
            "已将 AlwaysAllow 权限持久化到数据库设置 (engine v2), tool=%s, user_id=%s",
            pending.action_name,
            pending.user_id,
        )
    except Exception as e:
        logger.warning(
            "resolve_gate: 持久化 AlwaysAllow 失败, tool=%s, user_id=%s, error=%s",
            pending.action_name,
            pending.user_id,
            e,
        )

    return prior


async def revert_always_allow(
        agent: Agent,
        pending: PendingGate,
        prior: Optional[Dict[str, Any]],
) -> None:
    """
    当恢复的工具执行失败时，从数据库回滚 `AlwaysAllow`。

    恢复在 [`persist_always_allow`] 写入 `AlwaysAllow` 之前存在的 `prior` 值。
    如果没有先前值，则删除该键。
    """
    await revert_always_allow_with_store(
        agent.deps.settings_store, pending, prior
    )


async def revert_always_allow_with_store(
        settings_store: Optional[SettingsStore],
        pending: PendingGate,
        prior: Optional[Dict[str, Any]],
) -> None:
    """
    与 [`revert_always_allow`] 相同，但直接接收设置存储。
    与 [`persist_always_allow_with_store`] 配对，用于绕过 agent-loop mpsc 的
    网关 HTTP 快速路径。
    """
    if settings_store is None:
        return

    store = settings_store
    key = f"tool_permissions.{pending.action_name}"

    try:
        if prior is not None:
            # 调度豁免：persist_always_allow 的引擎内部回滚
            await store.set_setting(pending.user_id, key, prior)
        else:
            # 调度豁免：persist_always_allow 的引擎内部回滚
            await store.delete_setting(pending.user_id, key)
    except Exception as e:
        logger.warning(
            "resolve_gate: 执行失败后回滚 AlwaysAllow 失败, tool=%s, user_id=%s, error=%s",
            pending.action_name,
            pending.user_id,
            e,
        )


def snapshot_lease_still_valid(
        lease: CapabilityLease,
        pending: PendingGate,
) -> bool:
    """
    验证门控暂停时记录的 `paused_lease` 快照在恢复时是否仍代表可用租约。

    门控可以在待处理门控存储中停留数小时或跨进程重启；
    在此期间，原始租约可能已被撤销、过期，或者待处理记录可能已偏离其原始线程。
    未通过此检查的调用者不得使用快照——
    回退到 `LeaseManager.find_lease_for_action`（强制执行其自己的范围限定）或安全关闭。
    """
    if lease.thread_id != pending.thread_id:
        return False
    if not lease.granted_actions.covers(pending.action_name):
        return False
    if lease.revoked:
        return False
    if lease.expires_at is not None and lease.expires_at <= datetime.now(timezone.utc):
        return False
    return True


async def resume_lease_for_pending_gate(
        pending: PendingGate,
        leases: LeaseManager,
) -> Optional[CapabilityLease]:
    """
    选择用于恢复待处理门控操作的租约。如果门控记录的 `paused_lease` 快照仍然有效，
    则优先使用它；回退到 `LeaseManager` 中的实时查找。
    如果两条路径都没有产生租约，则返回 `None`——调用者将其映射到"无活动租约"错误。
    """
    if pending.paused_lease is not None:
        snapshot = pending.paused_lease
        if snapshot_lease_still_valid(snapshot, pending):
            return snapshot

    return await leases.find_lease_for_action(pending.thread_id, pending.action_name)


def emit_gate_expired_dismissal(
        state: EngineState,
        message: IncomingMessage,
        pending: PendingGate,
) -> BridgeOutcome:
    """
    广播 `GateResolved { resolution: "expired" }` 事件并返回关闭结果。
    当目标线程在 `take_verified` 和恢复之间被删除时使用，
    因此没有活动线程可以执行。

    持久化副作用的调用者（例如 `Approved { always }` 将 `AlwaysAllow` 写入设置）
    必须使用 `state.store.load_thread` 进行预检，并在持久化之前调用此辅助函数，
    以免缺失的线程静默提交对从未运行的工具的长期首选项 (#2347)。
    """
    logger.debug(
        "未找到待处理门控的线程；发出过期解析, thread_id=%s, gate=%s, action=%s",
        pending.thread_id,
        pending.gate_name,
        pending.action_name,
    )
    if state.sse is not None:
        state.sse.broadcast_for_user(
            message.user_id,
            AppEvent.GateResolved(
                request_id=str(pending.request_id),
                gate_name=pending.gate_name,
                tool_name=pending.action_name,
                resolution="expired",
                message="线程不再存在。",
                thread_id=pending.effective_wire_thread_id(),
            ),
        )
    return BridgeOutcome.Respond("线程不再存在。批准已关闭。")


async def execute_pending_gate_action(
        agent: Agent,
        state: EngineState,
        message: IncomingMessage,
        pending: PendingGate,
        approval_already_granted: bool,
        approval_event: Optional[Tuple[str, bool]],
) -> BridgeOutcome:
    """执行待处理门控操作。"""
    # 加载线程
    try:
        thread = await state.store.load_thread(pending.thread_id)
    except Exception as e:
        # 瞬态数据库故障——传播以便调用者可以重试，而不是永久丢弃门控
        raise engine_err("加载线程", e)

    if thread is None:
        return emit_gate_expired_dismissal(state, message, pending)

    resolved_call_id = await resolved_or_synthetic_call_id_for_pending_action(state, pending)

    lease = await resume_lease_for_pending_gate(pending, state.thread_manager.leases)
    if lease is None:
        raise engine_err(
            "恢复租约",
            f"没有活动租约覆盖操作 '{pending.action_name}'",
        )

    exec_ctx = ThreadExecutionContext(
        thread_id=pending.thread_id,
        thread_type=thread.thread_type,
        project_id=thread.project_id,
        user_id=thread.user_id,
        step_id=StepId.new(),
        current_call_id=resolved_call_id,
        source_channel=pending.source_channel,
        user_timezone=(
            ValidTimezone.parse(thread.metadata.get("user_timezone"))
            if thread.metadata.get("user_timezone")
            else None
        ),
        thread_goal=thread.goal,
        available_actions_snapshot=None,
        available_action_inventory_snapshot=None,
        conversation_scope=None,
        # 解析后重放：门控已在上游解析，因此不需要真正的控制器。
        # 惰性控制器将任何意外的重新门控显示为类型化拒绝，而不是重现修复前的回滚错误。
        gate_controller=CancellingGateController.arc(),
        # 遗留的 resolved-pending 路径直接将其自己的 `approval_already_granted`
        # 传递给 `execute_resolved_pending_action`，因此此字段对该路径无关。
        # 在此重置以保持默认值明显。
        call_approval_granted=False,
        # 解析后重放永远不会触发新的内联门控；对话路由在此无关。
        conversation_id=None,
    )

    active_leases = await state.thread_manager.leases.active_for_thread(thread.id)
    try:
        inventory = await state.effect_adapter.available_action_inventory(
            active_leases, exec_ctx
        )
        available_actions = list(inventory.inline)
        exec_ctx.available_actions_snapshot = available_actions
        exec_ctx.available_action_inventory_snapshot = inventory
    except Exception as error:
        logger.debug(
            "加载待处理门控恢复的操作清单失败, thread_id=%s, action=%s: %s",
            thread.id,
            pending.action_name,
            error,
        )

    state.effect_adapter.reset_call_count()
    try:
        result = await state.effect_adapter.execute_resolved_pending_action(
            pending.action_name,
            pending.parameters,
            lease,
            exec_ctx,
            approval_already_granted,
        )
        await state.thread_manager.resume_thread(
            pending.thread_id,
            message.user_id,
            resumed_action_result_message(
                resolved_call_id,
                pending.action_name,
                result.output,
            ),
            approval_event,
            resolved_call_id,
        )
        return await await_thread_outcome(
            agent,
            state,
            message,
            pending.conversation_id,
            pending.thread_id,
        )

    except EngineError.GatePaused as e:
        # 获取显示参数
        tool = await state.effect_adapter.tools().get(e.action_name)
        display_parameters = (
            redact_params(e.parameters, tool.sensitive_params())
            if tool
            else e.parameters
        )

        pending_gate = PendingGate(
            request_id=uuid.uuid4(),
            gate_name=e.gate_name,
            user_id=message.user_id,
            thread_id=pending.thread_id,
            scope_thread_id=pending.scope_thread_id,
            conversation_id=pending.conversation_id,
            source_channel=message.channel,
            action_name=e.action_name,
            call_id=e.call_id,
            parameters=e.parameters,
            display_parameters=display_parameters,
            description=f"工具 '{e.action_name}' 需要 {e.resume_kind.kind_name()}。",
            resume_kind=e.resume_kind,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            # 当恢复的门控立即链接到另一个门控时（例如批准后跟着认证），
            # 保留发起用户提示。OAuth 回调重放依赖于此作为原始请求，而不是批准负载。
            original_message=(
                pending.original_message
                if pending.original_message is not None
                else message.content
            ),
            resume_output=e.resume_output,
            paused_lease=e.paused_lease,
            approval_already_granted=(
                    approval_already_granted
                    or isinstance(pending.resume_kind, ResumeKind.Approval)
            ),
        )
        return await insert_and_notify_pending_gate(agent, state, message, pending_gate)

    except Exception as e:
        raise engine_err("执行待处理门控操作", e)


async def resolve_user_project(
        store: Store,
        user_id: str,
        fallback: ProjectId,
) -> ProjectId:
    """
    解析用户的默认项目，如果需要则创建一个。

    在多用户部署中，每个用户获得自己的项目，以便线程、任务和内存文档隔离。
    当用户就是所有者时，使用所有者的项目（作为 `fallback` 传入），
    避免了在常见的单用户情况下进行额外的存储查找。
    """
    # 快速路径：检查回退项目是否属于此用户
    try:
        project = await store.load_project(fallback)
        if project is not None and project.is_owned_by(user_id):
            return fallback
    except Exception:
        pass

    # 查找此用户拥有的现有默认项目
    projects = await store.list_projects(user_id)
    for project in projects:
        if project.name == "default":
            return project.id

    # 为此用户创建新的默认项目
    project = Project(user_id, "default", "Default project")
    pid = project.id
    await store.save_project(project)
    logger.debug("为用户创建了默认项目, user_id=%s, project_id=%s", user_id, pid)
    return pid


async def engine_external_tool_catalog() -> Optional[ExternalToolCatalog]:
    """
    如果引擎状态已初始化，则返回活动 `ExternalToolCatalog` 的克隆，
    如果引擎尚未启动（engine_v2 禁用或第一条消息尚未到达），则返回 `None`。
    Responses API 处理程序使用此功能在将用户消息发送到代理循环之前注册调用者提供的工具。
    """
    lock = ENGINE_STATE.get()
    if lock is None:
        return None
    async with lock.read() as guard:
        if guard is not None:
            return guard.external_tool_catalog
        return None


async def engine_capability_action_names() -> Optional[List[str]]:
    """
    可通过引擎 v2 能力注册表分派的操作名称（`mission_*`、`skill_*`、`memory_*` 等）。
    由 Responses API 处理程序用于拒绝其名称会遮蔽内部引擎操作的调用者提供的工具——
    单独的 `tool_registry` 检查可以捕获内置和扩展工具，但会遗漏能力操作，
    即使 LLM 可见的清单去重隐藏了它们，这些操作也可能在目录短路中落地。

    如果引擎 v2 未初始化，返回 `None`；调用者将其视为"没有引擎 v2 操作会发生冲突"。
    """
    lock = ENGINE_STATE.get()
    if lock is None:
        return None
    async with lock.read() as guard:
        state = guard
        if state is None:
            return None
        names: List[str] = []
        for cap in state.capability_registry.list():
            for action in cap.actions:
                names.append(action.name)
        return names


# ----------------------------------
from engine import (
    Capability,
    CapabilityRegistry,
    ConversationManager,
    EffectExecutor,
    LeaseManager,
    MissionManager,
    PolicyEngine,
    Project,
    ProjectId,
    Store,
    ThreadConfig,
    ThreadManager,
    ThreadOutcome
)
from engine.types import is_shared_owner, shared_owner_id


@dataclass(kw_only=True)
class EngineState:
    """跨消息持久化的引擎状态。"""
    thread_manager: ThreadManager
    conversation_manager: ConversationManager
    effect_adapter: EffectBridgeAdapter
    store: Store
    default_project_id: ProjectId
    # 统一待处理门控存储——按 (user_id, thread_id) 键控
    pending_gates: PendingGateStore
    # 用于向 Web 网关广播 AppEvents 的 SSE 管理器
    sse: Optional[SseManager] = None
    # 用于写入对话消息的 V1 数据库（网关从此处读取）
    db: Optional[Database] = None
    # 用于在认证流后存储凭证的密钥存储
    secrets_store: Optional[SecretsStore] = None
    # 用于设置指令查找和凭证检查的集中式认证管理器
    auth_manager: Optional[AuthManager] = None
    # 当没有认证管理器时，用于扩展支持的认证/设置的扩展管理器
    extension_manager: Optional[ExtensionManager] = None
    # 项目本地附件持久化的文件系统根目录
    project_root: Path

    # 调用者提供的外部工具的每线程目录（Responses API）。
    # 通过 Arc 克隆与 effect adapter（在操作列表和分派期间读取）
    # 和 Responses API 处理程序（在将请求发送到代理循环之前写入）共享。
    external_tool_catalog: ExternalToolCatalog = None
    # 引擎 v2 能力注册表。保存在此处（除了 `effect_adapter` 的内部句柄），
    # 以便 Responses API 处理程序可以枚举内部操作名称并拒绝会遮蔽它们的调用者提供的工具。
    capability_registry: CapabilityRegistry = None
    # 内联门控等待控制器。让引擎在 `Approval` 和 `Authentication` 门控上
    # 原地暂停 Tier 0 和 Tier 1 执行，而不是回退到编排器并在恢复时重新进入
    # （这会重新执行先前的非幂等工具调用）。
    gate_controller: BridgeGateController = None
    # 进程范围内的进行中门控解析通道注册表。
    # 与 `gate_controller` 一起保存，以便 OAuth 回调路径可以按凭证名称
    # 唤醒暂停的 Authentication 等待者，而无需通过控制器的内部机制。
    gate_resolutions: GateResolutions = None


# 全局引擎状态，首次使用时初始化
# 为了确保 Engine v2 状态的线程安全访问和单例初始化
ENGINE_STATE: Optional[EngineState] = None  # OnceLock<RwLock<Option<EngineState>>>


class PendingGateResolutionNone:
    pass


@dataclass
class PendingGateResolutionResolved:
    gate: PendingGate


class PendingGateResolutionAmbiguous:
    pass


PendingGateResolution = PendingGateResolutionNone | PendingGateResolutionResolved | PendingGateResolutionAmbiguous


def parse_engine_thread_id(scope: Optional[str]) -> Optional[ThreadId]:
    """从范围字符串解析引擎线程 ID。"""
    if scope is None:
        return None
    try:
        return ThreadId(uuid.UUID(scope))
    except (ValueError, AttributeError):
        return None


def parse_scope_uuid(scope: Optional[str]) -> Optional[uuid.UUID]:
    """从范围字符串解析 UUID。"""
    if scope is None:
        return None
    try:
        return uuid.UUID(scope)
    except (ValueError, AttributeError):
        return None


async def resolve_v1_conversation_for_message(
        db: Database,
        message: IncomingMessage,
) -> uuid.UUID:
    """
    为消息解析 v1 对话。

    如果有对话范围，则获取或创建限定范围的对话；
    否则获取或创建默认助手对话。
    """
    scope = message.conversation_scope()
    if scope is not None:
        return await db.get_or_create_scoped_conversation(
            message.user_id, message.channel, scope
        )

    return await db.get_or_create_assistant_conversation(
        message.user_id, message.channel
    )


async def reconcile_pending_gate_state(
        store: Store,
        pending_gates: PendingGateStore,
) -> None:
    """
    协调待处理门控状态：清理孤立门控和等待线程。

    在启动时调用，以修复跨进程重启的不一致状态。
    """
    restored_gates = await pending_gates.list_all()
    gate_keys: Set[PendingGateKey] = set()

    for gate in restored_gates:
        key = gate.key()
        gate_keys.add(key)

        try:
            thread = await store.load_thread(gate.thread_id)
        except Exception as e:
            raise engine_err("加载线程", e)

        if thread is None:
            await pending_gates.discard(gate.key())
            continue

        if (
                thread.state != ThreadState.Waiting
                or not thread.is_owned_by(gate.user_id)
        ):
            await pending_gates.discard(gate.key())

    # 清理没有对应门控的等待线程
    projects = await store.list_all_projects()
    for project in projects:
        threads = await store.list_all_threads(project.id)
        for thread in threads:
            if thread.state != ThreadState.Waiting:
                continue

            key = PendingGateKey(
                user_id=thread.user_id,
                thread_id=thread.id,
            )
            if key in gate_keys:
                continue

            try:
                thread.transition_to(
                    ThreadState.Failed,
                    "恢复期间待处理门控缺失",
                )
            except Exception as e:
                logger.debug(
                    "协调等待线程失败, thread_id=%s, error=%s",
                    thread.id,
                    e,
                )
                continue

            try:
                await store.save_thread(thread)
            except Exception as e:
                raise engine_err("保存协调后的线程", e)


async def fail_orphaned_waiting_thread_if_needed(
        state: EngineState,
        user_id: str,
        thread_id: ThreadId,
) -> bool:
    """
    如果需要，将孤立的等待线程标记为失败。

    检查是否存在对应的待处理门控；如果没有，则将该等待线程标记为失败。
    返回 True 表示线程已被标记为失败。
    """
    existing = await state.pending_gates.peek(
        PendingGateKey(
            user_id=user_id,
            thread_id=thread_id,
        )
    )
    if existing is not None:
        return False

    return await fail_waiting_thread(
        state,
        user_id,
        thread_id,
        "恢复前待处理门控缺失",
    )


async def fail_waiting_thread(
        state: EngineState,
        user_id: str,
        thread_id: ThreadId,
        reason: str,
) -> bool:
    """
    将 `user_id` 拥有的 Waiting 线程转换为 Failed 状态并附加 `reason`。
    当线程不存在、由其他人拥有或不在 `Waiting` 状态时返回 `False`。
    """
    try:
        thread = await state.store.load_thread(thread_id)
    except Exception as e:
        raise engine_err("加载线程", e)

    if thread is None:
        return False

    if not thread.is_owned_by(user_id) or thread.state != ThreadState.Waiting:
        return False

    thread.transition_to(ThreadState.Failed, reason)
    await state.store.save_thread(thread)
    return True


def format_auth_completed_resuming(raw: str) -> str:
    """
    为认证完成的 Ready 分支构建面向用户的 "<message>。正在恢复..." 状态文本。

    `ExtensionManager::configure_token` 的 `result.message` 已经以句号结尾
    （例如 `"Configuration saved for 'telegram'."`），因此简单的
    `format!("{}. Resuming...", msg)` 会产生 `"...telegram'.. Resuming..."`
    ——双句号。在格式化之前去除原始消息的尾部句号和空白。
    其他标点符号被有意保留（没有现实世界的后端消息以 `!`/`?`/等结尾）。
    """
    trimmed = raw.rstrip('.')
    trimmed = trimmed.strip()
    return f"{trimmed}。正在恢复..."


class PendingAuthCredentialSubmission(Enum):
    """
    `submit_pending_auth_credential` 的结果——
    区分"后端已存储凭证"和"没有后端配置为存储它"。
    调用者将后者映射为线程失败（生产环境）或静默继续（裸恢复测试夹具），
    参见 `resolve_gate` 中的匹配分支。
    """
    Stored = "stored"
    SkippedNoBackend = "skipped_no_backend"


async def submit_pending_auth_credential(
        state: EngineState,
        submit_target: str,
        credential_name: str,
        token: str,
        user_id: str,
) -> PendingAuthCredentialSubmission:
    """
    尝试持久化用户提供的认证凭证，按优先级顺序回退到三个后端：

    1. `AuthManager.submit_auth_token(submit_target, ...)`——规范路径
       （运行扩展的 `configure_token`，验证并发出 `ConfigureResult`）。
       需要密钥支持的认证管理器。
    2. `ExtensionManager.configure_token(submit_target, ...)`——用于
       在没有 `SECRETS_MASTER_KEY` 的情况下运行的托管实例，因此不存在
       持久认证管理器，但扩展管理器的内存密钥存储仍然可以接受凭证。
       `NotInstalled` / `NotFound` 穿透，以便非扩展凭证（纯密钥）
       在步骤 3 中存储。
    3. 纯 `SecretsStore.create(credential_name, ...)`——当没有扩展拥有该操作时，
       为非扩展操作（HTTP 工具、技能凭证）原样存储凭证。

    # 为什么有两个键（`submit_target` + `credential_name`）

    步骤 1–2 采用**扩展**标识（`submit_target`，例如 `"telegram"`）。
    它们通过遍历扩展的能力文件来解析实际的密钥键——这正是通过
    `configure_token` 路由的要点，它验证扩展已安装并选择正确的必需密钥槽。

    步骤 3 采用**凭证**标识（`credential_name`，例如 `"telegram_bot_token"`
    或 `"github_token"`），因为密钥存储没有扩展的概念——
    它按名称存储原始密钥。对于到达步骤 3 的流，没有扩展可以解析
    （内置 HTTP 工具、技能凭证），因此凭证名称本身就是存储键。

    这种不对称是有意的：引擎的 `ResumeKind::Authentication` 仅携带
    `credential_name`，因此调用者将两个标识都传递进来，每个后端选择
    自己要操作的那个。

    # 步骤 3 回退的凭证名称验证

    步骤 1–2 通过它们的能力查找拒绝未知凭证
    （`auth_manager` 通过 `get_credential_spec`；`extension_manager`
    通过 `determine_installed_kind`）。步骤 3 是非扩展路径，
    因此唯一的验证是上游信任链：待处理门控的
    `ResumeKind::Authentication.credential_name` 是类型化的
    `CredentialName`（在构造时验证的 newtype），并且待处理门控本身
    是由引擎为特定工具调用插入的，该工具调用的认证描述符产生了该凭证。
    此处的调用者将值作为 `str` 接收，因为 `CreateSecretParams` 在边界处
    是字符串类型的，但它源自上游已验证的 newtype。

    当三个后端都不可用时返回 `SkippedNoBackend`
    （已经准备了 `resume_output` 的裸测试夹具）。
    """
    if state.auth_manager is not None:
        result = await state.auth_manager.submit_auth_token(
            submit_target, token, user_id
        )
        return PendingAuthCredentialSubmission.Stored

    if state.extension_manager is not None:
        try:
            result = await state.extension_manager.configure_token(
                submit_target, token, user_id
            )
            return PendingAuthCredentialSubmission.Stored
        except ExtensionError.NotInstalled:
            # 不是扩展支持的凭证——穿透到 secrets_store
            pass
        except ExtensionError.NotFound:
            # 不是扩展支持的凭证——穿透到 secrets_store
            pass

    if state.secrets_store is not None:
        # 非扩展路径（内置 HTTP、技能凭证）：按原始凭证名称存储。
        # 关于为什么步骤 1–2 采用 `submit_target` 而步骤 3 采用
        # `credential_name`，请参见函数文档。
        params = CreateSecretParams(credential_name, token)
        await state.secrets_store.create(user_id, params)
        return PendingAuthCredentialSubmission.Stored

    return PendingAuthCredentialSubmission.SkippedNoBackend


async def init_engine(agent: Agent) -> None:
    """
    使用代理的依赖项获取或初始化引擎状态。

    在启动时（从 `Agent.run()`）当 `ENGINE_V2=true` 时急切调用，
    并在每个处理程序中作为惰性回退进行防御性调用。
    """
    global ENGINE_STATE

    if ENGINE_STATE is not None and ENGINE_STATE.is_some():
        return

    # 初始化
    if ENGINE_STATE is not None and ENGINE_STATE.is_some():
        return  # 获取写锁后再次检查

    logger.debug("engine v2: 正在初始化引擎状态")

    llm_adapter = LlmBridgeAdapter(
        agent.llm(),
        agent.cheap_llm(),
    )

    effect_adapter = EffectBridgeAdapter(
        agent.tools(),
        agent.safety(),
        agent.hooks(),
    ).with_global_auto_approve(agent.config().auto_approve_tools)

    # 传播跟踪 HTTP 拦截器（实时录制或重放），以便引擎 v2 工具分派
    # 录制/重放 HTTP 交换。没有这个，录制的跟踪会错过从引擎 v2 路径
    # 发出的每个出站调用，并且重放无法替换响应。
    if agent.deps.http_interceptor is not None:
        await effect_adapter.set_http_interceptor(agent.deps.http_interceptor)

    # 构建用于飞行前凭证检查的集中式认证管理器
    has_secrets = agent.tools().secrets_store() is not None
    has_cred_reg = agent.tools().credential_registry() is not None
    logger.debug(
        "engine v2: 认证管理器初始化检查, has_secrets_store=%s, has_credential_registry=%s",
        has_secrets,
        has_cred_reg,
    )
    auth_manager = None
    if agent.deps.auth_manager is not None:
        auth_manager = agent.deps.auth_manager
        await effect_adapter.set_auth_manager(auth_manager)
        logger.debug("engine v2: 已在效果适配器上设置认证管理器")
    elif agent.tools().secrets_store() is not None:
        auth_manager = AuthManager(
            agent.tools().secrets_store(),
            agent.deps.skill_registry,
            agent.deps.extension_manager,
            agent.tools(),
        )
        await effect_adapter.set_auth_manager(auth_manager)
        logger.debug("engine v2: 已在效果适配器上设置认证管理器")
    else:
        logger.debug("engine v2: 没有密钥存储——未创建认证管理器")

    store = HybridStore(agent.workspace())
    await store.load_state_from_workspace()
    await effect_adapter.set_engine_store(store)
    if agent.deps.skill_registry is not None:
        await effect_adapter.set_skill_registry(agent.deps.skill_registry)

    # 清理先前运行中已完成的线程和死租约
    cleaned = await store.cleanup_terminal_state(timedelta(minutes=5))
    if cleaned > 0:
        logger.debug("engine v2: 启动时清理了 %d 个终端状态条目", cleaned)

    # 生成引擎工作区 README
    await store.generate_engine_readme()

    capabilities = CapabilityRegistry()

    # 将任务函数注册为能力，以便线程接收租约。
    # 在常规工具执行器之前由 EffectBridgeAdapter.handle_mission_call() 处理。
    # 仅使用 "mission_*" 名称——描述中提到 "routine" 以便 LLM 正确映射用户意图。
    capabilities.register(Capability(
        name="missions",
        description="任务和例行任务生命周期管理",
        actions=mission_capability_actions(),
        knowledge=[],
        policies=[],
    ))

    leases = LeaseManager()
    policy = PolicyEngine()

    store_dyn = store

    # 与效果适配器共享注册表，以便其 `available_actions` 可以向 LLM 通告
    # 引擎原生能力操作（任务）。没有这个，任务工具具有活动租约但永远不会出现在
    # 每次 LLM 调用发送的工具列表中。
    capabilities_arc = capabilities
    await effect_adapter.set_capability_registry(capabilities_arc)

    thread_manager = ThreadManager(
        llm_adapter,
        effect_adapter,
        store_dyn,
        capabilities_arc,
        leases,
        policy,
    )

    # 迁移遗留记录：预先存在的引擎记录在反序列化时没有 user_id 字段，
    # 并获得 serde 默认值 "legacy"。将所有者身份标记到它们上面，
    # 以便用户范围的查询在升级后可以找到它们。
    owner_id = agent.deps.owner_id
    await migrate_legacy_user_ids(store_dyn, owner_id)

    # 在可用时重用持久化的默认项目
    projects = await store.list_projects(owner_id)
    default_project = next((p for p in projects if p.name == "default"), None)
    if default_project is not None:
        project_id = default_project.id
    else:
        project = Project(owner_id, "default", "引擎 v2 的默认项目")
        project_id = project.id
        await store.save_project(project)

    conversation_manager = ConversationManager(thread_manager, store)
    try:
        await conversation_manager.bootstrap_user(agent.deps.owner_id)
    except Exception as e:
        logger.debug("engine v2: bootstrap_user 失败: %s", e)

    # 创建任务管理器并启动 cron 定时器。附加：
    # - WorkspaceReader，以便带有 `context_paths` 的任务可以在触发时
    #   将工作区文档预加载到它们的元提示中。
    # - 基于主机 CostGuard 的 BudgetGate，以便当用户耗尽每日 LLM 预算时
    #   拒绝任务触发。
    mission_manager_inner = MissionManager(store_dyn, thread_manager).with_effect_executor(
        effect_adapter
    )
    if agent.workspace() is not None:
        reader = WorkspaceReaderAdapter(agent.workspace())
        mission_manager_inner = mission_manager_inner.with_workspace_reader(reader)

    cost_guard = agent.deps.cost_guard
    budget_gate = CostGuardBudgetGate(cost_guard)
    mission_manager_inner = mission_manager_inner.with_budget_gate(budget_gate)

    # 使用数据库优先的配置系统，而不是原始的 std::env::var 读取。
    # 在可用时从数据库支持的设置解析 MissionsConfig，回退到本地 settings.json + env vars。
    if agent.deps.store is not None:
        try:
            settings_map = await agent.deps.store.get_all_settings(agent.deps.owner_id)
            missions_settings = Settings.from_db_map(settings_map)
        except Exception:
            missions_settings = Settings.load()
    else:
        missions_settings = Settings.load()

    try:
        missions_config = MissionsConfig.resolve(missions_settings)
    except Exception as e:
        logger.warning("MissionsConfig.resolve 失败；回退到默认值: %s", e)
        missions_config = MissionsConfig.default()

    mission_manager_inner = mission_manager_inner.with_insights_interval(
        missions_config.insights_interval
    )
    mission_manager = mission_manager_inner

    try:
        await thread_manager.recover_project_threads(project_id)
    except Exception as e:
        logger.debug("engine v2: recover_project_threads 失败: %s", e)

    try:
        await mission_manager.bootstrap_project(project_id)
    except Exception as e:
        logger.debug("engine v2: bootstrap_project 失败: %s", e)

    try:
        await mission_manager.resume_recoverable_threads(agent.deps.owner_id)
    except Exception as e:
        logger.debug("engine v2: resume_recoverable_threads 失败: %s", e)

    try:
        await thread_manager.resume_background_threads(project_id)
    except Exception as e:
        logger.debug("engine v2: resume_background_threads 失败: %s", e)

    mission_manager.start_cron_ticker(agent.deps.owner_id)
    mission_manager.start_event_listener(agent.deps.owner_id)

    # 订阅任务结果通知并将结果路由到通道
    notification_rx = mission_manager.subscribe_notifications()
    channels = agent.channels
    sse_ref = agent.deps.sse_tx
    db_ref = agent.deps.store
    conv_mgr_ref = conversation_manager
    auth_mgr_ref = agent.deps.auth_manager
    tools_ref = agent.deps.tools
    ext_mgr_ref = agent.deps.extension_manager

    async def _handle_notifications():
        while True:
            try:
                notif = await notification_rx.recv()
            except Exception:
                break
            await handle_mission_notification(
                notif,
                channels,
                sse_ref,
                db_ref,
                conv_mgr_ref,
                auth_mgr_ref,
                tools_ref,
                ext_mgr_ref,
            )

    asyncio.create_task(_handle_notifications())

    # 确保所有者的每用户学习任务存在
    try:
        await mission_manager.ensure_learning_missions(project_id, owner_id)
    except Exception as e:
        logger.debug("engine v2: 创建学习任务失败: %s", e)

    # 将 v1 技能迁移到 v2 MemoryDocs（技能选择在运行时由 Python 编排器通过
    # __list_skills__ 进行）
    if agent.deps.skill_registry is not None:
        registry = agent.deps.skill_registry
        try:
            skills_snapshot = registry.read().skills()
            if skills_snapshot:
                try:
                    count = await migrate_v1_skill_list(
                        skills_snapshot,
                        store_dyn,
                        project_id,
                        owner_id,
                    )
                    if count > 0:
                        logger.debug("engine v2: 迁移了 %d 个 v1 技能", count)
                except Exception as e:
                    logger.debug("engine v2: 技能迁移失败: %s", e)
        except Exception as e:
            logger.debug("engine v2: 读取技能注册表失败: %s", e)

    # 在效果适配器上安装每项目工作区挂载表
    resolver = ProjectPathResolver(store_dyn)
    if engine_v2_sandbox_enabled():
        try:
            docker = await connect_docker()
            logger.debug("engine v2: SANDBOX_ENABLED=true——使用容器化挂载工厂")
            manager = ProjectSandboxManager(docker)
            factory = ContainerizedMountFactory(manager, resolver)
        except Exception as e:
            logger.warning(
                "engine v2: SANDBOX_ENABLED=true 但 Docker 不可达；回退到主机文件系统挂载工厂: %s",
                e,
            )
            factory = FilesystemMountFactory(resolver)
    else:
        factory = FilesystemMountFactory(resolver)

    mounts = WorkspaceMounts(factory)
    await effect_adapter.set_workspace_mounts(mounts)

    # 将任务管理器连接到效果适配器以处理 mission_* 函数调用
    await effect_adapter.set_mission_manager(mission_manager)

    # 将任务管理器连接到代理以处理 /expected 命令
    await agent.set_mission_manager(mission_manager)

    pending_gates = PendingGateStore(
        FileGatePersistence.with_default_path()
    )
    try:
        await pending_gates.restore_from_persistence()
    except Exception as e:
        logger.debug("engine v2: 恢复待处理门控失败: %s", e)

    # 重启清理：来自先前启动的任何进行中的 Approval 门控已丢失其内存中的等待接收器。
    # 回退到遗留重新进入将重新运行 LLM 步骤，并双重执行同一脚本中先前的非幂等工具调用
    # （这正是内联等待路径存在要防止的错误）。在启动时丢弃它们，
    # 以便用户获得干净的重试路径。
    await invalidate_stranded_approval_gates(pending_gates, agent.deps.sse_tx)
    try:
        await reconcile_pending_gate_state(store_dyn, pending_gates)
    except Exception as e:
        logger.debug("engine v2: 待处理门控协调失败: %s", e)

    # 构建每线程外部工具目录。通过 Arc 克隆与效果适配器共享
    # （在每次操作调用时查询），并暴露在引擎状态上，以便 Responses API 处理程序
    # 可以注册/清除调用者提供的工具。
    external_tool_catalog = ExternalToolCatalog()
    await effect_adapter.set_external_tool_catalog(external_tool_catalog)

    # 最后清理：除了 `await_thread_outcome` 中的每线程终端状态清理外，
    # 驱逐早于 `EXTERNAL_TOOL_CATALOG_TTL` 的目录条目以限制内存，
    # 当调用者注册工具然后放弃对话时（例如，在没有恢复待处理门控的情况下断开连接）。
    # 以固定频率运行，以便长期运行的网关不会积累过期条目。
    async def _sweep_external_tool_catalog():
        catalog = external_tool_catalog
        while True:
            await asyncio.sleep(EXTERNAL_TOOL_CATALOG_SWEEP_INTERVAL)
            evicted = await catalog.sweep_older_than(EXTERNAL_TOOL_CATALOG_TTL)
            if evicted:
                logger.debug(
                    "engine v2: 外部工具目录清理驱逐了 %d 个过期条目",
                    len(evicted),
                )

    asyncio.create_task(_sweep_external_tool_catalog())

    resolutions = GateResolutions()
    gate_controller = BridgeGateController(
        pending_gates,
        agent.deps.sse_tx,
        effect_adapter.tools(),
        auth_manager,
        agent.deps.extension_manager,
        channels,
        resolutions,
    )
    await thread_manager.set_gate_controller(gate_controller)

    ENGINE_STATE = EngineState(
        thread_manager=thread_manager,
        conversation_manager=conversation_manager,
        effect_adapter=effect_adapter,
        store=store,
        default_project_id=project_id,
        pending_gates=pending_gates,
        sse=agent.deps.sse_tx,
        db=agent.deps.store,
        secrets_store=agent.tools().secrets_store(),
        auth_manager=auth_manager,
        extension_manager=agent.deps.extension_manager,
        project_root=resolve_project_root(),
        external_tool_catalog=external_tool_catalog,
        capability_registry=capabilities_arc,
        gate_controller=gate_controller,
        gate_resolutions=resolutions,
    )


async def invalidate_stranded_approval_gates(
        pending_gates: PendingGateStore,
        sse: Optional[SseManager],
) -> None:
    """
    启动时清理：使来自先前进程的 `Approval` 类型的待处理门控失效。
    它们的内联等待接收器已消失，重新进入将重新运行先前的非幂等工具调用。
    Auth 和 External 门控保留——它们不依赖于活动 VM。
    """
    restored = await pending_gates.list_all()
    for gate in restored:
        if not isinstance(gate.resume_kind, ResumeKind.Approval):
            continue
        await pending_gates.discard(gate.key())
        if sse is not None:
            sse.broadcast_for_user(
                gate.user_id,
                AppEvent.GateResolved(
                    request_id=str(gate.request_id),
                    gate_name=gate.gate_name,
                    tool_name=gate.action_name,
                    resolution="expired",
                    message="批准因重启而中断。请重试。",
                    thread_id=gate.effective_wire_thread_id(),
                ),
            )  # 投影豁免：桥接调度器，线程存在之前的重启时门控清理


async def resolve_pending_gate_for_user(
        pending_gates: PendingGateStore,
        user_id: str,
        thread_id_hint: Optional[str],
) -> PendingGateResolution:
    """
    解析用户的待处理门控。

    根据 thread_id_hint 过滤候选门控：
    - 优先匹配 scope_thread_id
    - 回退到匹配 thread_id 或 conversation_id 的 UUID
    - 如果有多个候选，选择最近创建的
    - 如果有多个候选但没有有效的提示 UUID，返回 Ambiguous
    """
    hinted_uuid = parse_scope_uuid(thread_id_hint)
    hinted_scope = thread_id_hint

    candidates: List[PendingGate] = []
    for gate in await pending_gates.list_for_user(user_id):
        if hinted_scope is None:
            candidates.append(gate)
        elif (
                gate.scope_thread_id == hinted_scope
                or (
                        hinted_uuid is not None
                        and (
                                gate.thread_id == hinted_uuid
                                or gate.conversation_id == hinted_uuid
                        )
                )
        ):
            candidates.append(gate)

    if len(candidates) == 0:
        return PendingGateResolutionNone()
    elif len(candidates) == 1:
        return PendingGateResolutionResolved(candidates[0])
    elif hinted_uuid is not None:
        # 多个候选，选择最近创建的
        gate_latest = max(candidates, key=lambda gate: gate.created_at)
        return PendingGateResolutionResolved(gate_latest)
    else:
        return PendingGateResolutionAmbiguous()


async def get_engine_pending_gate(
        user_id: str,
        thread_id: Optional[str],
) -> Optional[PendingGateView]:
    """获取用户的引擎待处理门控视图。"""
    lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
    if lock is None:
        return None
    guard = await lock.read()
    if guard is None:
        return None

    resolution = await resolve_pending_gate_for_user(
        guard.pending_gates, user_id, thread_id
    )
    if isinstance(resolution, PendingGateResolution.Resolved):
        return PendingGateView.from_gate(resolution.gate)
    return None


async def get_pending_gate_by_request_id(
        user_id: str,
        request_id: uuid.UUID,
) -> Optional[PendingGateView]:
    """
    按 `request_id` 只读查找待处理门控，范围限定为请求用户。
    由聊天取消处理程序用于在客户端在解析负载中省略 `thread_id` 时
    恢复拥有线程——没有这个，前台内联等待门控将被搁浅
    （门控标记为已取消，暂停的 VM 永远不会展开）。参见 PR #3366 审查。
    """
    lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
    if lock is None:
        return None
    guard = await lock.read()
    if guard is None:
        return None
    return await guard.pending_gates.peek_by_request_id(request_id, user_id)


async def has_any_pending_gate(
        user_id: str,
        thread_id: Optional[str],
) -> bool:
    """
    检查用户是否有任何待处理门控（已解析、模糊或其他）。
    与 `get_engine_pending_gate` 不同，后者对模糊解析返回 `None`，
    此函数只要至少存在一个门控就返回 `True`——
    适用于决定裸关键字应被视为批准响应还是常规用户输入。
    """
    lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
    if lock is None:
        return False
    try:
        guard = await lock.read()
    except Exception:
        return False
    if guard is None:
        return False
    resolution = await resolve_pending_gate_for_user(
        guard.pending_gates, user_id, thread_id
    )
    return not isinstance(resolution, PendingGateResolution.
    None)

    class AuthCallbackContinuation:
        """认证回调的延续操作类型。"""
        None_ = "none"
        ResolveGateExternal = "resolve_gate_external"
        ReplayMessage = "replay_message"

    async def resolve_engine_auth_callback(
            user_id: str,
            credential_name: str,
    ) -> AuthCallbackContinuation:
        """
    解析引擎认证回调。

    查找匹配 credential_name 的待处理认证门控：
    - 如果操作是 "authentication_fallback" 且有原始消息，则重放消息
    - 否则返回 ResolveGateExternal 以通过外部回调解析门控
    """
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return AuthCallbackContinuation.
            None
        guard = await lock.read()
        if guard is None:
            return AuthCallbackContinuation.
            None

        matching: List[PendingGate] = []
        for gate in await guard.pending_gates.list_for_user(user_id):
            if (
                    isinstance(gate.resume_kind, ResumeKind.Authentication)
                    and gate.resume_kind.credential_name == credential_name
            ):
                matching.append(gate)

        matching.sort(key=lambda gate: gate.created_at)
        if not matching:
            return AuthCallbackContinuation.
            None

        pending = matching.pop()

        if pending.action_name == "authentication_fallback":
            if pending.original_message is not None:
                content = pending.original_message
                # 消费门控，以便重复的 OAuth 回调无法重放它
                key = pending.key()
                await guard.pending_gates.discard(key)
                return AuthCallbackContinuation.ReplayMessage(
                    channel=pending.source_channel,
                    thread_scope=pending.scope_thread_id,
                    content=content,
                )
            logger.debug(
                "OAuth 回调匹配了没有可重放请求的认证回退, user_id=%s, credential_name=%s, thread_id=%s",
                user_id,
                credential_name,
                pending.thread_id,
            )
            return AuthCallbackContinuation.
            None

        return AuthCallbackContinuation.ResolveGateExternal(
            channel=pending.source_channel,
            thread_scope=pending.scope_thread_id,
            request_id=pending.request_id,
        )

    async def resolve_inline_gates_for_credential(
            user_id: str,
            credential_name: str,
    ) -> int:
        """
    唤醒所有在 `(user_id, credential_name)` 对上暂停的
    Tier 0/Tier 1 内联等待等待者。

    #3133 的第二部分，内联等待分支。Tier 1 (CodeAct) 和 Tier 0 (结构化)
    路径现在对 Authentication 门控保持其 VM/batch 暂停在
    `GateController.pause()` 上，与 Approval 相同。
    当 OAuth 收到凭证时，此辅助函数向 `user_id` 的每个暂停等待者
    投递 `GateResolution.Approved`，以便暂停的操作内联重试
    针对现在存在的密钥——无需线程重新进入，无需重放同一步骤中
    先前的副作用。同一凭证名称上其他用户的暂停等待者保持不变。

    返回唤醒的等待者数量（零是正常的——大多数凭证写入不会解除任何内联 VM）。
    """
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return 0
        guard = await lock.read()
        if guard is None:
            return 0

        woken = await guard.gate_resolutions.deliver_for_credential(
            user_id, credential_name
        )
        if woken > 0:
            logger.debug(
                "在凭证写入时向暂停的内联等待等待者投递了 Approved, user=%s, credential=%s, woken=%d",
                user_id,
                credential_name,
                woken,
            )
            # #3533：同时从存储中删除匹配的 Authentication 待处理门控行。
            # 内联等待重试将在凭证现在存在的情况下运行，并成功或引发自己的后续门控；
            # 原始的 Authentication 行不再代表活动状态，否则会一直保留到过期
            # （并在没有后续门控的用户中出现在 `HistoryResponse.pending_gate` 中）。
            # 没有此删除，外部回调路径 (`resolve_engine_auth_callback`) 是唯一清理的途径——
            # 跳过该路径以避免"线程已在运行"竞争会使该行成为孤立行。
            matching: List[PendingGate] = []
            for gate in await guard.pending_gates.list_for_user(user_id):
                if (
                        isinstance(gate.resume_kind, ResumeKind.Authentication)
                        and gate.resume_kind.credential_name == credential_name
                ):
                    matching.append(gate)

            for gate in matching:
                await guard.pending_gates.discard(gate.key())

        return woken

        async def resume_paused_missions_for_credential(
                user_id: str,
                credential_name: str,
        ) -> int:
            """
        在凭证写入后自动恢复暂停的任务。

        查找匹配 credential_name 的凭证，并恢复所有因此暂停的任务。
        返回恢复的任务数量。
        """
            lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
            if lock is None:
                return 0
            guard = await lock.read()
            if guard is None:
                return 0

            mission_manager = await guard.effect_adapter.mission_manager()
            if mission_manager is None:
                return 0

            cred = CredentialName.new(credential_name)
            if cred is None:
                logger.debug(
                    "跳过任务自动恢复——凭证名称验证失败, credential_name=%s",
                    credential_name,
                )
                return 0

            try:
                ids = await mission_manager.resume_paused_for_credential(cred, user_id)
                if ids:
                    logger.debug(
                        "凭证写入后自动恢复了暂停的任务, user_id=%s, credential=%s, resumed=%d",
                        user_id,
                        credential_name,
                        len(ids),
                    )
                return len(ids)
            except Exception as e:
                logger.warning(
                    "凭证写入后自动恢复暂停的任务失败, user_id=%s, credential=%s, error=%s",
                    user_id,
                    credential_name,
                    e,
                )
                return 0

        async def resume_paused_missions_for_gate_request(
                user_id: str,
                gate_request_id: uuid.UUID,
                outcome: GateResolutionOutcome,
        ) -> Optional[MissionId]:
            """
        根据用户驱动的门控解析结果，自动恢复其 `paused_gate.gate_request_id`
        匹配 `gate_request_id` 的暂停任务。

        #3133 的批准/外部路径的第二部分。在前台门控解析后从
        `/api/chat/gate/resolve` 调用。对于 `Approved`，任务转换为 `Paused → Active`，
        并（对于非手动频率）立即触发。对于 `Denied`/`Cancelled`，任务标记为 `Failed`，
        以便用户修复底层问题并手动恢复。

        返回恢复/失败的任务 id，如果没有暂停任务等待此门控则返回 `None`
        （仅前台门控被解析）。
        """
            lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
            if lock is None:
                return None
            guard = await lock.read()
            if guard is None:
                return None

            mission_manager = await guard.effect_adapter.mission_manager()
            if mission_manager is None:
                return None

            try:
                result = await mission_manager.resume_paused_for_request_id(
                    gate_request_id, outcome, user_id
                )
                if result is not None:
                    logger.debug(
                        "门控解析后任务自动恢复, user_id=%s, gate_request_id=%s, mission_id=%s, outcome=%s",
                        user_id,
                        gate_request_id,
                        result,
                        outcome,
                    )
                return result
            except Exception as e:
                logger.warning(
                    "门控解析后自动恢复暂停的任务失败, user_id=%s, gate_request_id=%s, error=%s",
                    user_id,
                    gate_request_id,
                    e,
                )
                return None

        async def handle_approval(
                agent: Agent,
                message: IncomingMessage,
                approved: bool,
                always: bool,
        ) -> BridgeOutcome:
            """
        处理引擎 v2 的批准响应（yes/no/always）。

        当用户响应批准请求时从 `handle_message` 调用。
        """
            await init_engine(agent)

            lock = ENGINE_STATE.get()
            if lock is None:
                raise engine_err("init", "引擎状态未初始化")
            guard = await lock.read()
            if guard is None:
                raise engine_err("init", "引擎状态为空")

            # 在可用时将显式批准回复范围限定到活动的网关对话，
            # 以便 `/approve` 无法恢复属于另一个线程（例如后台例行任务）的
            # 不相关待处理门控。其他通道仍然使用不与引擎对话范围 1:1 映射的遗留线程 ID。
            thread_scope = (
                message.conversation_scope() if message.channel == "gateway" else None
            )
            resolution = await resolve_pending_gate_for_user(
                guard.pending_gates, message.user_id, thread_scope
            )

            if isinstance(resolution, PendingGateResolution.None):
                logger.debug(
                    "engine v2: 用户没有待处理的批准，忽略, user_id=%s",
                    message.user_id,
                )
                return BridgeOutcome.Respond("此线程没有待处理的批准。")

            if isinstance(resolution, PendingGateResolution.Ambiguous):
                return BridgeOutcome.Respond(
                    "多个待处理的门控正在等待。请从原始线程解析，或在选择该线程后重试。"
                )

            pending = resolution.gate

            if not isinstance(pending.resume_kind, ResumeKind.Approval):
                return BridgeOutcome.Respond(
                    "选定的待处理门控不是批准请求。"
                )

            request_id = pending.request_id
            thread_id = pending.thread_id
            guard.release()

            return await resolve_gate(
                agent,
                message,
                thread_id,
                request_id,
                GateResolution.Approved(always=always) if approved else GateResolution.Denied(reason=None),
            )

        async def handle_exec_approval(
                agent: Agent,
                message: IncomingMessage,
                request_id: uuid.UUID,
                approved: bool,
                always: bool,
        ) -> BridgeOutcome:
            """
        处理 `ExecApproval` 提交（带有显式 request_id 的 Web 网关 JSON 批准）。
        """
            await init_engine(agent)

            resolution = (
                GateResolution.Approved(always=always)
                if approved
                else GateResolution.Denied(reason=None)
            )

            # 首先尝试对话范围提示快捷路径
            thread_id = await hinted_pending_gate_thread_id(
                message.user_id,
                message.conversation_scope(),
                request_id,
                gate_view_is_approval,
            )
            if thread_id is not None:
                return await resolve_gate(agent, message, thread_id, request_id, resolution)

            # 回退到全量扫描
            thread_id = await pending_gate_thread_id_for_request(
                message.user_id, request_id, gate_is_approval
            )
            if thread_id is not None:
                return await resolve_gate(agent, message, thread_id, request_id, resolution)

            logger.debug(
                "engine v2: 未找到匹配 request_id 的待处理批准, user_id=%s, request_id=%s",
                message.user_id,
                request_id,
            )
            return BridgeOutcome.Respond("未找到匹配的待处理批准。")

        async def handle_external_callback(
                agent: Agent,
                message: IncomingMessage,
                request_id: uuid.UUID,
                payload: Optional[Dict[str, Any]],
        ) -> BridgeOutcome:
            """
        处理外部回调（OAuth/配对回调或 Responses API 调用者执行的工具结果）。
        """
            await init_engine(agent)

            resolution = GateResolution.ExternalCallback(
                payload=payload if payload is not None else None
            )

            # 认证风格的回调（遗留 OAuth/配对）：首先查询认证谓词，
            # 包括对话范围提示快捷路径。
            thread_id = await hinted_pending_gate_thread_id(
                message.user_id,
                message.conversation_scope(),
                request_id,
                gate_view_is_authentication,
            )
            if thread_id is not None:
                return await resolve_gate(agent, message, thread_id, request_id, resolution)

            thread_id = await pending_gate_thread_id_for_request(
                message.user_id, request_id, gate_is_authentication
            )
            if thread_id is not None:
                return await resolve_gate(agent, message, thread_id, request_id, resolution)

            # 非认证外部回调（例如 Responses API 调用者执行的工具结果）：
            # 门控的 resume_kind 是 `External`，但它不是认证门控，
            # 因此上面的认证谓词不匹配它。
            thread_id = await pending_gate_thread_id_for_request(
                message.user_id, request_id, gate_resume_is_external
            )
            if thread_id is not None:
                return await resolve_gate(agent, message, thread_id, request_id, resolution)

            logger.debug(
                "engine v2: 未找到匹配外部回调的待处理门控, user_id=%s, request_id=%s",
                message.user_id,
                request_id,
            )
            return BridgeOutcome.Respond("未找到匹配的待处理门控。")

        async def handle_auth_gate_resolution(
                agent: Agent,
                message: IncomingMessage,
                request_id: uuid.UUID,
                resolution: AuthGateResolution,
        ) -> BridgeOutcome:
            """
        处理认证门控解析（凭证提供或取消）。
        """
            await init_engine(agent)

            if isinstance(resolution, AuthGateResolution.CredentialProvided):
                gate_resolution = GateResolution.CredentialProvided(token=resolution.token)
            else:
                gate_resolution = GateResolution.Cancelled

            # 首先尝试对话范围提示快捷路径
            thread_id = await hinted_pending_gate_thread_id(
                message.user_id,
                message.conversation_scope(),
                request_id,
                gate_view_is_authentication,
            )
            if thread_id is not None:
                return await resolve_gate(agent, message, thread_id, request_id, gate_resolution)

            # 回退到全量扫描
            thread_id = await pending_gate_thread_id_for_request(
                message.user_id, request_id, gate_is_authentication
            )
            if thread_id is not None:
                return await resolve_gate(agent, message, thread_id, request_id, gate_resolution)

            logger.debug(
                "engine v2: 未找到匹配 request_id 的待处理认证门控, user_id=%s, request_id=%s",
                message.user_id,
                request_id,
            )
            return BridgeOutcome.Respond("未找到匹配的待处理认证门控。")

        def gate_is_approval(gate: PendingGate) -> bool:
            """检查门控是否为批准类型。"""
            return isinstance(gate.resume_kind, ResumeKind.Approval)

        def gate_is_authentication(gate: PendingGate) -> bool:
            """检查门控是否为认证类型。"""
            return isinstance(gate.resume_kind, ResumeKind.Authentication)

        def gate_resume_is_external(gate: PendingGate) -> bool:
            """
        匹配任何 resume kind 为 `External` 的门控。
        用作 `handle_external_callback` 中的回退，以恢复非认证工具调用暂停
        （例如 Responses API 调用者执行的工具结果路径），
        这些暂停从不经过认证谓词。
        """
            return isinstance(gate.resume_kind, ResumeKind.External)

        def gate_view_is_approval(gate: PendingGateView) -> bool:
            """检查门控视图是否为批准类型。"""
            return isinstance(gate.resume_kind, ResumeKind.Approval)

        def gate_view_is_authentication(gate: PendingGateView) -> bool:
            """检查门控视图是否为认证类型。"""
            return isinstance(gate.resume_kind, ResumeKind.Authentication)

        async def hinted_pending_gate_thread_id(
                user_id: str,
                conversation_scope: Optional[str],
                request_id: uuid.UUID,
                predicate: Callable[[PendingGateView], bool],
        ) -> Optional[ThreadId]:
            """
        通过对话范围提示查找待处理门控的线程 ID。

        如果对话范围可以解析为线程 ID，则直接在该线程中查找匹配的门控。
        这是 O(1) 的快捷路径，避免了全量扫描。
        """
            thread_id = parse_engine_thread_id(conversation_scope)
            if thread_id is None:
                return None

            lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
            if lock is None:
                raise engine_err("init", "引擎状态未初始化")
            guard = await lock.read()
            if guard is None:
                raise engine_err("init", "引擎状态为空")

            gate = await guard.pending_gates.peek(
                PendingGateKey(user_id=user_id, thread_id=thread_id)
            )
            guard.release()

            if gate is not None and str(gate.request_id) == str(request_id) and predicate(gate):
                return thread_id
            return None

        async def pending_gate_thread_id_for_request(
                user_id: str,
                request_id: uuid.UUID,
                predicate: Callable[[PendingGate], bool],
        ) -> Optional[ThreadId]:
            """
        按 request_id 和谓词查找待处理门控的线程 ID。

        遍历用户的所有待处理门控，找到第一个匹配 request_id 和谓词的门控。
        """
            lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
            if lock is None:
                raise engine_err("init", "引擎状态未初始化")
            guard = await lock.read()
            if guard is None:
                raise engine_err("init", "引擎状态为空")

            pending = None
            for gate in await guard.pending_gates.list_for_user(user_id):
                if gate.request_id == request_id and predicate(gate):
                    pending = gate
                    break
            guard.release()

            return pending.thread_id if pending is not None else None

        class InlineGateOutcome(Enum):
            """
        快速路径内联门控解析尝试的结果。

        参见 [`try_resolve_inline_approval_gate`]。
        """
            # 解析被直接投递到暂停的引擎 VM。待处理门控已被消费；
            # SSE `GateResolved` 已广播。
            Delivered = "delivered"
            # 没有活动 VM 等待此门控（引擎未初始化、没有匹配的暂停未来、
            # 或非 Approval 恢复类型）。待处理门控被保留在原位——
            # 调用者应回退到遗留 mpsc 分派路径，以便代理循环的 `resolve_gate`
            # 可以正常恢复线程。
            NoLiveVm = "no_live_vm"

        class InlineGateError(Exception):
            """
        [`try_resolve_inline_approval_gate`] 的验证失败，必须作为 4xx HTTP 响应显示，
        而不是回退到遗留恢复路径。变体映射到 HTTP 边界的特定状态码
        （参见 `chat_approval_handler`）：

        - [`InlineGateError.ChannelMismatch`] → 403 Forbidden
        - [`InlineGateError.Stale`] → 409 Conflict（request_id 已解析或与最新的待处理行不匹配）
        - [`InlineGateError.Expired`] → 409 Conflict（待处理门控的 `expires_at` 已过）
        - [`InlineGateError.Unauthorized`] → 403 Forbidden
        - [`InlineGateError.Other`] → 500 Internal Server Error

        在 API 边界进行类型化（而不是依赖 `error.to_string().contains("authorization")`），
        以便未来对错误格式字符串的更改不会静默地将 403 翻转为 500。
        """

            def __init__(
                    self,
                    kind: str,
                    message: str,
                    expected: Optional[str] = None,
                    actual: Optional[str] = None,
            ):
                self.kind = kind
                self.expected = expected
                self.actual = actual
                super().__init__(message)

            @classmethod
            def channel_mismatch(cls, expected: str, actual: str) -> "InlineGateError":
                """解析通道与发起门控的通道不匹配（且不在可信通道允许列表中）。"""
                return cls(
                    kind="channel_mismatch",
                    message=f"通道 '{actual}' 无法解析来自通道 '{expected}' 的门控",
                    expected=expected,
                    actual=actual,
                )

            @classmethod
            def stale(cls) -> "InlineGateError":
                """request_id 与活动的待处理门控不匹配（已解析、已丢弃或被新门控行替换）。"""
                return cls(
                    kind="stale",
                    message="批准请求已过时或已被解析",
                )

            @classmethod
            def expired(cls) -> "InlineGateError":
                """待处理门控的 `expires_at` 已过。"""
                return cls(
                    kind="expired",
                    message="批准请求已过期",
                )

            @classmethod
            def unauthorized(cls) -> "InlineGateError":
                """
            待处理门控存在但不属于请求用户。
            显示为 403 以避免跨租户泄漏门控存在性。
            """
                return cls(
                    kind="unauthorized",
                    message="无权解析此门控",
                )

            @classmethod
            def other(cls, message: str) -> "InlineGateError":
                """任何其他门控存储故障。"""
                return cls(
                    kind="other",
                    message=f"门控错误: {message}",
                )

        async def try_resolve_inline_approval_gate(
                user_id: str,
                channel: str,
                request_id: uuid.UUID,
                resolution: GateResolution,
                settings_store: Optional[SettingsStore],
        ) -> InlineGateOutcome:
            """
        尝试通过内联等待快速路径解析批准门控。

        只有 Approval 形状的解析才有资格进行内联等待。
        `BridgeGateController::pause` 对 Authentication 和 External 恢复类型
        立即返回 Cancelled 而不暂停，因此没有可投递的目标，
        我们只需要遗留恢复路径。
        """
            # 只有 Approval 形状的解析才有资格进行内联等待
            if not isinstance(resolution, (GateResolution.Approved, GateResolution.Denied)) and not isinstance(
                    resolution,
                    GateResolution.Cancelled):
                return InlineGateOutcome.NoLiveVm

            lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
            if lock is None:
                return InlineGateOutcome.NoLiveVm
            guard = await lock.read()
            if guard is None:
                return InlineGateOutcome.NoLiveVm

            # 按 `request_id`（系统范围内唯一）解析门控，而不是按调用者提供的线程标识符。
            # HTTP 表面上的线 `req.thread_id` 是通道可见的值——对于 Web 网关，
            # 是由 `/api/chat/thread/new` 返回的每对话 UUID，在门控上记录为
            # `scope_thread_id`——而不是键控 `PendingGateStore` 的内部引擎 `ThreadId`。
            # 在存储的单个互斥锁下按 `request_id` 查找保持查找 + 删除原子性，
            # 并避免线 vs. 引擎标识符混淆，否则会错过每个通道范围与其引擎线程不同的门控。
            try:
                pending = await guard.pending_gates.take_verified_by_request_id(
                    request_id, user_id, channel
                )
            except GateStoreError.NotFound:
                return InlineGateOutcome.NoLiveVm
            except GateStoreError.ChannelMismatch as e:
                raise InlineGateError.channel_mismatch(e.expected, e.actual)
            except GateStoreError.Unauthorized:
                raise InlineGateError.unauthorized()
            except GateStoreError.Expired:
                raise InlineGateError.expired()
            except GateStoreError.RequestIdMismatch:
                raise InlineGateError.stale()
            except Exception as e:
                raise InlineGateError.other(str(e))

            thread_id = pending.thread_id

            # 只有 Approval 恢复门控由门控控制器暂停。非 Approval 门控
            # 命中此处的 take_verified 意味着不同的 resume_kind 碰巧共享了 request_id——
            # 重新插入并告诉调用者回退到遗留恢复。
            if not isinstance(pending.resume_kind, ResumeKind.Approval):
                try:
                    await guard.pending_gates.insert(pending)
                except Exception as e:
                    logger.debug(
                        "try_resolve_inline_approval_gate: 重新插入非 Approval 门控失败, user_id=%s, thread_id=%s, error=%s",
                        user_id,
                        thread_id,
                        e,
                    )
                return InlineGateOutcome.NoLiveVm

            always_for_inline = (
                clamp_always_to_resume_kind(resolution.always, pending.resume_kind)
                if isinstance(resolution, GateResolution.Approved)
                else False
            )

            legacy_registry_name = legacy_extension_alias(pending.action_name)
            prior_permission = None
            if always_for_inline:
                await guard.effect_adapter.auto_approve_tool(pending.action_name)
                if legacy_registry_name is not None:
                    await guard.effect_adapter.auto_approve_tool(legacy_registry_name)
                prior_permission = await persist_always_allow_with_store(
                    settings_store, guard, pending
                )

            inline_resolution = resolution  # 对于非 Approved 情况保持不变
            if isinstance(resolution, GateResolution.Approved):
                inline_resolution = GateResolution.Approved(always=always_for_inline)

            if await guard.gate_controller.try_deliver(request_id, inline_resolution):
                if guard.sse is not None:
                    if isinstance(resolution, GateResolution.Approved):
                        label = "approved_always" if always_for_inline else "approved"
                        status_msg = "门控已批准。正在恢复执行。"
                    elif isinstance(resolution, GateResolution.Denied):
                        label = "denied"
                        status_msg = "门控已拒绝。"
                    else:
                        label = "cancelled"
                        status_msg = "门控已取消。"

                    guard.sse.broadcast_for_user(
                        user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution=label,
                            message=status_msg,
                            thread_id=pending.effective_wire_thread_id(),
                        ),
                    )  # 投影豁免：桥接调度器，内联等待快速路径解析事件
                return InlineGateOutcome.Delivered

            # try_deliver 返回 false：没有为此 request_id 暂停的 future。
            # 回滚我们安装的自动批准首选项，并重新插入待处理门控，
            # 以便遗留 mpsc 分派路径可以找到它。
            if always_for_inline:
                await guard.effect_adapter.revoke_auto_approve(pending.action_name)
                if legacy_registry_name is not None:
                    await guard.effect_adapter.revoke_auto_approve(legacy_registry_name)
                await revert_always_allow_with_store(settings_store, pending, prior_permission)

            try:
                await guard.pending_gates.insert(pending)
            except Exception as e:
                logger.debug(
                    "try_resolve_inline_approval_gate: 无活动 VM 后重新插入待处理门控失败, user_id=%s, thread_id=%s, error=%s",
                    user_id,
                    thread_id,
                    e,
                )
            return InlineGateOutcome.NoLiveVm

        async def resolve_gate(
                agent: Agent,
                message: IncomingMessage,
                thread_id: ThreadId,
                request_id: uuid.UUID,
                resolution: GateResolution,
        ) -> BridgeOutcome:
            """
        解析统一的待处理门控。

        这是解析存储在 [`PendingGateStore`] 中的门控的单一入口点。
        它在恢复或停止线程之前原子性地验证 request_id、通道授权和过期时间。

        使用统一门控抽象替换了新代码路径的单独批准和认证解析路径。
        """
            await init_engine(agent)

            lock = ENGINE_STATE.get()
            if lock is None:
                raise engine_err("init", "引擎状态未初始化")
            guard = await lock.read()
            if guard is None:
                raise engine_err("init", "引擎状态为空")

            key = PendingGateKey(user_id=message.user_id, thread_id=thread_id)

            try:
                pending = await guard.pending_gates.take_verified(
                    key, request_id, message.channel
                )
            except GateStoreError.ChannelMismatch as e:
                raise engine_err(
                    "授权",
                    f"通道 '{e.actual}' 无法解析来自通道 '{e.expected}' 的门控",
                )
            except GateStoreError.RequestIdMismatch:
                raise engine_err("过时", "批准请求已过时或已被解析")
            except GateStoreError.Expired:
                raise engine_err("已过期", "批准请求已过期")
            except Exception as e:
                raise engine_err("门控", e)

            # 内联门控等待快速路径：如果引擎正在主动等待此门控
            # （活动 Tier 0 批处理或 Tier 1 CodeAct VM），则通过控制器的
            # 内存通道将解析返回。引擎从确切的暂停点继续——
            # 无需重新进入、无需重放、无需双重执行同一步骤中先前的非幂等工具调用。
            #
            # 我们仍然在投递之前安装任何自动批准首选项，以便同一执行中的
            # 后续门控看到策略 `Allow` 而不是再次门控。
            if isinstance(resolution, (GateResolution.Approved, GateResolution.Denied, GateResolution.Cancelled)):
                always_for_inline = (
                    clamp_always_to_resume_kind(resolution.always, pending.resume_kind)
                    if isinstance(resolution, GateResolution.Approved)
                    else False
                )

                legacy_registry_name = legacy_extension_alias(pending.action_name)
                prior_permission = None
                if always_for_inline:
                    await guard.effect_adapter.auto_approve_tool(pending.action_name)
                    if legacy_registry_name is not None:
                        await guard.effect_adapter.auto_approve_tool(legacy_registry_name)
                    prior_permission = await persist_always_allow(agent, guard, pending)

                inline_resolution = resolution
                if isinstance(resolution, GateResolution.Approved):
                    inline_resolution = GateResolution.Approved(always=always_for_inline)

                if await guard.gate_controller.try_deliver(request_id, inline_resolution):
                    if guard.sse is not None:
                        if isinstance(resolution, GateResolution.Approved):
                            label = "approved_always" if always_for_inline else "approved"
                            status_msg = "门控已批准。正在恢复执行。"
                        elif isinstance(resolution, GateResolution.Denied):
                            label = "denied"
                            status_msg = "门控已拒绝。"
                        else:
                            label = "cancelled"
                            status_msg = "门控已取消。"

                        guard.sse.broadcast_for_user(
                            message.user_id,
                            AppEvent.GateResolved(
                                request_id=str(pending.request_id),
                                gate_name=pending.gate_name,
                                tool_name=pending.action_name,
                                resolution=label,
                                message=status_msg,
                                thread_id=pending.effective_wire_thread_id(),
                            ),
                        )  # 投影豁免：桥接调度器，内联等待快速路径解析事件
                    return BridgeOutcome.Pending

                # 投递失败——没有活动 VM 在等待（进程重启，或门控是通过未注册
                # 内联等待接收器的代码路径创建的）。回滚我们刚刚安装的任何自动批准，
                # 以便后续调用不会看到过时的首选项，然后穿透到下面的遗留重新进入路径。
                if always_for_inline:
                    await guard.effect_adapter.revoke_auto_approve(pending.action_name)
                    if legacy_registry_name is not None:
                        await guard.effect_adapter.revoke_auto_approve(legacy_registry_name)
                    await revert_always_allow(agent, pending, prior_permission)

            # 根据解析类型处理
            if isinstance(resolution, GateResolution.Approved):
                always = clamp_always_to_resume_kind(resolution.always, pending.resume_kind)

                # 飞行前线程检查，在提交 `AlwaysAllow` 持久化之前 (#2347)：
                # 如果线程在 `take_verified` 和现在之间被删除，持久化自动批准
                # 会为从未运行的工具留下永久首选项。此分支底部的回滚仅在 `Err` 上触发，
                # 因此 `execute_pending_gate_action` 对缺失线程的优雅 `Ok(Respond)` 会绕过它。
                # 在此处短路。
                try:
                    thread = await guard.store.load_thread(pending.thread_id)
                except Exception as e:
                    raise engine_err("加载线程", e)

                if thread is None:
                    return emit_gate_expired_dismissal(guard, message, pending)

                if guard.sse is not None:
                    guard.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="approved_always" if always else "approved",
                            message="门控已批准。正在恢复执行。",
                            thread_id=pending.effective_wire_thread_id(),
                        ),
                    )

                legacy_registry_name = legacy_extension_alias(pending.action_name)
                prior_permission = None
                if always:
                    await guard.effect_adapter.auto_approve_tool(pending.action_name)
                    if legacy_registry_name is not None:
                        await guard.effect_adapter.auto_approve_tool(legacy_registry_name)
                    prior_permission = await persist_always_allow(agent, guard, pending)

                result = await execute_pending_gate_action(
                    agent,
                    guard,
                    message,
                    pending,
                    True,
                    (pending.call_id, True),
                )

                if always and isinstance(result, Exception):
                    await guard.effect_adapter.revoke_auto_approve(pending.action_name)
                    if legacy_registry_name is not None:
                        await guard.effect_adapter.revoke_auto_approve(legacy_registry_name)
                    await revert_always_allow(agent, pending, prior_permission)

                return result

            elif isinstance(resolution, GateResolution.Denied):
                if guard.sse is not None:
                    guard.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="denied",
                            message="门控已拒绝。",
                            thread_id=pending.effective_wire_thread_id(),
                        ),
                    )

                try:
                    await agent.channels.send_status(
                        message.channel,
                        StatusUpdate.Status("工具调用已拒绝。"),
                        message.metadata,
                    )
                except Exception:
                    pass

                reason_text = f" 原因: {resolution.reason}" if resolution.reason else ""
                deny_msg = ThreadMessage.user(
                    f"用户拒绝了操作 '{pending.action_name}'。不要重试；选择不同的方法。{reason_text}"
                )

                guard.effect_adapter.reset_call_count()
                await guard.thread_manager.resume_thread(
                    pending.thread_id,
                    message.user_id,
                    deny_msg,
                    (pending.call_id, False),
                    None,
                )

            elif isinstance(resolution, GateResolution.Cancelled):
                if guard.sse is not None:
                    guard.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="cancelled",
                            message="门控已取消。",
                            thread_id=pending.effective_wire_thread_id(),
                        ),
                    )

                try:
                    await guard.thread_manager.stop_thread(pending.thread_id, message.user_id)
                except Exception as e:
                    logger.debug("取消时停止线程失败: %s", e)

                return BridgeOutcome.Respond("已取消。")

            elif isinstance(resolution, GateResolution.CredentialProvided):
                if not isinstance(pending.resume_kind, ResumeKind.Authentication):
                    raise engine_err(
                        "解析不匹配",
                        "为非认证门控发送了 CredentialProvided",
                    )

                credential_name = pending.resume_kind.credential_name

                submit_target = await resolve_extension_for_action(
                    guard.auth_manager,
                    guard.extension_manager,
                    guard.effect_adapter.tools(),
                    pending.action_name,
                    pending.parameters,
                    credential_name,
                    message.user_id,
                )
                display_name = submit_target

                if guard.sse is not None:
                    guard.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="credential_provided",
                            message="凭证已收到。正在恢复执行。",
                            thread_id=pending.effective_wire_thread_id(),
                        ),
                    )

                submission = await submit_pending_auth_credential(
                    guard,
                    str(submit_target),
                    credential_name,
                    resolution.token,
                    message.user_id,
                )

                # 根据提交结果处理
                if isinstance(submission, PendingAuthCredentialSubmission.Stored):
                    result = submission.result
                    if classify_configure_result(result) == ConfigureFlowOutcome.Ready:
                        try:
                            await agent.channels.send_status(
                                message.channel,
                                StatusUpdate.AuthCompleted(
                                    extension_name=display_name,
                                    success=True,
                                    message=format_auth_completed_resuming(result.message),
                                ),
                                message.metadata,
                            )
                        except Exception:
                            pass
                    elif classify_configure_result(result) == ConfigureFlowOutcome.PairingRequired:
                        next_pending = await requeue_pairing_pending_gate(
                            guard, pending, str(display_name)
                        )
                        if guard.sse is not None:
                            guard.sse.broadcast_for_user(
                                message.user_id,
                                OnboardingStateDto.pairing_required(
                                    display_name,
                                    str(next_pending.request_id),
                                    pending.effective_wire_thread_id(),
                                    result.message,
                                    result.instructions,
                                    result.onboarding,
                                ),
                            )
                        return BridgeOutcome.Pending
                    elif classify_configure_result(result) in (
                            ConfigureFlowOutcome.AuthRequired,
                            ConfigureFlowOutcome.RetryAuth,
                    ):
                        return await requeue_auth_pending_gate(
                            agent,
                            guard,
                            message,
                            pending,
                            result.message,
                            result.auth_url,
                        )
                elif isinstance(submission, PendingAuthCredentialSubmission.SkippedNoBackend):
                    if pending.resume_output is not None:
                        logger.debug(
                            "认证门控恢复：无后端，令牌已丢弃，因为 resume_output 已准备, user_id=%s, thread_id=%s, request_id=%s",
                            message.user_id,
                            pending.thread_id,
                            pending.request_id,
                        )
                    else:
                        msg = "没有可用的认证管理器、扩展管理器或密钥存储来存储凭证。"
                        await fail_waiting_thread(guard, message.user_id, pending.thread_id, msg)
                        try:
                            await agent.channels.send_status(
                                message.channel,
                                StatusUpdate.AuthCompleted(
                                    extension_name=display_name,
                                    success=False,
                                    message=msg,
                                ),
                                message.metadata,
                            )
                        except Exception:
                            pass
                        return BridgeOutcome.Respond(msg)

                # 处理认证回退重放
                if pending.action_name == "authentication_fallback" and pending.original_message is not None:
                    retry_content = pending.original_message
                    retry_msg = IncomingMessage(
                        content=retry_content,
                        channel=pending.source_channel,
                        user_id=pending.user_id,
                        metadata=message.metadata,
                    )
                    guard.release()
                    return await handle_with_engine_inner(agent, retry_msg, retry_content, 1)

                # 使用 resume_output 或执行待处理操作
                if pending.resume_output is not None:
                    resolved_call_id = await resolved_or_synthetic_call_id_for_pending_action(
                        guard, pending
                    )
                    await guard.thread_manager.resume_thread(
                        pending.thread_id,
                        message.user_id,
                        resumed_action_result_message(
                            resolved_call_id,
                            pending.action_name,
                            pending.resume_output,
                        ),
                        None,
                        resolved_call_id,
                    )
                else:
                    return await execute_pending_gate_action(
                        agent,
                        guard,
                        message,
                        pending,
                        pending.approval_already_granted,
                        None,
                    )

            elif isinstance(resolution, GateResolution.ExternalCallback):
                if guard.sse is not None:
                    guard.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.GateResolved(
                            request_id=str(pending.request_id),
                            gate_name=pending.gate_name,
                            tool_name=pending.action_name,
                            resolution="external_callback",
                            message="外部回调已收到。正在恢复执行。",
                            thread_id=pending.effective_wire_thread_id(),
                        ),
                    )

                is_external_tool_callback = (
                        isinstance(pending.resume_kind, ResumeKind.External)
                        and is_external_tool_callback_id(pending.resume_kind.callback_id)
                )

                if is_external_tool_callback:
                    resolved_call_id = await resolved_or_synthetic_call_id_for_pending_action(
                        guard, pending
                    )
                    synthesized_output = extract_external_tool_output(
                        resolution.payload, resolved_call_id
                    )
                    raw_rendered = json.dumps(synthesized_output, indent=2, ensure_ascii=False)
                    sanitized = guard.effect_adapter.safety().sanitize_tool_output(
                        pending.action_name, raw_rendered
                    )
                    await guard.thread_manager.resume_thread(
                        pending.thread_id,
                        message.user_id,
                        ThreadMessage.action_result(
                            resolved_call_id,
                            pending.action_name,
                            sanitized.content,
                        ),
                        None,
                        resolved_call_id,
                    )
                elif pending.resume_output is not None:
                    resolved_call_id = await resolved_or_synthetic_call_id_for_pending_action(
                        guard, pending
                    )
                    await guard.thread_manager.resume_thread(
                        pending.thread_id,
                        message.user_id,
                        resumed_action_result_message(
                            resolved_call_id,
                            pending.action_name,
                            pending.resume_output,
                        ),
                        None,
                        resolved_call_id,
                    )
                else:
                    return await execute_pending_gate_action(
                        agent,
                        guard,
                        message,
                        pending,
                        pending.approval_already_granted,
                        None,
                    )

            return await await_thread_outcome(
                agent,
                guard,
                message,
                pending.conversation_id,
                pending.thread_id,
            )

        async def handle_interrupt(
                agent: Agent,
                message: IncomingMessage,
        ) -> BridgeOutcome:
            """处理中断提交——停止活动引擎线程。"""
            await init_engine(agent)

            lock = ENGINE_STATE.get()
            if lock is None:
                raise engine_err("init", "引擎状态未初始化")
            guard = await lock.read()
            if guard is None:
                raise engine_err("init", "引擎状态为空")

            conv_id = await guard.conversation_manager.get_or_create_conversation(
                message.channel, message.user_id
            )

            conv = await guard.conversation_manager.get_conversation(conv_id)
            active_threads = conv.active_threads if conv is not None else []

            stopped = 0
            for tid in active_threads:
                if await guard.thread_manager.is_running(tid):
                    try:
                        await guard.thread_manager.stop_thread(tid, message.user_id)
                        stopped += 1
                    except Exception as e:
                        logger.debug("engine v2: 停止线程失败, thread_id=%s, error=%s", tid, e)

            if stopped > 0:
                logger.debug("engine v2: 中断了 %d 个运行中的线程", stopped)
                return BridgeOutcome.Respond("已中断。")
            else:
                return BridgeOutcome.Respond("没有可中断的内容。")

        async def handle_new_thread(
                agent: Agent,
                message: IncomingMessage,
        ) -> BridgeOutcome:
            """处理新线程提交——清除对话以重新开始。"""
            await clear_engine_conversation(agent, message)
            return BridgeOutcome.Respond("已开始新对话。")

        async def handle_clear(
                agent: Agent,
                message: IncomingMessage,
        ) -> BridgeOutcome:
            """处理清除提交——停止线程并重置对话。"""
            await clear_engine_conversation(agent, message)
            return BridgeOutcome.Respond("对话已清除。")

        async def clear_engine_conversation(
                agent: Agent,
                message: IncomingMessage,
        ) -> None:
            """停止所有活动线程并清除对话条目。"""
            await init_engine(agent)

            lock = ENGINE_STATE.get()
            if lock is None:
                raise engine_err("init", "引擎状态未初始化")
            guard = await lock.read()
            if guard is None:
                raise engine_err("init", "引擎状态为空")

            conv_id = await guard.conversation_manager.get_or_create_conversation(
                message.channel, message.user_id
            )

            # 先停止所有活动线程
            conv = await guard.conversation_manager.get_conversation(conv_id)
            if conv is not None:
                for tid in conv.active_threads:
                    if await guard.thread_manager.is_running(tid):
                        try:
                            await guard.thread_manager.stop_thread(tid, message.user_id)
                        except Exception:
                            pass
                    # 丢弃此线程的所有待处理门控，无论用户是谁，
                    # 防止产生永远无法解析的孤立门控 (#2323)
                    await guard.pending_gates.discard_for_thread(tid)

            # 清除此用户的进行中 OAuth 流 (#3320)
            if agent.deps.extension_manager is not None:
                flows = agent.deps.extension_manager.pending_oauth_flows()
                async with flows.write() as flow_dict:
                    before = len(flow_dict)
                    flow_dict.retain(lambda state, flow: flow.user_id != message.user_id)
                    removed = before - len(flow_dict)
                    if removed > 0:
                        logger.debug(
                            "engine v2: /clear 时清除了 %d 个待处理 OAuth 流, user_id=%s",
                            removed,
                            message.user_id,
                        )

            # 清除对话条目和活动线程列表
            await guard.conversation_manager.clear_conversation(conv_id, message.user_id)

            logger.debug(
                "engine v2: 对话已清除, user_id=%s, conversation_id=%s",
                message.user_id,
                conv_id,
            )

        async def has_pending_auth(user_id: str) -> bool:
            """检查用户是否有待处理的认证门控。"""
            lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
            if lock is None:
                return False
            try:
                guard = await lock.read()
            except Exception:
                return False
            if guard is None:
                return False

            for gate in await guard.pending_gates.list_for_user(user_id):
                if isinstance(gate.resume_kind, ResumeKind.Authentication):
                    return True
            return False

        async def clear_engine_pending_auth(
                user_id: str,
                thread_id: Optional[str],
        ) -> None:
            """
        在 v2 引擎中清除用户的待处理认证状态。

        从网关端认证清理路径调用，以确保在浏览器放弃提示或
        OAuth 回调在正常聊天消息路径之外完成时清除待处理认证门控。
        """
            lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
            if lock is None:
                return
            guard = await lock.read()
            if guard is None:
                return

            if thread_id is not None:
                resolution = await resolve_pending_gate_for_user(
                    guard.pending_gates, user_id, thread_id
                )
                if isinstance(resolution, PendingGateResolution.Resolved):
                    gate = resolution.gate
                    if isinstance(gate.resume_kind, ResumeKind.Authentication):
                        await guard.pending_gates.discard(gate.key())
                return

            for gate in await guard.pending_gates.list_for_user(user_id):
                if isinstance(gate.resume_kind, ResumeKind.Authentication):
                    await guard.pending_gates.discard(gate.key())

        async def clear_engine_pending_auth_for_credential(
                user_id: str,
                credential_name: str,
        ) -> None:
            """按凭证名称清除用户的待处理认证门控。"""
            cred = CredentialName.new(credential_name)
            if cred is None:
                return

            lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
            if lock is None:
                return
            guard = await lock.read()
            if guard is None:
                return

            for gate in await guard.pending_gates.list_for_user(user_id):
                if (
                        isinstance(gate.resume_kind, ResumeKind.Authentication)
                        and gate.resume_kind.credential_name == cred
                ):
                    await guard.pending_gates.discard(gate.key())

        async def discard_engine_pending_auth_request(
                user_id: str,
                request_id: uuid.UUID,
                thread_id: Optional[str],
        ) -> bool:
            """
        丢弃匹配 request_id 和可选 thread_id 的待处理认证门控。

        返回 True 表示找到并丢弃了门控，False 表示未找到匹配的门控。
        """
            lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
            if lock is None:
                return False
            guard = await lock.read()
            if guard is None:
                return False

            hinted_uuid = parse_scope_uuid(thread_id)
            hinted_scope = thread_id

            matching_gate = None
            for gate in await guard.pending_gates.list_for_user(user_id):
                if gate.request_id != request_id:
                    continue
                if not isinstance(gate.resume_kind, ResumeKind.Authentication):
                    continue

                # 检查 thread_id 提示匹配
                if hinted_scope is None:
                    matching_gate = gate
                    break
                if gate.scope_thread_id == hinted_scope:
                    matching_gate = gate
                    break
                if hinted_uuid is not None and (
                        gate.thread_id == hinted_uuid or gate.conversation_id == hinted_uuid
                ):
                    matching_gate = gate
                    break

            if matching_gate is None:
                return False

            try:
                await guard.pending_gates.discard(matching_gate.key())
                return True
            except Exception:
                return False

        async def transition_engine_pending_auth_request_to_pairing(
                user_id: str,
                request_id: uuid.UUID,
                thread_id: Optional[str],
                extension_name: str,
        ) -> Optional[str]:
            """
        将待处理认证门控转换为配对门控。

        返回新配对门控的 request_id，如果未找到匹配的门控则返回 None。
        """
            lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
            if lock is None:
                return None
            guard = await lock.read()
            if guard is None:
                return None

            hinted_uuid = parse_scope_uuid(thread_id)
            hinted_scope = thread_id

            matching_gate = None
            for gate in await guard.pending_gates.list_for_user(user_id):
                if gate.request_id != request_id:
                    continue
                if not isinstance(gate.resume_kind, ResumeKind.Authentication):
                    continue

                # 检查 thread_id 提示匹配
                if hinted_scope is None:
                    matching_gate = gate
                    break
                if gate.scope_thread_id == hinted_scope:
                    matching_gate = gate
                    break
                if hinted_uuid is not None and (
                        gate.thread_id == hinted_uuid or gate.conversation_id == hinted_uuid
                ):
                    matching_gate = gate
                    break

            if matching_gate is None:
                return None

            await guard.pending_gates.discard(matching_gate.key())

            next_pending = pairing_pending_gate_from_auth(matching_gate, extension_name)
            await guard.pending_gates.insert(next_pending)

            return str(next_pending.request_id)

    async def handle_with_engine(
            agent: Agent,
            message: IncomingMessage,
            content: str,
    ) -> BridgeOutcome:
        """通过引擎 v2 管道处理用户消息。"""
        return await handle_with_engine_inner(agent, message, content, 0)

    # 认证重试递归的最大深度（存储凭证 → 重试原始消息）
    MAX_AUTH_RETRY_DEPTH: int = 2

    async def handle_with_engine_inner(
            agent: Agent,
            message: IncomingMessage,
            content: str,
            depth: int,
    ) -> BridgeOutcome:
        """通过引擎 v2 管道处理用户消息的内部实现。"""

        # 防止认证重试无限递归，最大深度为 2
        if depth > MAX_AUTH_RETRY_DEPTH:
            return BridgeRespondOutcome(
                "凭证已存储，但认证重试次数过多。请重新发送您的消息。"
            )

        # --------Step1: 确保引擎已初始化--------
        await init_engine(agent)

        lock = ENGINE_STATE.get()
        if lock is None:
            raise engine_err("init", "引擎状态未初始化")

        async with lock.read() as guard:
            state = guard
            if state is None:
                raise engine_err("init", "引擎状态为空")

            logger.debug(
                "engine v2: 正在处理消息, user_id=%s, channel=%s",
                message.user_id,
                message.channel,
            )

            thread_scope = message.conversation_scope
            # 解析引擎线程 ID
            scoped_thread_id = parse_engine_thread_id(thread_scope)

            # --------Step2: 检查是否有待处理的gate，并按优先级处理--------
            # --------Step2.1: 解析用户的待处理门控--------
            resolution = await resolve_pending_gate_for_user(
                state.pending_gates, message.user_id, thread_scope
            )
            # --------Step2.2: 处理gate--------
            match resolution:
                case PendingGateResolutionResolved(gate):
                    # 用户必须提供凭证（令牌、API 密钥、OAuth 流）
                    if gate.resume_kind == ResumeKind.Authentication:
                        request_id = gate.request_id
                        if content.strip() == "" or content.strip().lower() == "cancel":
                            gate_resolution = GateResolution.Cancelled
                        else:
                            gate_resolution = GateResolution.CredentialProvided(
                                token=content.strip()
                            )
                        # 释放读取锁后再调用 resolve_gate
                        guard.release()
                        return await resolve_gate(
                            agent, message, gate.thread_id, request_id, gate_resolution
                        )

                    # 用户必须批准或拒绝工具调用
                    elif gate.resume_kind == ResumeKind.Approval:
                        pending = gate.clone()
                        # 从状态中克隆 SSE arc 和工具注册表，
                        # 然后在等待 broadcast + channel I/O 之前释放引擎读取锁。
                        # 上面的认证分支执行相同操作，`notify_pending_gate` 的签名
                        # 接受 owned Option<Arc<SseManager>>，正是为了让此
                        # 终端返回分支可以释放锁。`notify_pending_gate` 需要
                        # 工具注册表句柄来解析认证门控显示名称，而无需持有
                        # 引擎状态锁。
                        sse = state.sse.clone()
                        tools = state.effect_adapter.tools()
                        auth_manager = state.auth_manager.clone()
                        extension_manager = state.extension_manager.clone()
                        guard.release()
                        return await notify_pending_gate(
                            agent,
                            sse,
                            tools,
                            auth_manager,
                            extension_manager,
                            message,
                            pending,
                        )

                case PendingGateResolutionAmbiguous():
                    return BridgeRespondOutcome(
                        text="多个待处理的批准或认证提示正在等待。请从原始线程回复。"
                    )

            # --------Step3: 孤儿线程检查--------
            # 检查线程是否在等待批准或认证但状态丢失，如果是则标记为失败
            if scoped_thread_id is not None:
                orphaned = await fail_orphaned_waiting_thread_if_needed(
                    state, message.user_id, scoped_thread_id
                )
                if orphaned:
                    return BridgeRespondOutcome(
                        text="此线程正在等待批准或认证，但该待处理状态已丢失。"
                             "线程已标记为失败；请重新发送您的请求。"
                    )

            # --------Step4: 对输入内容进行安全验证--------

            # 安全检查——在线程操作 ::process_user_input 中镜像 v1 管道，
            # 确保两条引擎路径执行相同的入站保护措施。
            # 当消息带有附件时，空的文本正文是合法的（附件即为有效载荷）；
            # 跳过验证器对空输入的拒绝，但仍对文本应用长度/策略检查。
            trimmed_content = content.strip()
            skip_empty_check = trimmed_content == "" and len(message.attachments) > 0

            # --------Step4.1: 验证content是否合法--------
            if not skip_empty_check:

                validation = agent.safety().validate_input(content)
                if not validation.is_valid:
                    details = "; ".join(
                        f"{e.field}: {e.message}" for e in validation.errors
                    )
                    return BridgeOutcome.Respond(
                        f"输入被安全验证拒绝: {details}"
                    )

            # --------Step4.2: 验证content否违反任何策略规则--------
            violations = agent.safety().check_policy(content)
            if any(rule.action == PolicyAction.Block for rule in violations):
                return BridgeOutcome.Respond(
                    "输入被安全策略拒绝。"
                )

            # --------Step4.3: 输入中是否包含泄漏的密钥--------

            # 扫描入站消息中的密钥（API 密钥、令牌）。
            # 在此处捕获它们可以防止大语言模型将其回显，
            # 否则会触发外发泄漏检测器并造成错误循环。
            warning = agent.safety().scan_inbound_for_secrets(content)
            if warning is not None:
                logger.warning(
                    "engine v2: 入站消息被阻止——包含泄漏的密钥, user_id=%s, channel=%s",
                    message.user_id,
                    message.channel,
                )
                return BridgeOutcome.Respond(warning)

            # 解析每用户项目（如果需要则创建）
            project_id = await resolve_user_project(
                state.store, message.user_id, state.default_project_id
            )

            # --------Step5: 处理消息附件--------

            persisted_attachments = list(message.attachments)
            # --------Step5.1: 保存附件内容--------
            attachment_notes = await persist_project_attachments(
                state.project_root,
                message,
                project_id,
                persisted_attachments,
            )

            # --------Step5.2: 将附件内容处理为标准化信息--------

            # 引擎 v2 线程目前仅支持文本，因此附件必须在路由到引擎之前合并到有效的用户内容中。
            # 这样可以在引擎线程和双写的网关历史记录中保留提取的文档文本、项目本地文件路径以及附件元数据。
            augmented = augment_with_attachments(content, persisted_attachments)
            effective_content = augmented.text if augmented is not None else content

            # --------Step6: 触发OnEvent 任务--------

            # 触发所有处于活动状态的 OnEvent 任务，其模式（以及可选的频道过滤器）与此入站消息匹配。
            # 此处触发的任务是消息的副作用——独立于下方生成的常规对话线程，且与之并行执行。
            # 错误会被记录，但绝不会阻塞面向用户的消息处理。
            #
            # 此路径不会触及 v1 创建的例程：它们存在于 v1 例程存储中，并由后台的 v1 RoutineEngine 触发。
            # 通过 routine_create 别名创建的任务存在于引擎存储中，并在此处触发。
            await fire_event_missions_for_message(state, message, effective_content)

            # 向通道发送"思考中..."状态
            try:
                await agent.channels.send_status(
                    message.channel,
                    StatusUpdate.Thinking("处理中..."),
                    message.metadata,
                )
            except Exception:
                pass

            # 重置每步调用计数器，以便每个线程从头开始
            state.effect_adapter.reset_call_count()

            # --------Step7: 创建对话--------

            # --------Step7.1: 限定引擎对话的范围--------
            # 按（频道、用户、线程）限定引擎对话的范围。
            # 当前端发送 thread_id 时（用户创建了新对话），将其作为频道键的一部分，
            # 以便每个 v1 线程映射到独立的引擎对话。
            # 若不这样做，所有线程将共享同一个对话，消息会出现在错误的位置。
            scope = message.conversation_scope
            channel_key = (
                f"{message.channel}:{scope}" if scope is not None else message.channel
            )

            # --------Step7.2: 创建对话--------
            # 获取或创建频道+用户对对应的对话。
            conv_id = await state.conversation_manager.get_or_create_conversation(
                channel_key, message.user_id
            )

            # 在将频道提供的时区传递给引擎之前进行验证。
            # ValidTimezone::parse 会拒绝空字符串/无效字符串；
            # 我们发送规范的 IANA 时区名称（而非原始输入），以便下游消费者看到一个已知正确的值。
            # 必须在 spawn 时*传入*——线程启动后设置元数据对于首轮对话的内存执行器是不可见的。
            validated_tz = ValidTimezone.parse(message.timezone) if message.timezone else None

            # --------Step8: 规则检测显式执行意图--------
            # 检测执行意图并相应地配置义务
            thread_config = ThreadConfig.default()
            if user_signals_execution_intent(content):
                thread_config.require_action_attempt = True

            # --------Step9: 处理首轮对话丢失调用方工具的问题--------

            # 将对话范围（可解析为 Uuid）写入线程的 `initial_metadata` 中。
            # 引擎会将其读回到 `ThreadExecutionContext.conversation_scope` 中，
            # 这使得桥接器的 `EffectBridgeAdapter` 能够通过引擎 `thread_id` 或调用方范围来解析每个对话的状态
            # （目前为调用方提供的外部工具目录）。
            # 若不这样做，在 spawn 后立即启动的执行器任务将与桥接器 spawn 后的 `transfer` 产生竞态条件，
            # 从而导致首轮对话丢失调用方工具。
            scope_uuid = parse_engine_thread_id(scope)
            extra_metadata = None
            if scope_uuid is not None:
                extra_metadata = {
                    "conversation_scope": str(scope_uuid),
                }

            # --------Step10: 为线程绑定每次执行的上下文--------

            # 在引擎生成线程之前预先绑定每次执行的上下文。
            # `handle_user_message` 在内部分配并启动引擎任务；
            # 如果快速工具门控在 `set_execution_context` 落地之前触发，
            # 则控制器的 `pause()` 将找不到对应条目并静默取消门控。
            # 预执行插槽以 user_id 为键，上游的每个对话锁确保每个对话最多只有一个桥接轮次正在执行。
            scope_thread_id = ExternalThreadId.new(scope) if scope else None
            per_exec_context = PerExecutionContext(
                conversation_id=conv_id,
                source_channel=message.channel,
                scope_thread_id=scope_thread_id,
                channel_metadata=message.metadata,
                original_message=message.content,
            )
            await state.gate_controller.set_pre_execution_context(
                message.user_id, conv_id, per_exec_context
            )

            # --------Step11: 处理用户消息--------

            # 处理消息——生成新线程或注入到活动线程中。
            # 出错时我们必须清除刚刚安装的预执行插槽：
            # 若不这样做，失败的 `handle_user_message`（在分配任何 thread_id 之前引擎生成/注入失败）
            # 会留下一个以 user_id 为键的过时条目，
            # 这将导致同一用户的下一个门控提示被错误路由。
            try:
                thread_id = await state.conversation_manager.handle_user_message(
                    conv_id,
                    effective_content,
                    project_id,
                    message.user_id,
                    thread_config,
                    validated_tz.name() if validated_tz else None,
                    extra_metadata,
                )
            except Exception as e:
                await state.gate_controller.clear_pre_execution_context(
                    message.user_id, conv_id
                )
                raise engine_err("线程错误", e)

            # 将预执行条目提升为以（用户、线程）为键的条目。
            # 此后，来自此线程的门控将首先落在线线程键的条目上；
            # 每用户回退覆盖在此提升落地之前触发的任何门控。
            await state.gate_controller.set_execution_context(
                message.user_id, thread_id, per_exec_context
            )

            # 将目录重新键控到引擎分配的 `thread_id` 上，
            # 以便 `await_thread_outcome` 中的终端状态清理钩子
            # 在规范键下找到条目。竞争窗口保护是上面的
            # conversation_scope 管道；此 transfer 是记账部分。
            if scope_uuid is not None:
                await state.external_tool_catalog.transfer(scope_uuid, thread_id)

            if attachment_notes:
                await save_attachment_index_notes(
                    state.store,
                    project_id,
                    message.user_id,
                    thread_id,
                    attachment_notes,
                )

            # 双重写入 v1 数据库，以便网关历史 API 显示消息。
            # 在可用时使用限定范围的对话，回退到默认的助手对话。
            # 外部通道范围（例如 `wecom:group:*`）不是 UUID，
            # 因此它们被映射到稳定的 UUID 对话 ID，同时在
            # `conversations.thread_id` 中保留原始范围。
            if state.db is not None:
                try:
                    cid = await resolve_v1_conversation_for_message(state.db, message)
                    await state.db.add_conversation_message(cid, "user", effective_content)
                except Exception as e:
                    logger.warning(
                        "无法为用户消息持久化解析 v1 对话, message_id=%s: %s",
                        message.id,
                        e,
                    )

            logger.debug("engine v2: 线程已生成, thread_id=%s", thread_id)
            outcome = await await_thread_outcome(
                agent, state, message, conv_id, thread_id
            )

            # 删除每执行上下文。`PendingGate` 行（如果门控触发）
            # 携带了解析器从此处开始所需的一切。
            #
            # BridgeOutcome.Pending 意味着请求处理程序在引擎仍在运行时
            # 达到了截止时间（通常停在 `BridgeGateController::pause` 中等待批准）。
            # 在此处清除上下文会使暂停的线程搁浅——其最终解析将对任何后续门控
            # 调用 `pause()`，而没有注册的上下文，表现为静默的 `Cancelled`。
            # 将清理推迟到监视线程完成并在引擎实际完成后清除的后台任务。
            if (
                    isinstance(outcome, BridgeOutcome)
                    and outcome.is_pending()
                    and await state.thread_manager.is_running(thread_id)
            ):
                spawn_deferred_context_cleanup(
                    state.gate_controller,
                    state.thread_manager,
                    message.user_id,
                    thread_id,
                    conv_id,
                )
            else:
                await state.gate_controller.clear_execution_context(
                    message.user_id, thread_id, conv_id
                )

            return outcome

    def spawn_deferred_context_cleanup(
            gate_controller: BridgeGateController,
            thread_manager: ThreadManager,
            user_id: str,
            thread_id: ThreadId,
            conv_id: ConversationId,
    ) -> None:
        """
    生成一个后台任务，等待线程完成或超时后清除执行上下文。

    轮询频率：30 秒（廉价；一旦用户解析，线程完成时间为秒到分钟级）。
    上限为一小时——远超限制任何 pause() 调用的 30 分钟 PendingGate 过期时间。
    """

        async def _cleanup():
            poll_interval = 30  # 秒
            max_wait = 60 * 60  # 秒
            elapsed = 0

            while True:
                if not await thread_manager.is_running(thread_id):
                    break
                if elapsed >= max_wait:
                    logger.warning(
                        "延迟上下文清理达到一小时上限；仍将清除上下文, thread_id=%s",
                        thread_id,
                    )
                    break
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            await gate_controller.clear_execution_context(user_id, thread_id, conv_id)
            logger.debug(
                "engine v2: 延迟上下文清理已运行, thread_id=%s",
                thread_id,
            )

        asyncio.create_task(_cleanup())

    GATEWAY_CHANNEL_NAME = "gateway"

    def spawn_post_park_continuation(
            state: EngineState,
            channels: ChannelManager,
            message: IncomingMessage,
            conv_id: ConversationId,
            thread_id: ThreadId,
    ) -> None:
        """
    生成一个后台任务，在门控暂停后接管事件转发和最终响应投递。

    当线程在内联门控处暂停时，前台返回 Pending，此后台任务负责：
    - 转发线程事件到通道和 SSE
    - 在线程完成时投递最终响应
    - 清理执行上下文
    """
        thread_manager = state.thread_manager
        conversation_manager = state.conversation_manager
        effect_adapter = state.effect_adapter
        store = state.store
        gate_controller = state.gate_controller
        pending_gates = state.pending_gates
        sse = state.sse
        db = state.db
        auth_manager = state.auth_manager
        extension_manager = state.extension_manager
        user_id = message.user_id
        channel_name = message.channel
        metadata = message.metadata
        tid_str = str(thread_id)

        async def _continuation():
            event_rx = thread_manager.subscribe_events()
            max_wait = 60 * 60  # 一小时上限
            elapsed = 0
            poll_interval = 0.5  # 500 毫秒

            # 事件转发循环
            while True:
                try:
                    # 尝试接收事件
                    try:
                        event = await asyncio.wait_for(event_rx.recv(), timeout=poll_interval)
                        if getattr(event, 'thread_id', None) == thread_id:
                            await forward_event_to_channel(event, channels, channel_name, metadata)
                            if sse is not None:
                                skip_verbose = not sse.has_verbose_receivers()
                                leak_detector = effect_adapter.safety().leak_detector()
                                for app_event in thread_event_to_app_events(event, tid_str):
                                    if skip_verbose and app_event.is_verbose_only():
                                        continue
                                    redact_code_executed_secrets(app_event, leak_detector)
                                    sse.broadcast_for_user(user_id, app_event)
                    except asyncio.TimeoutError:
                        pass

                    # 检查线程是否仍在运行
                    if not await thread_manager.is_running(thread_id):
                        break

                    elapsed += poll_interval
                    if elapsed >= max_wait:
                        logger.warning(
                            "暂停后继续任务达到一小时上限；放弃, thread_id=%s",
                            thread_id,
                        )
                        await gate_controller.clear_execution_context(user_id, thread_id, conv_id)
                        return

                except Exception:
                    break

            # 线程已完成。镜像 `await_thread_outcome` 的循环后
            # outcome → BridgeOutcome 路径，但直接通过 channel + SSE 投递响应，
            # 而不是通过桥接返回值返回（前台调用很久以前就返回了 Pending）。
            try:
                outcome = await thread_manager.join_thread(thread_id)
            except Exception as e:
                logger.debug(
                    "暂停后继续: join_thread 失败, thread_id=%s, error=%s",
                    thread_id,
                    e,
                )
                await gate_controller.clear_execution_context(user_id, thread_id, conv_id)
                return

            try:
                await conversation_manager.record_thread_outcome(conv_id, thread_id, outcome)
            except Exception as e:
                logger.debug(
                    "暂停后继续: record_thread_outcome 失败, thread_id=%s, error=%s",
                    thread_id,
                    e,
                )

            response_text: Optional[str] = None

            if isinstance(outcome, ThreadOutcome.Completed):
                if db is not None:
                    await persist_v2_tool_calls(store, db, thread_id, message)
                response_text = outcome.response

            elif isinstance(outcome, ThreadOutcome.Stopped):
                response_text = "线程已停止。"

            elif isinstance(outcome, ThreadOutcome.MaxIterations):
                response_text = "达到最大迭代次数但未完成。"

            elif isinstance(outcome, ThreadOutcome.Failed):
                sanitized = user_facing_thread_failure(outcome.error)
                sse_will_deliver_to_user = sse is not None and channel_name == GATEWAY_CHANNEL_NAME
                if sse is not None:
                    sse.broadcast_for_user(
                        user_id,
                        AppEvent.Error(
                            message=sanitized,
                            thread_id=tid_str,
                        ),
                    )
                bridge_outcome = bridge_outcome_for_failed_thread(
                    outcome.error,
                    outcome.debug_detail,
                    user_id,
                    channel_name,
                    sse_will_deliver_to_user,
                )
                if isinstance(bridge_outcome, BridgeOutcome.Respond):
                    response_text = bridge_outcome.text

            elif isinstance(outcome, ThreadOutcome.GatePaused):
                # 恢复后的引擎遇到了另一个（遗留）GatePaused 结果——
                # 通常是 Authentication 或 External。构建新的待处理门控行
                # 并显示提示；目前没有响应文本要投递。
                tool = await effect_adapter.tools().get(outcome.action_name)
                redacted_params = (
                    redact_params(outcome.parameters, tool.sensitive_params())
                    if tool
                    else outcome.parameters
                )

                pending = PendingGate(
                    request_id=uuid.uuid4(),
                    gate_name=outcome.gate_name,
                    user_id=user_id,
                    thread_id=thread_id,
                    scope_thread_id=(
                        ExternalThreadId.new(scope)
                        if (scope := message.conversation_scope())
                        else None
                    ),
                    conversation_id=conv_id,
                    source_channel=channel_name,
                    action_name=outcome.action_name,
                    call_id=outcome.call_id,
                    parameters=outcome.parameters,
                    display_parameters=redacted_params,
                    description=(
                        f"工具 '{outcome.action_name}' 需要 {outcome.resume_kind.kind_name()}"
                        f" (门控: {outcome.gate_name})"
                    ),
                    resume_kind=outcome.resume_kind,
                    created_at=datetime.now(timezone.utc),
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
                    original_message=message.content,
                    resume_output=outcome.resume_output,
                    paused_lease=outcome.paused_lease,
                    approval_already_granted=False,
                )

                try:
                    await pending_gates.insert(pending)
                except Exception as e:
                    logger.debug(
                        "暂停后继续: 存储后续待处理门控失败, gate=%s, error=%s",
                        outcome.gate_name,
                        e,
                    )
                else:
                    extension_name = await resolve_auth_gate_extension_name(
                        auth_manager,
                        extension_manager,
                        effect_adapter.tools(),
                        pending,
                    )

                    status_update = None
                    if isinstance(pending.resume_kind, ResumeKind.Approval):
                        status_update = StatusUpdate.ApprovalNeeded(
                            request_id=str(pending.request_id),
                            tool_name=pending.action_name,
                            description=pending.description,
                            parameters=(
                                pending.display_parameters
                                if pending.display_parameters is not None
                                else pending.parameters
                            ),
                            allow_always=pending.resume_kind.allow_always,
                        )
                    elif isinstance(pending.resume_kind, ResumeKind.Authentication):
                        status_update = StatusUpdate.AuthRequired(
                            extension_name=(
                                extension_name
                                if extension_name is not None
                                else ExtensionName.from_trusted(pending.action_name)
                            ),
                            instructions=pending.resume_kind.instructions,
                            auth_url=pending.resume_kind.auth_url,
                            setup_url=None,
                            request_id=str(pending.request_id),
                        )

                    if status_update is not None:
                        try:
                            await channels.send_status(channel_name, status_update, metadata)
                        except Exception:
                            pass

            # 投递最终响应
            if response_text is not None:
                if sse is not None:
                    sse.broadcast_for_user(
                        user_id,
                        AppEvent.Response(
                            content=response_text,
                            thread_id=tid_str,
                        ),
                    )
                try:
                    await channels.respond(message, OutgoingResponse.text(response_text))
                except Exception as e:
                    logger.debug(
                        "暂停后继续: 通道响应失败, channel=%s, error=%s",
                        channel_name,
                        e,
                    )
                try:
                    await channels.send_status(
                        channel_name,
                        StatusUpdate.Status("完成"),
                        metadata,
                    )
                except Exception as e:
                    logger.debug(
                        "暂停后继续: 完成状态发送失败, channel=%s, error=%s",
                        channel_name,
                        e,
                    )
                if db is not None:
                    try:
                        cid = await resolve_v1_conversation_for_message(db, message)
                        await db.add_conversation_message(cid, "assistant", response_text)
                    except Exception as e:
                        logger.warning(
                            "暂停后继续: 解析 v1 对话以持久化助手响应失败, thread_id=%s: %s",
                            thread_id,
                            e,
                        )

            await gate_controller.clear_execution_context(user_id, thread_id, conv_id)
            logger.debug(
                "engine v2: 暂停后继续任务已运行, thread_id=%s",
                thread_id,
            )

        asyncio.create_task(_continuation())

    async def fire_event_missions_for_message(
            state: EngineState,
            message: IncomingMessage,
            content: str,
    ) -> None:
        """
    触发其模式匹配入站消息的活动 OnEvent 任务。

    构建包含消息元数据的负载，任务线程可以通过 `state["trigger_payload"]` 读取。
    跳过空内容和系统通道消息。错误以 debug 级别记录——
    此处的失败绝不能阻塞面向用户的消息流。
    """
        trimmed = content.strip()
        if not trimmed:
            return

        # 递归守卫。将代理自己的出站文本作为入站事件回显的通道适配器
        # 必须设置 is_agent_broadcast（Slack/Discord 风格）；
        # 作为任务触发副作用产生的消息必须设置 triggering_mission_id
        # （跨不同任务的链式递归）。任一标志意味着：不要重新触发。
        if message.is_agent_broadcast:
            logger.debug(
                "engine v2: 跳过任务触发——消息是代理广播回显, channel=%s",
                message.channel,
            )
            return
        if message.triggering_mission_id is not None:
            logger.debug(
                "engine v2: 跳过任务触发——消息源自任务, channel=%s, upstream_mission_id=%s",
                message.channel,
                message.triggering_mission_id,
            )
            return

        mission_manager = await state.effect_adapter.mission_manager()
        if mission_manager is None:
            return

        payload = {
            "channel": message.channel,
            "user_id": message.user_id,
            "content": content,
            "metadata": message.metadata,
        }

        try:
            spawned = await mission_manager.fire_on_message_event(
                message.channel, content, message.user_id, payload
            )
            if spawned:
                logger.debug(
                    "engine v2: 从入站消息触发了 %d 个 OnEvent 任务, channel=%s, user_id=%s",
                    len(spawned),
                    message.channel,
                    message.user_id,
                )
        except Exception as error:
            logger.debug(
                "engine v2: fire_on_message_event 失败；继续正常处理, channel=%s, error=%s",
                message.channel,
                error,
            )

    async def await_thread_outcome(
            agent: Agent,
            state: EngineState,
            message: IncomingMessage,
            conv_id: ConversationId,
            thread_id: ThreadId,
    ) -> BridgeOutcome:
        """
    等待线程完成并返回桥接结果。

    处理事件转发、内联门控检测、超时和所有线程结果类型。
    """
        event_rx = state.thread_manager.subscribe_events()
        channels = agent.channels
        channel_name = message.channel
        metadata = message.metadata
        sse = state.sse
        tid_str = str(thread_id)

        # 安全超时：如果线程在 5 分钟内未完成，则跳出以避免永远挂起用户会话
        # （例如，在拒绝批准后线程无法恢复）
        deadline = asyncio.get_event_loop().time() + 300
        timed_out = False
        gate_parked = False
        pending_key = PendingGateKey(user_id=message.user_id, thread_id=thread_id)

        while True:
            try:
                event = await asyncio.wait_for(event_rx.recv(), timeout=0.5)
                if getattr(event, 'thread_id', None) == thread_id:
                    await forward_event_to_channel(event, channels, channel_name, metadata)
                    if sse is not None:
                        skip_verbose = not sse.has_verbose_receivers()
                        leak_detector = state.effect_adapter.safety().leak_detector()
                        for app_event in thread_event_to_app_events(event, tid_str):
                            if skip_verbose and app_event.is_verbose_only():
                                continue
                            # 引擎 crate 原始发出 CodeExecuted——它不依赖 `ironclaw_safety`。
                            # 在此处的桥接边界，在事件到达任何 SSE 订阅者之前，
                            # 清除 code/stdout/return_value 负载中的密钥
                            # （bearer 令牌、API 密钥等）。
                            redact_code_executed_secrets(app_event, leak_detector)
                            sse.broadcast_for_user(message.user_id, app_event)
            except asyncio.TimeoutError:
                pass
            except Exception:
                break

            # 检查线程是否仍在运行
            if not await state.thread_manager.is_running(thread_id):
                break

            # 内联门控检测：如果在线程仍在运行时已为 (user, thread) 注册了待处理门控，
            # 则引擎在 `BridgeGateController::pause` 内暂停等待用户解析。
            # 在此处持有 `handle_message` 会使每用户代理循环串行化在暂停之后——
            # 排队在 `msg_tx` 中的第二个线程的 `UserInput` 无法分派，
            # 直到用户解析此门控或下面的 5 分钟截止时间触发。
            # 移交给后台继续任务（保留事件转发 + 最终响应投递）
            # 并显示为 `Pending`，以便代理循环解除阻塞。
            if await state.pending_gates.peek(pending_key) is not None:
                gate_parked = True
                break

            if asyncio.get_event_loop().time() >= deadline:
                logger.warning(
                    "await_thread_outcome 在 5 分钟后超时——跳出以避免挂起, thread_id=%s",
                    thread_id,
                )
                timed_out = True
                break

        # 如果我们因为线程在内联门控处暂停而退出，将生命周期的其余部分
        # （事件转发 + 完成时的最终响应广播 + 每执行上下文清理）
        # 移交给后台任务并返回 `Pending`。join_thread 不能在前台任务上运行，
        # 因为它会在暂停的 future 上阻塞长达门控的 30 分钟过期时间。
        if gate_parked and await state.thread_manager.is_running(thread_id):
            spawn_post_park_continuation(
                state,
                agent.channels,
                message,
                conv_id,
                thread_id,
            )
            return BridgeOutcome.Pending

        # 如果我们达到截止时间且线程仍在运行（通常是因为它在
        # `BridgeGateController::pause` 中暂停等待用户尚未操作的批准），
        # 不要调用 `join_thread`——这会在同一暂停任务上阻塞请求处理程序
        # 长达门控的 `expires_at`（30 分钟）。显示为 `Pending`：
        # 活动的 `PendingGate` 行保持可用，用户仍可以解析它，
        # 解析器路径将把解析投递到暂停的 oneshot 中。
        if timed_out and await state.thread_manager.is_running(thread_id):
            return BridgeOutcome.Pending

        outcome = await state.thread_manager.join_thread(thread_id)

        # 在终端结果上丢弃外部工具目录条目——线程永远无法从
        # `Completed`、`Stopped`、`MaxIterations` 或 `Failed` 恢复，
        # 因此条目将永远泄漏。`GatePaused` 有意保留条目：
        # 后续恢复请求需要目录仍然知道此线程的调用者提供的工具。
        if not isinstance(outcome, ThreadOutcome.GatePaused):
            await state.external_tool_catalog.clear(thread_id)

        await state.conversation_manager.record_thread_outcome(
            conv_id, thread_id, outcome
        )

        # 为所有产生响应的结果写入 v1 数据库响应
        async def _write_v1_response(text: str) -> None:
            if state.db is not None:
                try:
                    cid = await resolve_v1_conversation_for_message(state.db, message)
                    await state.db.add_conversation_message(cid, "assistant", text)
                except Exception as e:
                    logger.warning(
                        "解析 v1 对话以持久化助手响应失败, message_id=%s: %s",
                        message.id,
                        e,
                    )

        # SSE 响应广播（web）
        if (
                state.sse is not None
                and isinstance(outcome, ThreadOutcome.Completed)
                and outcome.response is not None
        ):
            state.sse.broadcast_for_user(
                message.user_id,
                AppEvent.Response(
                    content=outcome.response,
                    thread_id=tid_str,
                ),
            )

        result: BridgeOutcome

        if isinstance(outcome, ThreadOutcome.Completed):
            logger.debug("engine v2: 已完成, thread_id=%s", thread_id)

            response = outcome.response

            # 基于文本的认证回退：检测响应中的 authentication_required 并进入认证模式。
            # 这是纵深防御安全网——飞行前认证门控应在执行前捕获大多数情况。
            if response is not None and "authentication_required" in response:
                logger.debug(
                    "基于文本的认证回退触发——飞行前门控未捕获到, thread_id=%s",
                    thread_id,
                )

                parsed_cred_name = parse_credential_name(response)

                # 防御凭证名称注入：仅当解析的名称是实际注册的凭证时，
                # 才启用回退认证门控。使用选定凭证名称构造
                # `authentication_required` 消息的工具不能强制用户提供不相关的密钥。
                # 没有凭证注册表就无法验证名称，因此门控不得触发——
                # 没有注册表的测试/嵌入夹具有意丢失回退路径，而不是获得提示注入向量。
                cred_name = None
                if parsed_cred_name is not None:
                    cred_reg = agent.tools().credential_registry()
                    if cred_reg is not None and cred_reg.has_secret(parsed_cred_name):
                        cred_name = parsed_cred_name

                if cred_name is None:
                    logger.warning(
                        "基于文本的认证回退拒绝未知或缺失的凭证名称, thread_id=%s",
                        thread_id,
                    )
                    return BridgeOutcome.Respond(response)

                # 通过 AuthManager 查找设置说明（或回退到内联查找）
                setup_hint = f"提供您的 {cred_name} 令牌"
                if state.auth_manager is not None:
                    hint = state.auth_manager.get_setup_instructions(cred_name)
                    if hint is not None:
                        setup_hint = hint

                pending = PendingGate(
                    request_id=uuid.uuid4(),
                    gate_name="authentication",
                    user_id=message.user_id,
                    thread_id=thread_id,
                    scope_thread_id=(
                        ExternalThreadId.new(scope)
                        if (scope := message.conversation_scope())
                        else None
                    ),
                    conversation_id=conv_id,
                    source_channel=message.channel,
                    action_name="authentication_fallback",
                    call_id=f"fallback-auth-{thread_id}",
                    parameters={"credential_name": cred_name},
                    display_parameters=None,
                    description=f"需要为 '{cred_name}' 进行认证。",
                    resume_kind=ResumeKind.Authentication(
                        credential_name=CredentialName.from_trusted(cred_name),
                        instructions=setup_hint,
                        auth_url=None,
                    ),
                    created_at=datetime.now(timezone.utc),
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
                    original_message=message.content,
                    resume_output=None,
                    paused_lease=None,
                    approval_already_granted=False,
                )
                pending_request_id = str(pending.request_id)
                try:
                    await state.pending_gates.insert(pending)
                except Exception as e:
                    logger.debug("存储回退认证门控失败: %s", e)

                # 通过通道显示认证提示（仅卡片，无文本）
                try:
                    await agent.channels.send_status(
                        message.channel,
                        StatusUpdate.AuthRequired(
                            extension_name=ExtensionName.from_trusted(cred_name),
                            instructions=setup_hint,
                            auth_url=None,
                            setup_url=None,
                            request_id=pending_request_id,
                        ),
                        message.metadata,
                    )
                except Exception:
                    pass

                return BridgeOutcome.Pending

            # 仅为已完成的线程持久化 tool_calls——不为 GatePaused 持久化
            # （部分工具，在恢复时会产生孤立行）
            if state.db is not None:
                await persist_v2_tool_calls(state.store, state.db, thread_id, message)

            if response is not None:
                result = BridgeOutcome.Respond(response)
            else:
                result = BridgeOutcome.NoResponse

        elif isinstance(outcome, ThreadOutcome.Stopped):
            result = BridgeOutcome.Respond("线程已停止。")

        elif isinstance(outcome, ThreadOutcome.MaxIterations):
            result = BridgeOutcome.Respond("达到最大迭代次数但未完成。")

        elif isinstance(outcome, ThreadOutcome.Failed):
            sanitized = user_facing_thread_failure(outcome.error)
            sse_will_deliver_to_user = (
                    state.sse is not None and message.channel == GATEWAY_CHANNEL_NAME
            )
            if state.sse is not None:
                state.sse.broadcast_for_user(
                    message.user_id,
                    AppEvent.Error(
                        message=sanitized,
                        thread_id=tid_str,
                    ),
                )
            result = bridge_outcome_for_failed_thread(
                outcome.error,
                outcome.debug_detail,
                message.user_id,
                message.channel,
                sse_will_deliver_to_user,
            )

        elif isinstance(outcome, ThreadOutcome.GatePaused):
            # 在存储/广播之前编辑敏感参数
            tool = await state.effect_adapter.tools().get(outcome.action_name)
            redacted_params = (
                redact_params(outcome.parameters, tool.sensitive_params())
                if tool
                else outcome.parameters
            )

            # 存储在统一的 PendingGateStore 中（按 user_id + thread_id 键控）
            pending = PendingGate(
                request_id=uuid.uuid4(),
                gate_name=outcome.gate_name,
                user_id=message.user_id,
                thread_id=thread_id,
                scope_thread_id=(
                    ExternalThreadId.new(scope)
                    if (scope := message.conversation_scope())
                    else None
                ),
                conversation_id=conv_id,
                source_channel=message.channel,
                action_name=outcome.action_name,
                call_id=outcome.call_id,
                parameters=outcome.parameters,
                display_parameters=redacted_params,
                description=(
                    f"工具 '{outcome.action_name}' 需要 {outcome.resume_kind.kind_name()}"
                    f" (门控: {outcome.gate_name})"
                ),
                resume_kind=outcome.resume_kind,
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
                original_message=message.content,
                resume_output=outcome.resume_output,
                paused_lease=outcome.paused_lease,
                approval_already_granted=False,
            )

            try:
                await state.pending_gates.insert(pending)
            except Exception as e:
                logger.debug(
                    "存储待处理门控失败（可能重复）, gate=%s, error=%s",
                    outcome.gate_name,
                    e,
                )

            # 来自 Responses API 的调用者提供的外部工具：
            # 显示为 `AppEvent::ExternalToolCall`，以便 /v1/responses 处理程序
            # 可以发出 `function_call` ResponseOutputItem 并完成回合。
            if (
                    isinstance(pending.resume_kind, ResumeKind.External)
                    and is_external_tool_callback_id(pending.resume_kind.callback_id)
            ):
                if state.sse is not None:
                    arguments = json.dumps(pending.parameters)
                    state.sse.broadcast_for_user(
                        message.user_id,
                        AppEvent.ExternalToolCall(
                            request_id=str(pending.request_id),
                            call_id=pending.call_id,
                            name=pending.action_name,
                            arguments=arguments,
                            thread_id=pending.effective_wire_thread_id(),
                        ),
                    )
                else:
                    logger.debug(
                        "外部工具门控已暂停（CodeAct 后）但没有连接广播器；调用者将不会被通知, user_id=%s, callback=%s, request_id=%s",
                        message.user_id,
                        pending.resume_kind.callback_id,
                        pending.request_id,
                    )
                return BridgeOutcome.Pending

            # 通过源通道发送批准/认证卡片
            extension_name = await resolve_auth_gate_extension_name(
                state.auth_manager,
                state.extension_manager,
                state.effect_adapter.tools(),
                pending,
            )
            await send_pending_gate_status(agent, message, pending, extension_name)
            result = BridgeOutcome.Pending

        else:
            result = BridgeOutcome.NoResponse

        # 为所有结果写入 v1 数据库响应，以便历史端点显示正确状态
        if isinstance(result, BridgeOutcome.Respond):
            await _write_v1_response(result.text)

        return result

    # ── 共享事件显示辅助函数 ────────────────────────────

    def format_action_display_name(action_name: str, params_summary: Optional[str]) -> str:
        """
    格式化操作名称，附带可选参数摘要以供显示。
    例如：`"http(https://api.github.com/...)"` 或仅 `"web_search"`。
    """
        if params_summary:
            return f"{action_name}({params_summary})"
        return action_name

    def interpret_message_event(role: str, content_preview: str) -> Optional[str]:
        """
    将 MessageAdded 事件解释为人类可读的状态消息。
    对于不需要 UI 显示的事件返回 `None`。
    """
        if role == "User" and content_preview.startswith("[stdout]"):
            return "代码已执行"
        elif role == "User" and content_preview.startswith("[code "):
            return "代码已执行（无输出）"
        elif role == "User" and (
                "Error" in content_preview or content_preview.startswith("Traceback")
        ):
            return "代码错误——正在重试..."
        elif role == "Assistant":
            return "正在执行代码..."
        else:
            return None

    # ── 引擎查询 DTO ────────────────────────────────────────

    @dataclass
    class EngineThreadInfo:
        """列表视图的轻量级线程摘要。"""
        id: str
        goal: str
        thread_type: str
        state: str
        project_id: str
        step_count: int
        total_tokens: int
        created_at: str
        updated_at: str
        title: Optional[str] = None
        parent_id: Optional[str] = None

    @dataclass
    class EngineThreadDetail:
        """包含消息和配置的线程详情。"""
        # 来自 EngineThreadInfo 的展平字段
        id: str
        goal: str
        thread_type: str
        state: str
        project_id: str
        step_count: int
        total_tokens: int
        created_at: str
        updated_at: str
        # 特有字段
        messages: List[Dict[str, Any]]
        max_iterations: int
        total_cost_usd: float
        title: Optional[str] = None
        parent_id: Optional[str] = None
        completed_at: Optional[str] = None

    @dataclass
    class EngineStepInfo:
        """线程详情视图的步骤摘要。"""
        id: str
        sequence: int
        status: str
        tier: str
        action_results_count: int
        tokens_input: int
        tokens_output: int
        started_at: Optional[str] = None
        completed_at: Optional[str] = None

    @dataclass
    class EngineProjectInfo:
        """项目摘要。"""
        id: str
        name: str
        description: str
        created_at: str
        goals: List[str] = field(default_factory=list)
        metrics: List[ProjectMetric] = field(default_factory=list)

    @dataclass
    class AttentionItem:
        """项目概览中显示的关注项。"""
        # `"gate"` 或 `"failure"`
        kind: str
        project_id: str
        project_name: str
        message: str
        thread_id: Optional[str] = None

    @dataclass
    class ProjectOverviewEntry:
        """包含计算出的健康状态和统计信息的每项目摘要。"""
        id: str
        name: str
        description: str
        # `"green"`、`"yellow"` 或 `"red"`
        health: str
        active_missions: int
        total_missions: int
        threads_today: int
        cost_today_usd: float
        failures_24h: int
        pending_gates: int
        created_at: str
        goals: List[str] = field(default_factory=list)
        last_activity: Optional[str] = None

    @dataclass
    class ProjectsOverviewResponse:
        """完整项目概览响应。"""
        attention: List[AttentionItem]
        projects: List[ProjectOverviewEntry]

    @dataclass
    class EngineMissionInfo:
        """列表视图的任务摘要。"""
        # 类型化的任务标识符，从引擎传递而来，而不是在适配器边界
        # 往返转换为 `String`。通过 `MissionId` 的派生 `Serialize`
        # 透明地序列化为 UUID 字符串，因此线形状与 newtype 之前的 DTO 保持相同。
        id: MissionId
        name: str
        goal: str
        status: str
        cadence_type: str
        # 频率的人类可读描述（例如 "每周一 09:00"、"webhook: /github"、"手动"）。
        # 比单独的 `cadence_type` 渲染更好。
        cadence_description: str
        thread_count: int
        created_at: str
        updated_at: str
        current_focus: Optional[str] = None

    @dataclass
    class EngineMissionDetail:
        """包含完整策略和预算信息的任务详情。"""
        # 来自 EngineMissionInfo 的展平字段
        id: MissionId
        name: str
        goal: str
        status: str
        cadence_type: str
        cadence_description: str
        thread_count: int
        created_at: str
        updated_at: str
        # 特有字段
        cadence: Dict[str, Any]
        approach_history: List[str]
        notify_channels: List[str]
        threads_today: int
        max_threads_per_day: int
        threads: List[EngineThreadInfo]
        current_focus: Optional[str] = None
        success_criteria: Optional[str] = None
        next_fire_at: Optional[str] = None

    # ── 引擎查询函数 ───────────────────────────────────

    def cadence_type_label(cadence: MissionCadence) -> str:
        """返回任务频率类型的标签。"""
        if isinstance(cadence, MissionCadence.Cron):
            return "cron"
        elif isinstance(cadence, MissionCadence.OnEvent):
            return "event"
        elif isinstance(cadence, MissionCadence.OnSystemEvent):
            return "system_event"
        elif isinstance(cadence, MissionCadence.Webhook):
            return "webhook"
        elif isinstance(cadence, MissionCadence.Manual):
            return "manual"
        return "unknown"

    def cadence_description(cadence: MissionCadence) -> str:
        """
    为 UI 提供任务频率的人类可读描述。

    对于 cron 表达式，识别常见模式（"每小时"、"每周一 09:00" 等），
    对于无法识别的模式回退到 `"cron: <expression>"`。
    其他频率类型包含其模式/路径，以便用户可以看到触发任务的内容。
    """
        if isinstance(cadence, MissionCadence.Cron):
            base = describe_cron(cadence.expression) or f"cron: {cadence.expression}"
            if cadence.timezone:
                return f"{base} ({cadence.timezone})"
            return base
        elif isinstance(cadence, MissionCadence.OnEvent):
            return f"on event: {cadence.event_pattern}"
        elif isinstance(cadence, MissionCadence.OnSystemEvent):
            return f"on system event: {cadence.source}/{cadence.event_type}"
        elif isinstance(cadence, MissionCadence.Webhook):
            return f"webhook: {cadence.path}"
        elif isinstance(cadence, MissionCadence.Manual):
            return "manual"
        return "unknown"

    def describe_cron(expression: str) -> Optional[str]:
        """
    将 5 字段 cron 表达式翻译为常见模式的英文描述。
    如果表达式不匹配已知形状，返回 `None`；调用者应回退到显示原始表达式。
    """
        parts = expression.split()
        # 接受标准 5 字段 cron；暂时忽略 6/7 字段变体
        if len(parts) != 5:
            return None

        minute, hour, dom, month, dow = parts[0], parts[1], parts[2], parts[3], parts[4]

        is_any = lambda s: s == "*"
        parse_num = lambda s: int(s) if s.isdigit() else None

        def day_name(n: int) -> str:
            names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
            return names[n % 7] if n % 7 < 7 else ""

        # 每分钟
        if is_any(minute) and is_any(hour) and is_any(dom) and is_any(month) and is_any(dow):
            return "every minute"

        # 每小时的第 M 分钟
        m = parse_num(minute)
        if is_any(hour) and is_any(dom) and is_any(month) and is_any(dow) and m is not None:
            if m == 0:
                return "every hour"
            return f"every hour at :{m:02d}"

        # 每天 H:M（无星期几、无月中日限制）
        if is_any(dom) and is_any(month) and is_any(dow):
            m = parse_num(minute)
            h = parse_num(hour)
            if m is not None and h is not None:
                return f"every day at {h:02d}:{m:02d}"

        # 每周特定一天 H:M
        if is_any(dom) and is_any(month):
            m = parse_num(minute)
            h = parse_num(hour)
            d = parse_num(dow)
            if m is not None and h is not None and d is not None:
                name = day_name(d)
                if name:
                    return f"every {name} at {h:02d}:{m:02d}"

        # 每月特定日期 H:M
        if is_any(month) and is_any(dow):
            m = parse_num(minute)
            h = parse_num(hour)
            d = parse_num(dom)
            if m is not None and h is not None and d is not None:
                return f"monthly on day {d} at {h:02d}:{m:02d}"

        return None

    def thread_to_info(t: Thread) -> EngineThreadInfo:
        """
    将引擎线程转换为线程信息 DTO。

    对于在 `title` 字段存在之前持久化的遗留线程，回退到从 `goal`
    派生的简短标签。没有这个，`EngineThreadInfo` 的前端消费者
    （TUI、任务详情视图）会渲染 UUID 前缀，因为 DTO 缺少 `turn_count`。
    """
        title = t.title if t.title else Thread.derive_title_from_message(t.goal)
        return EngineThreadInfo(
            id=str(t.id),
            goal=t.goal,
            title=title,
            thread_type=str(t.thread_type),
            state=str(t.state),
            project_id=str(t.project_id),
            parent_id=str(t.parent_id) if t.parent_id else None,
            step_count=t.step_count,
            total_tokens=t.total_tokens_used,
            created_at=t.created_at.isoformat(),
            updated_at=t.updated_at.isoformat(),
        )

    async def list_engine_threads(
            project_id: Optional[str],
            user_id: str,
    ) -> List[EngineThreadInfo]:
        """列出引擎线程，可选按项目过滤。"""
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return []
        guard = await lock.read()
        if guard is None:
            return []

        if project_id is not None:
            pid = ProjectId(uuid.UUID(project_id))
        else:
            pid = guard.default_project_id

        threads = await guard.store.list_threads(pid, user_id)
        return [thread_to_info(t) for t in threads]

    async def get_engine_thread(
            thread_id: str,
            user_id: str,
    ) -> Optional[EngineThreadDetail]:
        """按 ID 获取单个引擎线程。"""
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return None
        guard = await lock.read()
        if guard is None:
            return None

        tid = ThreadId(uuid.UUID(thread_id))

        thread = await guard.store.load_thread(tid)
        if thread is None:
            return None

        # 所有权检查：仅当线程属于请求用户时才返回
        if not thread.is_owned_by(user_id):
            return None

        messages = [
            {
                "role": str(m.role),
                "content": m.content,
                "timestamp": m.timestamp.isoformat(),
            }
            for m in thread.messages
        ]

        return EngineThreadDetail(
            id=str(thread.id),
            goal=thread.goal,
            title=thread.title or Thread.derive_title_from_message(thread.goal),
            thread_type=str(thread.thread_type),
            state=str(thread.state),
            project_id=str(thread.project_id),
            parent_id=str(thread.parent_id) if thread.parent_id else None,
            step_count=thread.step_count,
            total_tokens=thread.total_tokens_used,
            created_at=thread.created_at.isoformat(),
            updated_at=thread.updated_at.isoformat(),
            messages=messages,
            max_iterations=thread.config.max_iterations,
            completed_at=thread.completed_at.isoformat() if thread.completed_at else None,
            total_cost_usd=thread.total_cost_usd,
        )

    async def list_engine_thread_steps(
            thread_id: str,
            user_id: str,
    ) -> List[EngineStepInfo]:
        """列出线程的步骤。"""
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return []
        guard = await lock.read()
        if guard is None:
            return []

        tid = uuid.UUID(thread_id)

        # 在返回步骤之前验证线程所有权
        thread = await guard.store.load_thread(ThreadId(tid))
        if thread is None or not thread.is_owned_by(user_id):
            return []

        steps = await guard.store.load_steps(ThreadId(tid))
        return [
            EngineStepInfo(
                id=str(s.id),
                sequence=s.sequence,
                status=str(s.status),
                tier=str(s.tier),
                action_results_count=len(s.action_results),
                tokens_input=s.tokens_used.input_tokens,
                tokens_output=s.tokens_used.output_tokens,
                started_at=s.started_at.isoformat(),
                completed_at=s.completed_at.isoformat() if s.completed_at else None,
            )
            for s in steps
        ]

    async def list_engine_thread_events(
            thread_id: str,
            user_id: str,
    ) -> List[Dict[str, Any]]:
        """将线程事件列出为原始 JSON 值。"""
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return []
        guard = await lock.read()
        if guard is None:
            return []

        tid = uuid.UUID(thread_id)

        # 在返回事件之前验证线程所有权
        thread = await guard.store.load_thread(ThreadId(tid))
        if thread is None or not thread.is_owned_by(user_id):
            return []

        events = await guard.store.load_events(ThreadId(tid))
        import json
        result = []
        for e in events:
            try:
                result.append(json.loads(json.dumps(e, default=str)))
            except Exception:
                pass
        return result

    async def list_engine_projects(user_id: str) -> List[EngineProjectInfo]:
        """列出所有项目。"""
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return []
        guard = await lock.read()
        if guard is None:
            return []

        projects = await guard.store.list_projects(user_id)
        return [
            EngineProjectInfo(
                id=str(p.id),
                name=p.name,
                description=p.description,
                goals=p.goals,
                metrics=p.metrics,
                created_at=p.created_at.isoformat(),
            )
            for p in projects
        ]

    async def get_engine_project(
            project_id: str,
            user_id: str,
    ) -> Optional[EngineProjectInfo]:
        """按 ID 获取单个项目。"""
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return None
        guard = await lock.read()
        if guard is None:
            return None

        pid = ProjectId(uuid.UUID(project_id))
        project = await guard.store.load_project(pid)

        if project is None or not project.is_owned_by(user_id):
            return None

        return EngineProjectInfo(
            id=str(project.id),
            name=project.name,
            description=project.description,
            goals=project.goals,
            metrics=project.metrics,
            created_at=project.created_at.isoformat(),
        )

    def is_real_thread_failure(thread: Thread, h24_ago: datetime) -> bool:
        """
    检查线程是否代表真实的故障（而非引擎重启恢复）。

    过滤由 `recover_project_threads` 在引擎重启时强制失败的线程——
    它们被标记了 `engine_restart_recovery` 元数据，不是可操作的故障。
    """
        if thread.state != ThreadState.Failed:
            return False
        if thread.updated_at < h24_ago:
            return False
        recovery_flag = thread.metadata.get(ENGINE_RESTART_RECOVERY_METADATA_KEY)
        if recovery_flag is True:
            return False
        return True

    async def get_engine_projects_overview(user_id: str) -> ProjectsOverviewResponse:
        """
    项目概览——所有项目的健康状态、统计信息、关注项。

    遍历所有项目，从任务和线程计算每项目统计信息，
    并将待处理门控收集为关注项。设计用于控制室仪表板，
    用户在此检查高度自主的代理。
    """
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return ProjectsOverviewResponse(attention=[], projects=[])
        guard = await lock.read()
        if guard is None:
            return ProjectsOverviewResponse(attention=[], projects=[])

        # 克隆 Arc 以在 I/O 之前释放锁
        store = guard.store
        pending_gates = guard.pending_gates
        guard.release()

        projects = await store.list_projects(user_id)

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        h24_ago = now - timedelta(hours=24)

        # 一次性收集所有用户门控（稍后按 thread_id 键控）
        user_gates = await pending_gates.list_for_user(user_id)

        # 并发获取所有项目的线程和任务
        project_data = []
        for project in projects:
            pid = project.id
            threads = await store.list_threads(pid, user_id)
            missions = await store.list_missions_with_shared(pid, user_id)
            project_data.append((threads, missions))

        attention: List[AttentionItem] = []
        entries: List[ProjectOverviewEntry] = []

        for project, (threads, missions) in zip(projects, project_data):
            pid = project.id

            active_missions = sum(
                1 for m in missions if m.status == MissionStatus.Active
            )

            threads_today = sum(
                1 for t in threads if t.created_at >= today_start
            )

            cost_today_usd = sum(
                t.total_cost_usd for t in threads if t.created_at >= today_start
            )

            # 过滤重启恢复噪音：`recover_project_threads` 在引擎重启时
            # 强制失败非终端线程，并用 `engine_restart_recovery` 标记它们。
            # 它们不是可操作的故障，因此我们将它们从计数和关注提要中排除 (#3274)
            failures_24h = sum(
                1 for t in threads if is_real_thread_failure(t, h24_ago)
            )

            last_activity = max(
                (t.updated_at for t in threads),
                default=None,
            )
            if last_activity is not None:
                last_activity = last_activity.isoformat()

            # 计算此项目中线程的待处理门控
            project_thread_ids = {t.id for t in threads}
            project_gates = [
                g for g in user_gates if g.thread_id in project_thread_ids
            ]
            pending_gate_count = len(project_gates)

            # 为此项目构建关注项
            for gate in project_gates:
                attention.append(AttentionItem(
                    kind="gate",
                    project_id=str(pid),
                    project_name=project.name,
                    message=gate.description,
                    thread_id=str(gate.thread_id),
                ))

            for thread in threads:
                if is_real_thread_failure(thread, h24_ago):
                    attention.append(AttentionItem(
                        kind="failure",
                        project_id=str(pid),
                        project_name=project.name,
                        message=f"线程失败: {thread.goal}",
                        thread_id=str(thread.id),
                    ))

            # 健康状态：红色表示有故障或门控，黄色表示有暂停，绿色表示其他
            if failures_24h > 0 or pending_gate_count > 0:
                health = "red"
            elif any(m.status == MissionStatus.Paused for m in missions):
                health = "yellow"
            else:
                health = "green"

            entries.append(ProjectOverviewEntry(
                id=str(pid),
                name=project.name,
                description=project.description,
                goals=project.goals,
                health=health,
                active_missions=active_missions,
                total_missions=len(missions),
                threads_today=threads_today,
                cost_today_usd=cost_today_usd,
                failures_24h=failures_24h,
                pending_gates=pending_gate_count,
                last_activity=last_activity,
                created_at=project.created_at.isoformat(),
            ))

        return ProjectsOverviewResponse(attention=attention, projects=entries)

    async def list_engine_missions(
            project_id: Optional[str],
            user_id: str,
    ) -> List[EngineMissionInfo]:
        """列出任务，可选按项目过滤。"""
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return []
        guard = await lock.read()
        if guard is None:
            return []

        if project_id is not None:
            pid = ProjectId(uuid.UUID(project_id))
        else:
            pid = guard.default_project_id

        missions = await guard.store.list_missions_with_shared(pid, user_id)
        return [
            EngineMissionInfo(
                id=m.id,
                name=m.name,
                goal=m.goal,
                status=str(m.status),
                cadence_type=cadence_type_label(m.cadence),
                cadence_description=cadence_description(m.cadence),
                thread_count=len(m.thread_history),
                current_focus=m.current_focus,
                created_at=m.created_at.isoformat(),
                updated_at=m.updated_at.isoformat(),
            )
            for m in missions
        ]

    async def get_engine_mission(
            mission_id: str,
            user_id: str,
    ) -> Optional[EngineMissionDetail]:
        """按 ID 获取单个任务。"""
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return None
        guard = await lock.read()
        if guard is None:
            return None

        mid = MissionId(uuid.UUID(mission_id))
        m = await guard.store.load_mission(mid)

        if m is None:
            return None

        # 所有权检查：允许访问用户自己的任务和共享任务
        if m.user_id != user_id and not is_shared_owner(m.user_id):
            return None

        import json
        cadence_json = json.loads(json.dumps(m.cadence, default=str))

        # 为生成的线程表加载线程摘要
        threads: List[EngineThreadInfo] = []
        for tid in m.thread_history:
            try:
                thread = await guard.store.load_thread(tid)
                if thread is not None:
                    threads.append(thread_to_info(thread))
            except Exception:
                pass

        return EngineMissionDetail(
            id=m.id,
            name=m.name,
            goal=m.goal,
            status=str(m.status),
            cadence_type=cadence_type_label(m.cadence),
            cadence_description=cadence_description(m.cadence),
            thread_count=len(m.thread_history),
            current_focus=m.current_focus,
            created_at=m.created_at.isoformat(),
            updated_at=m.updated_at.isoformat(),
            cadence=cadence_json,
            approach_history=m.approach_history,
            notify_channels=m.notify_channels,
            success_criteria=m.success_criteria,
            threads_today=m.threads_today,
            max_threads_per_day=m.max_threads_per_day,
            next_fire_at=m.next_fire_at.isoformat() if m.next_fire_at else None,
            threads=threads,
        )

    async def fire_engine_mission(mission_id: str, user_id: str) -> Optional[str]:
        """手动触发任务（生成新线程）。"""
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            raise engine_err("未初始化", "引擎 v2 未运行")
        guard = await lock.read()
        if guard is None:
            raise engine_err("未初始化", "引擎 v2 未运行")

        mid = MissionId(uuid.UUID(mission_id))

        mission_manager = await guard.effect_adapter.mission_manager()
        if mission_manager is None:
            raise engine_err("任务", "任务管理器不可用")

        result = await mission_manager.fire_mission(mid, user_id, None)
        return str(result) if result is not None else None

    async def pause_engine_mission(
            mission_id: str,
            user_id: str,
            is_admin: bool,
    ) -> None:
        """
    暂停任务。

    对于共享任务，调用者必须是管理员（传递 `is_admin=True`）。
    对于用户任务，所有权由引擎强制执行。
    """
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            raise engine_err("未初始化", "引擎 v2 未运行")
        guard = await lock.read()
        if guard is None:
            raise engine_err("未初始化", "引擎 v2 未运行")

        mid = uuid.UUID(mission_id)
        mission_manager = await guard.effect_adapter.mission_manager()
        if mission_manager is None:
            raise engine_err("任务", "任务管理器不可用")

        # 共享任务需要管理员角色；传递共享所有者 id 以满足引擎检查
        effective_user_id = await resolve_mission_user_id(
            guard.store, mid, user_id, is_admin
        )
        await mission_manager.pause_mission(MissionId(mid), effective_user_id)

    async def resume_engine_mission(
            mission_id: str,
            user_id: str,
            is_admin: bool,
    ) -> None:
        """
    恢复暂停的任务。

    对于共享任务，调用者必须是管理员（传递 `is_admin=True`）。
    对于用户任务，所有权由引擎强制执行。
    """
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            raise engine_err("未初始化", "引擎 v2 未运行")
        guard = await lock.read()
        if guard is None:
            raise engine_err("未初始化", "引擎 v2 未运行")

        mid = uuid.UUID(mission_id)
        mission_manager = await guard.effect_adapter.mission_manager()
        if mission_manager is None:
            raise engine_err("任务", "任务管理器不可用")

        effective_user_id = await resolve_mission_user_id(
            guard.store, mid, user_id, is_admin
        )
        await mission_manager.resume_mission(MissionId(mid), effective_user_id)

    async def reset_engine_state() -> None:
        """
    重置全局引擎状态，以便可以初始化全新的引擎。

    由测试装备用于隔离引擎 v2 测试——每个测试获得干净的引擎状态，
    而不是继承先前测试的 `OnceLock` 单例。
    """
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is not None:
            await lock.write()
            ENGINE_STATE = None

    async def override_engine_project_root_for_test(path: Path) -> bool:
        """
    仅测试用途的 `EngineState.project_root` 覆盖。

    附件持久化通过缓存的 `bootstrap.ironclaw_base_dir()` 解析路径；
    在想要断言临时目录的测试中，此覆盖让测试在 `init_engine` 填充
    `ENGINE_STATE` 后将写入重定向到已知位置。
    如果覆盖已应用，返回 `True`。
    """
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return False
        guard = await lock.write()
        if guard is None:
            return False
        guard.project_root = path
        return True

    async def engine_retrospectives_for_test() -> List[ExecutionTrace]:
        """
    为每个当前已知的引擎线程构建回溯 `ExecutionTrace`。
    当引擎 v2 未初始化时返回空列表。

    仅测试辅助函数：基于快照的重放测试将每个跟踪折叠到
    `ReplayOutcome.engine_threads` 下的每线程条目中。
    不是任何公共 API 的一部分；在 `#[doc(hidden)]` 下暴露，
    因为集成测试位于单独的 crate 中，无法看到 `#[cfg(test)]`-only 项。

    当多个引擎 v2 重放可以并发运行时，调用者必须序列化访问——
    `ENGINE_STATE` 是进程全局单例，此函数遍历每个项目的每个线程。
    `tests/e2e_engine_v2.rs` 中的快照测试因此获取 `engine_v2_test_lock()`；
    生成引擎线程的新测试套件必须执行相同操作，或在调用前通过
    `reset_engine_state()` 清除状态。
    """
        lock = ENGINE_STATE.get() if ENGINE_STATE is not None else None
        if lock is None:
            return []
        guard = await lock.read()
        if guard is None:
            return []

        try:
            projects = await guard.store.list_all_projects()
        except Exception:
            return []

        out: List[ExecutionTrace] = []
        for project in projects:
            try:
                threads = await guard.store.list_all_threads(project.id)
            except Exception:
                continue

            for thread in threads:
                try:
                    events = await guard.store.load_events(thread.id)
                    thread.events = events
                except Exception:
                    pass
                out.append(build_trace(thread))

        return out

    async def resolve_mission_user_id(
            store: Store,
            mid: uuid.UUID,
            user_id: str,
            is_admin: bool,
    ) -> str:
        """
    解析任务管理操作的有效 user_id。

    如果任务是共享拥有的，则需要管理员角色并返回共享所有者 id，
    以便引擎所有权检查通过。否则返回调用者的 user_id。
    """
        try:
            mission = await store.load_mission(MissionId(mid))
        except Exception:
            mission = None

        if mission is not None and is_shared_owner(mission.user_id):
            if not is_admin:
                raise engine_err(
                    "禁止访问",
                    "共享任务只能由管理员管理",
                )
            return shared_owner_id()

        return user_id

    # ── 遗留迁移 ────────────────────────────────────────────

    async def migrate_legacy_user_ids(store: Store, owner_id: str) -> None:
        """
    一次性迁移：将所有者的 user_id 标记到任何使用 serde 默认值
    `"legacy"` 反序列化的引擎记录上（多租户之前的数据）。

    在引擎初始化时、用户范围查询之前运行。迁移后，记录可以通过
    所有者身份找到，并且 "legacy" 哨兵消失。
    """
        # 项目
        try:
            legacy_projects = await store.list_projects("legacy")
            for project in legacy_projects:
                project.user_id = owner_id
                project.updated_at = datetime.now(timezone.utc)
                try:
                    await store.save_project(project)
                except Exception:
                    pass
        except Exception:
            pass

        # 我们需要一个 project_id 来查询线程/任务/文档。
        # 使用现在已迁移的 owner_id 调用 list_projects，
        # 或在保存失败时回退到 "legacy"。
        all_projects = await store.list_projects(owner_id) or []

        for project in all_projects:
            pid = project.id

            # 线程
            try:
                legacy_threads = await store.list_all_threads(pid)
                for thread in legacy_threads:
                    if thread.user_id == "legacy":
                        thread.user_id = owner_id
                        thread.updated_at = datetime.now(timezone.utc)
                        try:
                            await store.save_thread(thread)
                        except Exception:
                            pass
            except Exception:
                pass

            # 任务
            try:
                legacy_missions = await store.list_all_missions(pid)
                for mission in legacy_missions:
                    if mission.user_id == "legacy":
                        # 系统学习任务保持 "system"；仅标记真正孤立的
                        mission.user_id = owner_id
                        mission.updated_at = datetime.now(timezone.utc)
                        try:
                            await store.save_mission(mission)
                        except Exception:
                            pass
            except Exception:
                pass

            # 内存文档（直接使用 list_memory_docs，因为 "legacy" 是 user_id）。
            # PR 之前的代码将所有迁移的技能标记为 __shared__，因此遗留 Skill
            # 文档必须恢复到 shared_owner_id()——将它们标记为 owner_id
            # 会使它们对 list_skills_global() 不可见，并破坏网关用户的
            # 跨项目可见性（问题 #2084）。
            try:
                legacy_docs = await store.list_memory_docs(pid, "legacy")
                for doc in legacy_docs:
                    doc.user_id = (
                        shared_owner_id()
                        if doc.doc_type == DocType.Skill
                        else owner_id
                    )
                    doc.updated_at = datetime.now(timezone.utc)
                    try:
                        await store.save_memory_doc(doc)
                    except Exception:
                        pass
            except Exception:
                pass

        # 从旧 frontmatter（在 project_id 持久化之前）反序列化的内存文档
        # 以 project_id = nil 加载。上面的每项目循环永远不会匹配它们，
        # 因为 nil 不是真实项目。将它们分配给所有者的默认项目，
        # 以便它们对项目范围的查询可见。
        if all_projects:
            default_project = all_projects[0]
            nil_pid = ProjectId(uuid.UUID(int=0))
            try:
                orphaned = await store.list_memory_docs(nil_pid, "legacy")
                for doc in orphaned:
                    doc.project_id = default_project.id
                    doc.user_id = (
                        shared_owner_id()
                        if doc.doc_type == DocType.Skill
                        else owner_id
                    )
                    doc.updated_at = datetime.now(timezone.utc)
                    try:
                        await store.save_memory_doc(doc)
                    except Exception:
                        pass
            except Exception:
                pass

        logger.debug(
            "engine v2: 所有者 %s 的遗留 user_id 迁移完成",
            owner_id,
        )

    def clamp_always_to_resume_kind(always: bool, resume_kind: ResumeKind) -> bool:
        """
    将调用者提供的 `always` 批准标志限制为待处理门控的
    `ResumeKind` 实际允许的范围。

    受保护操作的门控（编排器自我修改写入）通告
    `ResumeKind::Approval { allow_always: false }`，因此 UI 隐藏
    "始终批准"按钮。但批准 HTTP 端点仍然接受用户提供的
    `always: true`，因此没有此限制，精心构造的请求可以为
    `memory_write` 安装会话范围的自动批准，并绕过每个后续的每次调用门控。
    待处理门控自己的 `allow_always` 是权威的服务器端策略。

    非批准恢复类型（auth、外部回调）不携带 "always" 语义，
    始终限制为 `false`。
    """
        if not always:
            return False
        return (
                isinstance(resume_kind, ResumeKind.Approval)
                and resume_kind.allow_always
        )
