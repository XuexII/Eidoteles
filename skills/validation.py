# 技能名称校验与内容转义处理

from enum import Enum
from pathlib import Path
from typing import Optional, List

import regex as re

from .types import SkillCredentialSpec, SkillOAuthConfig

# ── 正则表达式 ───────────────────────────────────────────────

# 技能名称：字母数字、连字符、下划线、点，以字母数字开头，1-64 字符
SKILL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

# 技能版本：字母数字、点、连字符、加号、下划线、波浪号，1-32 字符
SKILL_VERSION_PATTERN = re.compile(r"^[a-zA-Z0-9._\-+~]{1,32}$")

# 凭证名称：小写字母数字 + 下划线，1-64 字符
CREDENTIAL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")

# 用于转义技能内容中的标签
SKILL_TAG_RE = re.compile(r"(?i)</?[\s\x00]*skill")


# ── 安全相对路径错误 ─────────────────────────────────────────

class SafeRelativePathError(Enum):
    Empty = "Empty"
    Absolute = "Absolute"
    NonUtf8 = "NonUtf8"
    NonAscii = "NonAscii"
    Traversal = "Traversal"


# ── 验证函数 ─────────────────────────────────────────────────

def validate_skill_name(name: str) -> bool:
    """根据允许的模式验证技能名称"""
    return bool(SKILL_NAME_PATTERN.match(name))


def normalize_skill_identifier(value: str) -> Optional[str]:
    """尽可能将外部标识符规范化为安全的技能名称

    用于恢复路径，其中发布的标识符或显示名称需要转换为有效的磁盘/内部技能名称。
    有效名称被保留；无效标识符被小写，非字母数字连续字符被折叠为
    `-`、`_` 或 `.` 分隔符（如技能名称语法所允许）。

    非 ASCII 字符（带重音的字母、CJK、emoji）被视为分隔符并实际上被丢弃：
    例如 `"café"` 变为 `"caf"`，`"中文-skill"` 变为 `"skill"`。
    规范化为空或其他无效名称的标识符返回 `None`
    """
    trimmed = value.strip()
    if validate_skill_name(trimmed):
        return trimmed

    sanitized = []
    last_was_separator = False

    for ch in trimmed:
        if ch.isascii() and ch.isalnum():
            sanitized.append(ch.lower())
            last_was_separator = False
            continue

        if ch in ('.', '_', '-'):
            if sanitized and not last_was_separator:
                sanitized.append(ch)
                last_was_separator = True
            continue

        # 其他字符作为分隔符
        if sanitized and not last_was_separator:
            sanitized.append('-')
            last_was_separator = True

    result = ''.join(sanitized).rstrip('-_.')

    if len(result) > 64:
        result = result[:64].rstrip('-_.')

    if validate_skill_name(result):
        return result
    return None


def escape_xml_attr(s: str) -> str:
    """转义字符串以安全包含在 XML 属性中

    防止通过技能名称/版本字段进行属性注入攻击
    """
    return s.replace('&', "&amp;").replace('"', "&quot;").replace("'", "&apos;").replace('<', "&lt;").replace('>',
                                                                                                              "&gt;")


def escape_skill_content(content: str) -> str:
    """转义提示内容以防止标签从 `<skill>` 分隔符中突破

    使用不区分大小写的正则表达式捕获混合大小写、可选空白和空字节，
    中和开标签 (`<skill`) 和闭标签 (`</skill`)。
    开标签被转义以防止注入具有提升信任属性的虚假技能块。
    `<` 被替换为 `&lt;`
    """

    def replace_tag(match):
        matched = match.group(0)
        return "&lt;" + matched[1:]

    return SKILL_TAG_RE.sub(replace_tag, content)


def validate_skill_version(version: str) -> bool:
    """验证技能版本字符串。参见 [`SKILL_VERSION_PATTERN`]"""
    return bool(SKILL_VERSION_PATTERN.match(version))


