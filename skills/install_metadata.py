import json
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# ── 常量 ─────────────────────────────────────────────────────

INSTALL_METADATA_FILE_NAME = ".ironclaw-install.json"
MAX_INSTALL_METADATA_BYTES = 4096


# ── 已安装技能元数据来源 ─────────────────────────────────────

class InstalledSkillMetadataSource(Enum):
    """已安装技能元数据的来源"""
    InstalledUrl = "installed_url"

    def as_str(self) -> str:
        """返回来源的字符串表示"""
        return self.value


# ── 已安装技能元数据 ─────────────────────────────────────────

@dataclass
class InstalledSkillMetadata:
    """已安装技能的元数据"""
    source: Optional[InstalledSkillMetadataSource] = None
    source_url: Optional[str] = None
    source_subdir: Optional[str] = None

    @classmethod
    def installed_url(cls, source_url: Optional[str] = None) -> "InstalledSkillMetadata":
        """创建表示通过 URL 安装的元数据"""
        return cls(
            source=InstalledSkillMetadataSource.InstalledUrl,
            source_url=source_url,
            source_subdir=None,
        )

    def to_pretty_json(self) -> bytes:
        """将元数据序列化为格式化的 JSON 字节"""
        data = {}
        if self.source is not None:
            data["source"] = self.source.value
        if self.source_url is not None:
            data["source_url"] = self.source_url
        if self.source_subdir is not None:
            data["source_subdir"] = self.source_subdir
        return json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')

    @classmethod
    def from_bytes(cls, data: bytes) -> Optional["InstalledSkillMetadata"]:
        """从 JSON 字节反序列化元数据"""
        try:
            obj = json.loads(data.decode('utf-8'))
            source_str = obj.get("source")
            source = None
            if source_str == "installed_url":
                source = InstalledSkillMetadataSource.InstalledUrl
            return cls(
                source=source,
                source_url=obj.get("source_url"),
                source_subdir=obj.get("source_subdir"),
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    @staticmethod
    def sidecar_bytes_mark_installed(data: bytes) -> bool:
        """检查字节是否标记为已安装"""
        metadata = InstalledSkillMetadata.from_bytes(data)
        if metadata is None:
            return True
        if metadata.source is None or metadata.source == InstalledSkillMetadataSource.InstalledUrl:
            return True
        return False
