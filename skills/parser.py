# OpenClaw 技能格式专用 SKILL.md 解析器
#
# 解析以 `---` 分隔符划分 YAML 前置元数据、后接 Markdown 提示正文的文件

import logging
from dataclasses import dataclass
from typing import Optional, Any

import yaml

from .types import SkillManifest
from .validation import validate_skill_name, validate_skill_version

logger = logging.getLogger(__name__)


# ── 技能解析错误 ─────────────────────────────────────────────

class SkillParseError(Exception):
    """SKILL.md 解析失败的错误类型"""

    @classmethod
    def MissingFrontmatter(cls) -> "SkillParseError":
        """缺少 YAML frontmatter 分隔符（预期文件开头有 `---`）"""
        return cls("缺少 YAML frontmatter 分隔符（预期文件开头有 `---`）")

    @classmethod
    def InvalidYaml(cls, message: str) -> "SkillParseError":
        """无效的 YAML frontmatter"""
        return cls(f"无效的 YAML frontmatter: {message}")

    @classmethod
    def EmptyPrompt(cls) -> "SkillParseError":
        """提示正文为空（frontmatter 之后没有内容）"""
        return cls("提示正文为空（frontmatter 之后没有内容）")

    @classmethod
    def InvalidName(cls, name: str) -> "SkillParseError":
        """无效的技能名称：必须匹配 [a-zA-Z0-9][a-zA-Z0-9._-]{0,63}"""
        return cls(f"无效的技能名称 '{name}': 必须匹配 [a-zA-Z0-9][a-zA-Z0-9._-]{{0,63}}")

    @classmethod
    def InvalidVersion(cls, version: str) -> "SkillParseError":
        """无效的技能版本：必须匹配 [a-zA-Z0-9._\\-+~]{1,32}（字母数字/点/连字符/加号/下划线/波浪号，1-32 个字符）"""
        return cls(f"无效的技能版本 '{version}': 必须匹配 [a-zA-Z0-9._\\-+~]{{1,32}}")


# ── 已解析技能 ───────────────────────────────────────────────

@dataclass
class ParsedSkill:
    """解析 SKILL.md 文件的结果"""
    # 从 YAML frontmatter 解析的清单
    manifest: SkillManifest
    # 提示内容（frontmatter 之后的 markdown 正文）
    prompt_content: str


# ── 主解析函数 ───────────────────────────────────────────────

def parse_skill_md(content: str) -> ParsedSkill:
    """从原始内容字符串解析 SKILL.md 文件

    预期格式：
    ```text
    ---
    name: my-skill
    description: Does something
    activation:
      keywords: ["foo", "bar"]
    ---

    You are a helpful assistant that...
    ```
    """
    return parse_skill_md_impl(content, validate_name=True)


def starts_with_frontmatter_delimiter(content: str) -> bool:
    """检查内容是否以 frontmatter 分隔符开头"""
    normalized = content.replace('\r\n', '\n').replace('\r', '\n')
    # 剥离可选的 UTF-8 BOM
    stripped = normalized.lstrip('\ufeff')
    stripped = stripped.lstrip('\n\r')
    return stripped.startswith("---")


def parse_skill_md_for_install_recovery(content: str) -> ParsedSkill:
    """为安装恢复解析 SKILL.md 文件，不验证 `name` 字段

    由安装路径使用，需要通过将无效的发布名称重写为安全的内部标识符
    后再持久化到磁盘来恢复

    这是有意限制在安装恢复路径中的 crate 私有函数。
    正常的发现/加载必须继续使用 [`parse_skill_md`] 以拒绝无效名称
    """
    return parse_skill_md_impl(content, validate_name=False)


