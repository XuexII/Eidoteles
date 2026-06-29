# 使用结构化的附件上下文来增强用户消息内容。

import base64
from dataclasses import dataclass
from typing import List, Optional

from channels import AttachmentKind, IncomingAttachment
from llm import ContentPart, ImageUrl


@dataclass
class AugmentResult:
    """为 LLM 管道处理附件的结果。"""
    # 附加了附件元数据的增强文本内容
    text: str
    # 作为多模态输入包含的图像内容部分
    image_parts: List[ContentPart]


def augment_with_attachments(
        content: str,
        attachments: List[IncomingAttachment],
) -> Optional[AugmentResult]:
    """
    将附件处理为增强文本和多模态图像部分。

    如果 `attachments` 为空，返回 `None`（调用者应使用原始内容）。
    返回 `Some(AugmentResult)` 包含：
    - `text`：原始内容 + `<attachments>` 块（元数据、转录等）
    - `image_parts`：带有数据的图像的 `ContentPart.ImageUrl` 条目
    """
    if not attachments:
        return None

    text = content
    text += "\n\n<attachments>"

    image_parts: List[ContentPart] = []

    for i, att in enumerate(attachments):
        text += "\n"
        text += _format_attachment(i + 1, att)

        # 当图像数据可用时，构建多模态图像部分
        if att.kind == AttachmentKind.Image and att.data:
            b64 = base64.b64encode(att.data).decode('ascii')
            data_url = f"data:{att.mime_type};base64,{b64}"
            image_parts.append(ContentPart.ImageUrl(
                image_url=ImageUrl(
                    url=data_url,
                    detail="auto",
                )
            ))

    text += "\n</attachments>"
    return AugmentResult(text=text, image_parts=image_parts)


def _escape_xml_attr(s: str) -> str:
    """转义字符串以用作 XML 属性值。"""
    return s.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')


def _escape_xml_text(s: str) -> str:
    """转义字符串以用作 XML 文本内容。"""
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _format_attachment(index: int, att: IncomingAttachment) -> str:
    """格式化单个附件的 XML 表示。"""
    filename = _escape_xml_attr(att.filename if att.filename else "unknown")
    mime = _escape_xml_attr(att.mime_type)
    project_path_attr = (
        f' project_path="{_escape_xml_attr(att.local_path)}"'
        if att.local_path
        else ""
    )

    if att.kind == AttachmentKind.Audio:
        duration_attr = (
            f' duration="{att.duration_secs}s"'
            if att.duration_secs is not None
            else ""
        )
        size_attr = (
            f' size="{_format_size(att.size_bytes)}"'
            if att.size_bytes is not None
            else ""
        )

        body_content = (
            f"Transcript: {_escape_xml_text(att.extracted_text)}"
            if att.extracted_text
            else "Audio transcript unavailable."
        )
        body = _format_attachment_body(att.local_path, body_content)

        return (
            f'<attachment index="{index}" type="audio" filename="{filename}"'
            f' mime="{mime}"{project_path_attr}{duration_attr}{size_attr}>\n'
            f'{body}\n'
            f'</attachment>'
        )

    elif att.kind == AttachmentKind.Image:
        size_attr = (
            f' size="{_format_size(att.size_bytes)}"'
            if att.size_bytes is not None
            else ""
        )

        # 根据图像字节是否到达模型，为代理选择正确的提示。
        # 引擎 v2 将文件持久化到磁盘，但保留 `data` 填充，以便
        # `augment_with_attachments` 可以发出多模态 `image_parts` 条目——
        # 这是实际将图像发送到 LLM 的路径。设置了 `local_path` 的空 `data`
        # 仅在下游调用者清除了缓冲区（或通道省略了它）时才会发生；
        # 在这种情况下，模型看不到像素，必须通过项目文件路径访问。
        if att.data:
            body_text = (
                "[Image attached — you can already see this image directly in the conversation. "
                "Do NOT use image_analyze or try to find this file on disk — "
                "it exists only in memory. Analyze it using your vision capabilities.]"
            )
        elif att.local_path:
            body_text = (
                "[Image attached — the raw bytes are not in this turn's multimodal context, "
                "but the file has been persisted at the project file path above. "
                "Reference that path when you need the image.]"
            )
        else:
            body_text = "[Image attached — visual content not available in this conversation.]"

        body = _format_attachment_body(att.local_path, body_text)

        return (
            f'<attachment index="{index}" type="image" filename="{filename}"'
            f' mime="{mime}"{project_path_attr}{size_attr}>\n'
            f'{body}\n'
            f'</attachment>'
        )

    elif att.kind == AttachmentKind.Document:
        if att.extracted_text:
            body = _format_attachment_body(
                att.local_path,
                _escape_xml_text(att.extracted_text),
            )
        else:
            size_info = (
                f' size="{_format_size(att.size_bytes)}"'
                if att.size_bytes is not None
                else ""
            )
            body = _format_attachment_body(
                att.local_path,
                "[Document attached — text extraction unavailable]",
            )
            return (
                f'<attachment index="{index}" type="document" filename="{filename}"'
                f' mime="{mime}"{project_path_attr}{size_info}>\n'
                f'{body}\n'
                f'</attachment>'
            )

        size_attr = (
            f' size="{_format_size(att.size_bytes)}"'
            if att.size_bytes is not None
            else ""
        )

        return (
            f'<attachment index="{index}" type="document" filename="{filename}"'
            f' mime="{mime}"{project_path_attr}{size_attr}>\n'
            f'{body}\n'
            f'</attachment>'
        )

    return ""


def _format_attachment_body(local_path: Optional[str], content: str) -> str:
    """格式化附件主体内容，如果存在本地路径则包含它。"""
    if local_path:
        return f"Saved to project file: {_escape_xml_text(local_path)}\n{content}"
    return content


def _format_size(bytes_val: int) -> str:
    """将字节数格式化为人类可读的大小字符串。"""
    if bytes_val < 1024:
        return f"{bytes_val}B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val // 1024}KB"
    else:
        return f"{bytes_val / (1024.0 * 1024.0):.1f}MB"
