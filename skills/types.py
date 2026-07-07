#! 核心技能类型。
# !
# ! 包含技能清单、激活条件、信任级别和已加载技能的数据结构。
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Dict

# ---------- 常量 ----------

# 每个技能允许的最大关键词数量，防止评分操纵。
MAX_KEYWORDS_PER_SKILL = 20

# 每个技能允许的最大正则表达式模式数量。
MAX_PATTERNS_PER_SKILL = 5

# 每个技能允许的最大标签数量，防止评分操纵。
MAX_TAGS_PER_SKILL = 10

# setup_marker 路径的最大长度（字节）。
# 防止不受信任的技能注入过长的标记字符串。
MAX_SETUP_MARKER_LENGTH = 256

# 镜像宿主 crate 的 skill_install 工具中的 MAX_CHAIN_DEPS，
# 以使链式安装程序的队列大小不受恶意清单的影响。
MAX_REQUIRED_SKILLS_PER_MANIFEST = 10

# 关键词和标签的最小长度。像 "a" 或 "is" 这样的短标记匹配范围太广，
# 可能被用于操纵评分系统。
MIN_KEYWORD_TAG_LENGTH = 3

# SKILL.md 的最大文件大小（64 KiB）。
MAX_PROMPT_FILE_SIZE = 64 * 1024

# 编译正则表达式的最大大小（64 KiB），防止 ReDoS。
MAX_REGEX_SIZE = 1 << 16


# ---------- 枚举定义 ----------

class SkillTrust(str, Enum):
    """
    技能的信任状态，决定其权限上限。

    安全性：变体顺序很重要。Ord 是从判别值派生的，
    安全模型依赖于 Installed < Trusted。
    请勿重新排序变体或更改判别值，
    除非审查了衰减代码中所有使用 min() / 比较的调用点。
    """
    # 注册表/外部技能。仅限只读工具。
    Installed = "installed"

    # 用户放置的技能（本地或工作空间）。完全信任，所有工具可用。
    Trusted = "trusted"

@dataclass
class SkillSourcedFromWorkspace:
    """
    工作区技能目录（`<workspace>/skills/`）——可信来源
    """
    path: Path

@dataclass
class SkillSourcedFromUser:
    """
    用户技能目录（`~/.ironclaw/skills/`）——可信来源
    """
    path: Path

@dataclass
class SkillSourcedFromInstalled:
    """
    已安装技能目录（`~/.ironclaw/installed_skills/`）——已安装来源
    """
    path: Path

@dataclass
class SkillSourcedFromBundled:
    """
    编译进二进制文件的内置捆绑技能——可信来源
    """
    path: Path

SkillSource = SkillSourcedFromWorkspace | SkillSourcedFromUser | SkillSourcedFromInstalled | SkillSourcedFromBundled


# ---------- 数据类定义 ----------

@dataclass
class ActivationCriteria:
    """
    从 SKILL.md frontmatter 的 activation 部分解析的激活条件。

    Attributes:
        keywords: 触发此技能的关键词（精确匹配和子串匹配）。
                  加载时限制为 MAX_KEYWORDS_PER_SKILL 个。
        exclude_keywords: 否决此技能的关键词 —— 如果匹配到任一关键词，
                          则无论关键词/模式匹配结果如何，评分为 0。防止跨技能干扰。
        patterns: 用于更复杂匹配的正则表达式模式。
                  加载时限制为 MAX_PATTERNS_PER_SKILL 个。
        tags: 用于广泛类别匹配的标签。
        max_context_tokens: 此技能的提示最多可消耗的上下文令牌数。
        setup_marker: 工作空间路径，当存在时标记此技能的设置已完成。
                      选择器会在工作空间已包含此路径时将技能排除在候选之外。
    """
    # 触发此技能的关键词（精确匹配和子串匹配）。
    # 加载时限制为 MAX_KEYWORDS_PER_SKILL 个。
    keywords: List[str] = field(default_factory=list)
    # 否决此技能的关键词 —— 如果匹配到任一关键词，
    # 则无论关键词/模式匹配结果如何，评分为 0。防止跨技能干扰。
    exclude_keywords: List[str] = field(default_factory=list)
    # 用于更复杂匹配的正则表达式模式。
    # 加载时限制为 MAX_PATTERNS_PER_SKILL 个。
    patterns: List[str] = field(default_factory=list)
    # 用于广泛类别匹配的标签。
    tags: List[str] = field(default_factory=list)
    # 此技能的提示最多可消耗的上下文令牌数。
    max_context_tokens: int = 2000
    # 工作区路径，当存在此路径时，表示该技能的设置已完成。
    # 如果工作区中已包含此路径，选择器会将该技能从候选列表中排除。
    #
    # 用于**一次性设置技能**（即 `*-setup` 角色包），
    # 使其在引导期间激活一次，在设置步骤中写入标记文件，
    # 之后便不再占用激活预算。响应式运营技能（承诺分类、
    # 决策捕获等）会保留此字段为空，并继续在每个匹配的消息上激活。
    #
    # 如需重新触发设置，请从工作区中删除标记文件。
    # 典型的标记路径是设置技能自身在首次运行时创建的路径
    # （例如开发人员设置的 `commitments/.developer-setup-complete`）。
    setup_marker: Optional[str] = None

    def enforce_limits(self) -> None:
        """
        对关键词、模式和标签强制实施限制，防止评分操纵。

        过滤掉匹配范围过广的短关键词/标签（< 3 字符），
        然后将各字段截断到上限。
        """
        # 过滤并截断关键词
        self.keywords = [k for k in self.keywords if len(k) >= MIN_KEYWORD_TAG_LENGTH]
        self.keywords = self.keywords[:MAX_KEYWORDS_PER_SKILL]

        # 过滤并截断排除关键词
        self.exclude_keywords = [k for k in self.exclude_keywords if len(k) >= MIN_KEYWORD_TAG_LENGTH]
        self.exclude_keywords = self.exclude_keywords[:MAX_KEYWORDS_PER_SKILL]

        # 截断模式
        self.patterns = self.patterns[:MAX_PATTERNS_PER_SKILL]

        # 过滤并截断标签
        self.tags = [t for t in self.tags if len(t) >= MIN_KEYWORD_TAG_LENGTH]
        self.tags = self.tags[:MAX_TAGS_PER_SKILL]

        # 清理 setup_marker：拒绝路径遍历并强制实施长度限制
        if self.setup_marker is not None:
            if len(self.setup_marker) > MAX_SETUP_MARKER_LENGTH or ".." in self.setup_marker:
                self.setup_marker = None