def split_skill_md_frontmatter(content: str) -> tuple:
    """将 SKILL.md 文件分割为其原始 YAML frontmatter 和提示正文，
    而不反序列化为类型化的 [`SkillManifest`]

    由安装恢复使用，以修改单个字段（`name`）同时保留类型化的
    `SkillManifest` 会丢弃的任何未知 YAML 键
    """
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    # 剥离可选的 UTF-8 BOM
    content = content.lstrip('\ufeff')

    trimmed = content.lstrip('\n\r')
    if not trimmed.startswith("---"):
        raise SkillParseError.MissingFrontmatter()

    after_first = trimmed[3:]
    newline_pos = after_first.find('\n')
    if newline_pos == -1:
        raise SkillParseError.MissingFrontmatter()

    after_first_line = after_first[newline_pos + 1:]

    yaml_end = find_closing_delimiter(after_first_line)
    if yaml_end is None:
        raise SkillParseError.MissingFrontmatter()

    yaml_str = after_first_line[:yaml_end]

    after_yaml = after_first_line[yaml_end:]
    prompt_start = after_yaml.find('\n')
    if prompt_start == -1:
        prompt_start = len(after_yaml)
    else:
        prompt_start += 1
    prompt_content = after_yaml[prompt_start:].lstrip('\n')

    return (yaml_str, prompt_content)


def parse_skill_md_impl(content: str, validate_name: bool) -> ParsedSkill:
    """SKILL.md 解析的内部实现"""
    # 在解析之前规范化行尾以处理 CRLF（调用者可能尚未预规范化）。
    # 这也使得 `find_closing_delimiter` 的字节偏移算术正确，
    # 因为它假定单字节 `\n` 分隔符
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    # 剥离可选的 UTF-8 BOM
    content = content.lstrip('\ufeff')

    # 找到第一个 `---` 分隔符（必须在第 1 行）
    trimmed = content.lstrip('\n\r')
    if not trimmed.startswith("---"):
        raise SkillParseError.MissingFrontmatter()

    # 找到第二个 `---` 分隔符
    after_first = trimmed[3:]
    # 跳过第一个 `---` 行的其余部分（包括任何尾随字符/换行）
    newline_pos = after_first.find('\n')
    if newline_pos == -1:
        raise SkillParseError.MissingFrontmatter()

    after_first_line = after_first[newline_pos + 1:]

    # 在单独的行上找到闭合的 `---`
    yaml_end = find_closing_delimiter(after_first_line)
    if yaml_end is None:
        raise SkillParseError.MissingFrontmatter()

    yaml_str = after_first_line[:yaml_end]

    # 解析 YAML frontmatter
    try:
        manifest_data = yaml.safe_load(yaml_str)
        if not isinstance(manifest_data, dict):
            raise SkillParseError.InvalidYaml("frontmatter 不是 YAML 映射")
    except yaml.YAMLError as e:
        raise SkillParseError.InvalidYaml(str(e))

    # 构建 SkillManifest 对象
    manifest = _build_manifest_from_yaml(manifest_data)

    # 检测旧的 `metadata.openclaw.requires` 形状并发出警告。
    # 新的扁平 `requires:` 字段替代了它；没有此警告，技能作者可能认为
    # 门控有效而它完全无效
    warn_on_legacy_requires(yaml_str, getattr(manifest, 'name', 'unknown'))

    # 验证技能名称
    if validate_name and not validate_skill_name(getattr(manifest, 'name', '')):
        raise SkillParseError.InvalidName(name=getattr(manifest, 'name', ''))

    # 验证技能版本。编排器将此值直接插值到 XML 属性
    # （`<skill version="...">`）中，因此我们拒绝任何可能跳出属性的字符串
    if not validate_skill_version(getattr(manifest, 'version', '')):
        raise SkillParseError.InvalidVersion(version=getattr(manifest, 'version', ''))

    # 强制执行激活标准限制
    activation = getattr(manifest, 'activation', None)
    if activation is not None and hasattr(activation, 'enforce_limits'):
        activation.enforce_limits()

    # 强制执行门控要求限制（目前只有 `requires.skills` 被限制以保持
    # 链安装器的队列有界）
    requires = getattr(manifest, 'requires', None)
    if requires is not None and hasattr(requires, 'enforce_limits'):
        requires.enforce_limits()

    # 提取提示内容（闭合 `---` 行之后的所有内容）
    after_yaml = after_first_line[yaml_end:]
    # 跳过 `---` 行本身
    prompt_start = after_yaml.find('\n')
    if prompt_start == -1:
        prompt_start = len(after_yaml)
    else:
        prompt_start += 1
    prompt_content = after_yaml[prompt_start:].lstrip('\n')

    if prompt_content.strip() == '':
        raise SkillParseError.EmptyPrompt()

    return ParsedSkill(
        manifest=manifest,
        prompt_content=prompt_content,
    )