def normalize_safe_relative_path(path: Path) -> Path:
    """规范化安全的相对路径，拒绝遍历和绝对路径"""
    path_str = str(path)
    if not path_str or path.is_absolute():
        raise SafeRelativePathError.Empty

    normalized = Path()
    for part in path.parts:
        if part == '..':
            raise SafeRelativePathError.Traversal
        if part == '.':
            continue
        if not part:
            raise SafeRelativePathError.Empty
        # 检查 ASCII 可打印？
        if not all(ord(c) < 128 for c in part):
            raise SafeRelativePathError.NonAscii
        normalized = normalized / part

    if str(normalized) == '.' or str(normalized) == '':
        raise SafeRelativePathError.Empty
    return normalized


def validate_credential_name(name: str) -> bool:
    """验证凭证名称：小写字母数字和下划线，1-64 字符"""
    return bool(CREDENTIAL_NAME_PATTERN.match(name))


def is_https_url(url: str) -> bool:
    """验证 URL 是否为 HTTPS"""
    return url.startswith("https://")


def validate_credential_spec(spec: SkillCredentialSpec) -> List[str]:
    """验证技能 frontmatter 中的单个凭证规范

    返回验证错误列表（空 = 有效）
    """
    errors = []

    if not validate_credential_name(spec.name):
        errors.append(
            f"凭证名称 '{spec.name}' 必须为小写字母数字/下划线，1-64 字符"
        )

    if not spec.provider:
        errors.append("凭证提供者不能为空")

    if not spec.hosts:
        errors.append(
            f"凭证 '{spec.name}' 必须声明至少一个主机模式"
        )

    for host in spec.hosts:
        if not host:
            errors.append(
                f"凭证 '{spec.name}' 有一个空的主机模式"
            )

    for pattern in spec.path_patterns:
        errors.extend(validate_path_pattern(spec.name, pattern))

    if spec.oauth is not None:
        errors.extend(validate_oauth_config(spec.name, spec.oauth))

    return errors


def validate_path_pattern(credential_name: str, pattern: str) -> List[str]:
    """验证凭证规范中的单个路径模式

    捕获在运行时静默永不匹配的常见错误：
    缺少前导 `/`、空字符串、字面 `..` 段以及 `?`/`#` 字符
    （匹配针对 `Url::path()` 运行，后者已剥离查询字符串和片段）。
    公开此函数以便 WASM 能力加载器 (`CredentialMappingSchema`) 可以重用相同的规则
    """
    errors = []
    if not pattern:
        errors.append(
            f"凭证 '{credential_name}' 具有空路径模式 — 省略 `path_patterns` 以匹配所有路径"
        )
        return errors
    if not pattern.startswith('/'):
        errors.append(
            f"凭证 '{credential_name}' 路径模式 '{pattern}' 必须以 '/' 开头"
        )
    if any(seg == '..' for seg in pattern.split('/')):
        errors.append(
            f"凭证 '{credential_name}' 路径模式 '{pattern}' 不得包含 '..' 段"
        )
    if '?' in pattern or '#' in pattern:
        errors.append(
            f"凭证 '{credential_name}' 路径模式 '{pattern}' 不得包含 '?' 或 '#' — 匹配仅针对 URL 路径运行（查询字符串和片段已被剥离）"
        )
    return errors


def validate_oauth_config(credential_name: str, oauth: SkillOAuthConfig) -> List[str]:
    """验证凭证规范中的 OAuth 配置"""
    errors = []

    if not is_https_url(oauth.authorization_url):
        errors.append(
            f"凭证 '{credential_name}' OAuth authorization_url 必须为 HTTPS"
        )

    if not is_https_url(oauth.token_url):
        errors.append(
            f"凭证 '{credential_name}' OAuth token_url 必须为 HTTPS"
        )

    if oauth.test_url and not is_https_url(oauth.test_url):
        errors.append(
            f"凭证 '{credential_name}' OAuth test_url 必须为 HTTPS"
        )

    return errors


def normalize_line_endings(content: str) -> str:
    """在哈希之前将行尾规范化为 LF，以确保跨平台一致性"""
    return content.replace("\r\n", "\n").replace('\r', '\n')
