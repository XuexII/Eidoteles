from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


def _normalize_mime_type(mime: str) -> str:
    """
    将 MIME 类型规范化为其规范比较形式：去掉任何
    `; parameter` 后缀，修剪周围空白，并转换为小写。

    MIME 类型不区分大小写（RFC 2045 §5.1），因此这是工作区中
    每个 MIME 比较都要经过的单一规范化器——附件格式注册表、
    类型推断和音频转录都调用它，而不是在本地重新实现
    `split(';').next().trim()`（并在大小写处理上不一致）。
    """
    # 去掉参数部分（如 "; charset=utf-8"），取第一部分
    base = mime.split(';')[0].strip().lower()
    return base if base else mime.strip().lower()


class AttachmentKind(str, Enum):
    """
    传入消息携带的附件类型。

    序列化为线稳定的 snake_case 字符串（`"audio"`、`"image"`、
    `"document"`），以便可以持久化在转录附件引用和其他持久化契约中。
    """
    # 音频内容（语音消息、音频文件）
    Audio = "audio"
    # 图片内容（照片、截图）
    Image = "image"
    # 文档内容（PDF、文件）
    Document = "document"

    @classmethod
    def from_mime_type(cls, mime: str) -> "AttachmentKind":
        """从 MIME 类型字符串推断附件类型。"""
        base = _normalize_mime_type(mime)
        if base.startswith("audio/"):
            return cls.Audio
        elif base.startswith("image/"):
            return cls.Image
        else:
            return cls.Document


@dataclass
class IncomingAttachment:
    """
    传入消息上的文件或媒体附件。

    参见 [`AttachmentRef`] 了解在字节已落地到主机端存储后，
    持久化在转录上的、无字节的投影。
    """
    # 通道内唯一标识符（例如 Telegram file_id）
    id: str
    # 内容类型
    kind: AttachmentKind
    # MIME 类型（例如 "image/jpeg"、"audio/ogg"、"application/pdf"）
    mime_type: str
    # 原始文件名（如果已知）
    filename: Optional[str] = None
    # 文件大小（字节），如果已知
    size_bytes: Optional[int] = None
    # 从通道 API 下载文件的 URL
    source_url: Optional[str] = None
    # 主机端存储的不透明键（例如下载/缓存后）
    storage_key: Optional[str] = None
    # 保存在磁盘上的项目本地副本的相对路径（如果已持久化）
    local_path: Optional[str] = None
    # 提取的文本内容（例如 OCR 结果、PDF 文本、音频转录）
    extracted_text: Optional[str] = None
    # 原始文件字节（用于通道下载的小文件）
    data: bytes = b''
    # 时长（秒）（用于音频/视频）
    duration_secs: Optional[int] = None