def has_legacy_metadata_openclaw_requires(yaml_str: str) -> bool:
    """检测旧的 `metadata.openclaw.requires` SKILL.md frontmatter 形状。
    当存在旧形状时返回 True

    当反序列化为 `SkillManifest` 时，serde 会静默丢弃这些嵌套字段，
    因此没有此检查，技能作者可能认为他们的门控/依赖要求得到遵守，
    而它们完全无效
    """
    try:
        raw = yaml.safe_load(yaml_str)
        if not isinstance(raw, dict):
            return False
        metadata = raw.get("metadata")
        if not isinstance(metadata, dict):
            return False
        openclaw = metadata.get("openclaw")
        if not isinstance(openclaw, dict):
            return False
        return "requires" in openclaw
    except yaml.YAMLError:
        return False


def warn_on_legacy_requires(yaml_str: str, skill_name: str) -> None:
    """如果存在旧的 `metadata.openclaw.requires` 形状则发出警告"""
    if has_legacy_metadata_openclaw_requires(yaml_str):
        logger.warning(
            f"技能 '{skill_name}' 使用了旧的 `metadata.openclaw.requires` frontmatter 形状，"
            f"该形状被忽略。将要求移到顶层 `requires:` 块（包含 `bins`、`env`、`config`、`skills`），"
            f"以便门控和依赖声明生效。"
        )


def find_closing_delimiter(content: str) -> Optional[int]:
    """在单独的行上找到闭合 `---` 分隔符的位置。
    返回 `content` 中 `---` 行开头的字符偏移量
    """
    pos = 0
    for line in content.split('\n'):
        if line.strip() == "---":
            return pos
        pos += len(line) + 1  # +1 用于换行符
    return None


def _build_manifest_from_yaml(data: dict) -> Any:
    """从 YAML 数据构建 SkillManifest 对象

    这是一个简化的实现，实际项目中需要根据 SkillManifest 的定义来构建
    """

    class SkillActivation:
        def __init__(self, data: dict):
            self.keywords = data.get("keywords", []) if isinstance(data.get("keywords"), list) else []
            self.exclude_keywords = data.get("exclude_keywords", []) if isinstance(data.get("exclude_keywords"),
                                                                                   list) else []
            self.patterns = data.get("patterns", []) if isinstance(data.get("patterns"), list) else []
            self.tags = data.get("tags", []) if isinstance(data.get("tags"), list) else []
            self.max_context_tokens = data.get("max_context_tokens", 0) or 0

        def enforce_limits(self) -> None:
            """强制执行激活标准限制"""
            pass

    class SkillRequires:
        def __init__(self, data: dict):
            self.skills = data.get("skills", []) if isinstance(data.get("skills"), list) else []

        def enforce_limits(self) -> None:
            """强制执行门控要求限制"""
            pass

    class SkillManifest:
        def __init__(self, data: dict):
            self.name = data.get("name", "")
            self.version = data.get("version", "")
            self.description = data.get("description", "")
            self.activation = SkillActivation(
                data.get("activation", {}) if isinstance(data.get("activation"), dict) else {})
            self.requires = SkillRequires(data.get("requires", {}) if isinstance(data.get("requires"), dict) else {})

    return SkillManifest(data)