@dataclass
class GatingRequirements:
    """
    技能加载必须满足的要求
    """
    # 必须在 PATH 上的必需二进制文件。
    bins: List[str] = field(default_factory=list)
    # 必须设置的必需环境变量。
    env: List[str] = field(default_factory=list)
    # 必须存在的必需配置文件路径。
    config: List[str] = field(default_factory=list)
    # 应与此技能一起安装的配套技能。
    skills: List[str] = field(default_factory=list)

    def enforce_limits(self) -> None:
        """
        对 requires.skills 强制实施按清单的限制。

        从解析器调用，以防止具有数百个配套技能声明的恶意或错误清单
        在下游 MAX_CHAIN_DEPS 上限生效之前导致链式安装程序无界队列增长。

        对应 Rust:
        pub fn enforce_limits(&mut self) { self.skills.truncate(MAX_REQUIRED_SKILLS_PER_MANIFEST); }
        """
        self.skills = self.skills[:MAX_REQUIRED_SKILLS_PER_MANIFEST]


@dataclass
class SkillManifest:
    """
    从 SKILL.md YAML frontmatter 解析的技能清单。
    """
    # 技能名称（经过 SKILL_NAME_PATTERN 模式验证）。
    name: str = ""
    # 版本
    version: str = "0.0.0"
    # 技能的简短描述。
    description: str = ""
    # 激活标准。
    activation: ActivationCriteria = field(default_factory=ActivationCriteria)
    # API 访问的凭证要求。
    # 在加载时解析；凭证值永远不会出现在大语言模型上下文中。
    credentials: List[SkillCredentialSpec] = field(default_factory=list)
    # 门控要求（二进制文件、环境变量、配置文件、配套技能）。
    requires: GatingRequirements = field(default_factory=GatingRequirements)


@dataclass
class Bearer:
    """授权：Bearer {secret}"""
    pass


@dataclass
class BasicAuth:
    """授权：Basic base64(用户名:密钥)"""
    username: str


@dataclass
class Header:
    """自定义头部，可带有可选前缀（例如 `X-API-Key: Token {secret}`）"""
    name: str
    prefix: Optional[str] = None


@dataclass
class QueryParam:
    """查询参数（例如 `?api_key={secret}`）"""
    name: str


# 凭据在 HTTP 请求中的注入位置。
#
# 与 src/secrets/ironclaw_types.rs 中的 CredentialLocation 一一对应，
# 但定义在此处以便 ironclaw_skills 保持独立于主 crate。
# 转换在注册时在 src/skills/mod.rs 中进行。
SkillCredentialLocation = Bearer | BasicAuth | Header | QueryParam


@dataclass
class Standard:
    """标准 OAuth2 `refresh_token` 授权模式。"""
    pass


@dataclass
class ReauthorizeOnly:
    """提供商不支持刷新——过期后需重新授权。"""
    pass


@dataclass
class Custom:
    """提供商特定的刷新端点或额外参数。"""
    refresh_url: str
    extra_params: Dict[str, str] = field(default_factory=dict)


# 提供商处理令牌刷新的方式。
ProviderRefreshStrategy = Standard | ReauthorizeOnly | Custom


@dataclass
class SkillOAuthConfig:
    """
    技能声明的凭据的 OAuth 配置。
    """
    authorization_url: str = ""
    token_url: str = ""
    client_id: Optional[str] = None
    client_id_env: Optional[str] = None
    client_secret: Optional[str] = None
    client_secret_env: Optional[str] = None
    scopes: List[str] = field(default_factory=list)
    use_pkce: bool = False
    extra_params: Dict[str, str] = field(default_factory=dict)
    refresh: ProviderRefreshStrategy = field(default_factory=Standard)
    test_url: Optional[str] = None


@dataclass
class SkillCredentialSpec:
    """
    技能声明的凭据需求。

    技能在 YAML frontmatter 中声明凭据，以便系统可以注册宿主机→凭据映射
    并管理 OAuth 流程，而无需 WASM 模块。凭据*值*永远不会出现在 LLM 的上下文中 ——
    只有这些元数据规范在技能加载时被解析。
    """
    # `SecretsStore` 中的密钥名称（例如 `google_oauth_token`）。
    name: str = ""
    # 提供商提示（例如 `google`、`github`、`slack`）。
    provider: str = ""
    # 在 HTTP 请求中注入凭证的位置。
    location: Optional[SkillCredentialLocation] = None
    # 此凭证适用的主机模式（glob 语法，例如 `*.googleapis.com`）。
    hosts: List[str] = field(default_factory=list)
    # 将此凭证限定于特定端点的字面路径前缀。
    path_patterns: List[str] = field(default_factory=list)
    # 用于自动化令牌交换和刷新的可选 OAuth 配置。
    oauth: Optional[SkillOAuthConfig] = None
    # 凭证缺失时显示的人类可读设置说明。
    setup_instructions: Optional[str] = None


@dataclass
class LoadedSkill:
    """
    准备激活的完全加载的技能

    Attributes:
        manifest: 从 YAML frontmatter 解析的清单。
        prompt_content: 原始提示内容（frontmatter 之后的 markdown 正文）。
        trust: 信任状态（由来源位置决定）。
        source: 此技能从何处加载。
        content_hash: 提示内容的 SHA-256 哈希（加载时计算）。
        compiled_patterns: 从激活条件预编译的正则表达式模式（加载时编译）。
        lowercased_keywords: 用于评分的预计算小写关键词（避免每次消息都分配内存）。
        lowercased_exclude_keywords: 用于否决评分的预计算小写排除关键词。
        lowercased_tags: 用于评分的预计算小写标签（避免每次消息都分配内存）。
    """
    # 从 YAML frontmatter 解析的清单。
    manifest: SkillManifest = field(default_factory=SkillManifest)
    # 原始提示内容（frontmatter 之后的 markdown 正文）。
    prompt_content: str = ""
    # 信任状态（由来源位置决定）。
    trust: SkillTrust = SkillTrust.Installed
    # 此技能从何处加载。
    source: Optional[SkillSource] = None
    # 提示内容的 SHA-256 哈希（加载时计算）。
    content_hash: str = ""
    # 从激活条件预编译的正则表达式模式（加载时编译）。
    compiled_patterns: List[re.Pattern] = field(default_factory=list)
    # 用于评分的预计算小写关键词（避免每次消息都分配内存）。
    lowercased_keywords: List[str] = field(default_factory=list)
    # 用于否决评分的预计算小写排除关键词。
    lowercased_exclude_keywords: List[str] = field(default_factory=list)
    # 用于评分的预计算小写标签（避免每次消息都分配内存）。
    lowercased_tags: List[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """
        获取技能名称
        """
        return self.manifest.name

    @property
    def version(self) -> str:
        """
        获取技能版本
        """
        return self.manifest.version

    @staticmethod
    def compile_patterns(patterns: List[str]) -> List[re.Pattern]:
        """
        从激活条件编译正则表达式模式。无效或过大的模式会被记录并跳过。
        对编译后的正则表达式状态施加 64 KiB 的大小限制，防止通过病态模式进行 ReDoS。

        参数:
            patterns: 要编译的正则表达式模式字符串列表。

        返回:
            成功编译的正则表达式模式列表。
        """
        import logging
        logger = logging.getLogger(__name__)

        compiled = []
        for p in patterns:
            try:
                # Python 中无法直接限制正则表达式编译大小，
                # 但可以限制模式字符串的长度作为近似保护
                if len(p) > MAX_REGEX_SIZE:
                    logger.warning("激活正则表达式模式过长，跳过: '%s'", p)
                    continue
                regex = re.compile(p)
                compiled.append(regex)
            except re.error as e:
                logger.warning("无效的激活正则表达式模式 '%s': %s", p, e)
        return compiled
