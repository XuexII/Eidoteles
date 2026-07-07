# 用于检索、加载和管理可用技能的技能注册表
#
# 技能可从多个来源检索：
# 1. 工作区技能目录（`<workspace>/skills/`）——可信来源
# 2. 用户技能目录（`~/.ironclaw/skills/`）——可信来源
# 3. 已安装技能目录（`~/.ironclaw/installed_skills/`）——已安装来源
# 4. 编译进二进制文件的内置捆绑技能——可信来源
#
# 同时支持平铺目录结构（`skills/SKILL.md`）与子目录结构（`skills/<name>/SKILL.md`）。
# 不含`SKILL.md`的子目录视为捆绑目录并递归扫描（最大扫描深度为`SKILLS_MAX_SCAN_DEPTH`，默认值3）。
# 若技能名称冲突，优先级靠前的来源生效（工作区覆盖用户目录，用户目录覆盖已安装目录，已安装目录覆盖内置捆绑技能）。
# 全程采用异步IO，避免阻塞Tokio运行时。

from __future__ import annotations
import asyncio
import json
import os
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Optional, List, Dict, Set, Tuple, Any
from enum import Enum
import logging
from skills.parser import ParsedSkill

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────

# 所有来源可检索到的技能总数上限
# 适用于工作区、用户及已安装目录
# 防止包含大量文件的目录造成资源耗尽
MAX_DISCOVERED_SKILLS = 100

# 捆绑目录扫描的默认递归深度
DEFAULT_MAX_SCAN_DEPTH = 3

# 技能文件的最大大小
MAX_PROMPT_FILE_SIZE = 500 * 1024  # 500 KB

# 安装元数据文件名
INSTALL_METADATA_FILE_NAME = ".install_metadata.json"


# ── 技能信任级别 ─────────────────────────────────────────────

class SkillTrust(Enum):
    Trusted = "Trusted"
    Installed = "Installed"


# ── 技能来源 ─────────────────────────────────────────────────

@dataclass
class SkillSource:
    """技能来源"""
    source_type: str
    path: Path

    @classmethod
    def Workspace(cls, path: Path) -> "SkillSource":
        return cls(source_type="Workspace", path=path)

    @classmethod
    def User(cls, path: Path) -> "SkillSource":
        return cls(source_type="User", path=path)

    @classmethod
    def Installed(cls, path: Path) -> "SkillSource":
        return cls(source_type="Installed", path=path)

    @classmethod
    def Bundled(cls, path: Path) -> "SkillSource":
        return cls(source_type="Bundled", path=path)


# ── 技能注册表错误 ───────────────────────────────────────────

class SkillRegistryError(Exception):
    """技能注册表操作的错误类型"""

    @classmethod
    def NotFound(cls, name: str) -> "SkillRegistryError":
        return cls(f"未找到技能: {name}")

    @classmethod
    def ReadError(cls, path: str, reason: str) -> "SkillRegistryError":
        return cls(f"读取技能文件失败 {path}: {reason}")

    @classmethod
    def ParseError(cls, name: str, reason: str) -> "SkillRegistryError":
        return cls(f"解析 '{name}' 的 SKILL.md 失败: {reason}")

    @classmethod
    def FileTooLarge(cls, name: str, size: int, max_size: int) -> "SkillRegistryError":
        return cls(f"技能文件过大 '{name}': {size} 字节 (最大 {max_size} 字节)")

    @classmethod
    def SymlinkDetected(cls, path: str) -> "SkillRegistryError":
        return cls(f"技能目录中检测到符号链接: {path}")

    @classmethod
    def GatingFailed(cls, name: str, reason: str) -> "SkillRegistryError":
        return cls(f"技能 '{name}' 门控失败: {reason}")

    @classmethod
    def TokenBudgetExceeded(cls, name: str, approx_tokens: int, declared: int) -> "SkillRegistryError":
        return cls(
            f"技能 '{name}' 提示超过 token 预算: "
            f"约 {approx_tokens} tokens 但声明 max_context_tokens={declared}"
        )

    @classmethod
    def AlreadyExists(cls, name: str) -> "SkillRegistryError":
        return cls(f"技能 '{name}' 已存在")

    @classmethod
    def CannotRemove(cls, name: str, reason: str) -> "SkillRegistryError":
        return cls(f"无法移除技能 '{name}': {reason}")

    @classmethod
    def CannotUpdate(cls, name: str, reason: str) -> "SkillRegistryError":
        return cls(f"无法更新技能 '{name}': {reason}")

    @classmethod
    def WriteError(cls, path: str, reason: str) -> "SkillRegistryError":
        return cls(f"写入技能文件失败 {path}: {reason}")


# ── 已安装技能元数据 ─────────────────────────────────────────

@dataclass
class InstalledSkillMetadata:
    """已安装技能的元数据"""
    installed_at: Optional[str] = None
    source_url: Optional[str] = None
    version: Optional[str] = None


# ── 安装文件 ─────────────────────────────────────────────────

@dataclass
class InstallFile:
    """安装期间与 SKILL.md 一起物化的额外捆绑文件"""
    relative_path: Path
    contents: bytes


# ── 已加载技能 ───────────────────────────────────────────────

@dataclass
class LoadedSkill:
    """已加载的技能"""
    manifest: Any  # SkillManifest
    prompt_content: str
    trust: SkillTrust
    source: SkillSource
    content_hash: str = ""
    compiled_patterns: List[Any] = field(default_factory=list)
    lowercased_keywords: List[str] = field(default_factory=list)
    lowercased_exclude_keywords: List[str] = field(default_factory=list)
    lowercased_tags: List[str] = field(default_factory=list)

    @staticmethod
    def compile_patterns(patterns: List[str]) -> List[Any]:
        """编译正则表达式模式列表"""
        compiled = []
        for pattern in patterns:
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error:
                logger.debug(f"跳过无效的正则表达式模式: {pattern}")
        return compiled


# ── 技能注册表 ───────────────────────────────────────────────

@dataclass(kw_only=True)
class SkillRegistry:
    """可用技能的注册表"""
    # 所有已加载的技能
    skills: List[LoadedSkill] = field(default_factory=list, init=False)
    # 用户技能目录 (~/.ironclaw/skills/)。此处的技能是 Trusted
    user_dir: Path
    # 注册表安装的技能目录 (~/.ironclaw/installed_skills/)。此处的技能是 Installed
    installed_dir: Optional[Path] = field(default=None, init=False)
    # 可选的工作区技能目录
    workspace_dir: Optional[Path] = field(default=None, init=False)
    # 编译到二进制文件中的捆绑技能内容（名称，原始 SKILL.md 内容）
    bundled_content: List[Tuple[str, str]] = field(default_factory=list, init=False)
    # 捆绑目录扫描的最大递归深度（默认：3）
    max_scan_depth: int = field(default=DEFAULT_MAX_SCAN_DEPTH, init=False)

    def with_installed_dir(self, dir: Path) -> "SkillRegistry":
        """设置注册表安装的技能目录"""
        self.installed_dir = dir
        return self

    def with_workspace_dir(self, dir: Path) -> "SkillRegistry":
        """设置工作区技能目录"""
        self.workspace_dir = dir
        return self

    def with_bundled_content(self, content: List[Tuple[str, str]]) -> "SkillRegistry":
        """设置编译到二进制文件中的捆绑技能内容"""
        self.bundled_content = content
        return self

    def with_max_scan_depth(self, depth: int) -> "SkillRegistry":
        """设置捆绑目录扫描的最大递归深度"""
        self.max_scan_depth = depth
        return self

    def clone_config_for_user_dirs(
            self, user_dir: Path, installed_dir: Optional[Path] = None
    ) -> "SkillRegistry":
        """构建具有相同共享覆盖但不同用户拥有技能根的新注册表"""
        registry = SkillRegistry(
            user_dir=user_dir,
            bundled_content=self.bundled_content,
            max_scan_depth=self.max_scan_depth,
        )
        if self.workspace_dir is not None:
            registry = registry.with_workspace_dir(self.workspace_dir)
        if installed_dir is not None:
            registry = registry.with_installed_dir(installed_dir)
        return registry

    def clone_config_for_user_scope(self, user_id: str) -> "SkillRegistry":
        """为托管用户的私有技能挂载构建新注册表"""
        return self.clone_config_for_tenant_user_scope("default", user_id)

    def clone_config_for_tenant_user_scope(self, tenant_id: str, user_id: str) -> "SkillRegistry":
        """为托管用户在租户内的私有技能挂载构建新注册表"""
        user_root = self.user_dir / ".users" / self._tenant_user_scope_segment(tenant_id, user_id)
        return self.clone_config_for_user_dirs(
            user_dir=user_root / "skills",
            installed_dir=user_root / "installed_skills",
        )

    @staticmethod
    def user_scope_segment(user_id: str) -> str:
        """托管每用户技能根的稳定文件系统段"""
        return SkillRegistry._tenant_user_scope_segment("default", user_id)

    @staticmethod
    def _tenant_user_scope_segment(tenant_id: str, user_id: str) -> str:
        """托管每租户、每用户技能根的稳定文件系统段"""
        hasher = hashlib.sha256()
        hasher.update(tenant_id.encode('utf-8'))
        hasher.update(b'\x00')
        hasher.update(user_id.encode('utf-8'))
        return hasher.hexdigest()

    async def discover_all(self) -> List[str]:
        """从所有配置的目录发现并加载技能

        发现顺序（名称冲突时较早的优先）：
        1. 工作区技能目录（如果设置）-- Trusted
        2. 用户技能目录 -- Trusted
        3. 安装的技能目录（如果设置）-- Installed
        """
        loaded_names = []
        seen = set()

        # 1. 工作区技能（最高优先级）
        if self.workspace_dir is not None:
            cap = min(MAX_DISCOVERED_SKILLS, MAX_DISCOVERED_SKILLS - len(loaded_names))
            skills = await self._discover_from_dir(
                self.workspace_dir, SkillTrust.Trusted, SkillSource.Workspace, cap, 0,
            )
            self._absorb(skills, seen, loaded_names, "workspace")

        # 2. 用户技能
        if len(loaded_names) < MAX_DISCOVERED_SKILLS:
            cap = MAX_DISCOVERED_SKILLS - len(loaded_names)
            skills = await self._discover_from_dir(
                self.user_dir, SkillTrust.Trusted, SkillSource.User, cap, 0,
            )
            self._absorb(skills, seen, loaded_names, "user")

        # 3. 安装的技能（注册表安装的）
        if len(loaded_names) < MAX_DISCOVERED_SKILLS and self.installed_dir is not None:
            cap = MAX_DISCOVERED_SKILLS - len(loaded_names)
            skills = await self._discover_from_dir(
                self.installed_dir, SkillTrust.Installed, SkillSource.Installed, cap, 0,
            )
            self._absorb(skills, seen, loaded_names, "installed")

        # 4. 捆绑技能（编译到二进制文件中，最低优先级）
        if self.bundled_content:
            bundled = self._load_bundled_skills(seen)
            for name, skill in bundled:
                seen.add(name)
                loaded_names.append(name)
                self.skills.append(skill)

        if len(loaded_names) >= MAX_DISCOVERED_SKILLS:
            logger.warning(f"全局技能发现上限已达到（{MAX_DISCOVERED_SKILLS} 个技能）")

        # 发现后伴随技能检查
        loaded_set = set(loaded_names)
        for skill in self.skills:
            requires = getattr(skill.manifest, 'requires', None)
            if requires is not None:
                companions = getattr(requires, 'skills', [])
                for companion in companions:
                    if companion not in loaded_set:
                        logger.warning(
                            f"技能 '{skill.manifest.name}' 在 `requires.skills` 中声明了伴随技能 "
                            f"'{companion}'，但未加载。通过 `skill_install` 安装它或 "
                            f"将 SKILL.md 放在 ~/.ironclaw/skills/ 中以避免降级体验。"
                        )

        return loaded_names

    def _absorb(
            self,
            skills: List[Tuple[str, LoadedSkill]],
            seen: Set[str],
            loaded_names: List[str],
            override_source: str,
    ) -> None:
        """去重并将发现的技能吸收到注册表中"""
        for name, skill in skills:
            if name in seen:
                logger.debug(f"跳过技能 '{name}'（被 {override_source} 覆盖）")
                continue
            seen.add(name)
            loaded_names.append(name)
            self.skills.append(skill)

    async def _discover_from_dir(
            self,
            dir: Path,
            trust: SkillTrust,
            source_factory: callable,
            remaining_cap: int,
            current_depth: int,
    ) -> List[Tuple[str, LoadedSkill]]:
        """从单个目录发现技能，递归进入捆绑目录"""
        results = []

        try:
            entries = list(dir.iterdir())
        except OSError as e:
            if isinstance(e, FileNotFoundError):
                logger.debug(f"技能目录不存在: {dir}")
            else:
                logger.warning(f"读取技能目录失败 {dir}: {e}")
            return results

        count = 0
        for entry in entries:
            if count >= remaining_cap:
                logger.warning(f"技能发现上限已达到（此扫描中 {count} 个技能），跳过剩余")
                break

            path = entry

            # 跳过隐藏目录
            if path.name.startswith('.'):
                logger.debug(f"跳过隐藏的技能目录条目: {path.name}")
                continue

            # 跳过符号链接
            if path.is_symlink():
                logger.warning(f"跳过技能目录中的符号链接: {path.name}")
                continue

            # 情况 1：包含 SKILL.md 的子目录
            if path.is_dir():
                skill_md = path / "SKILL.md"
                if skill_md.exists():
                    count += 1
                    source = source_factory(path)
                    try:
                        loaded_name, skill = await self._load_skill_md(skill_md, trust, source)
                        logger.debug(f"已加载技能: {loaded_name}")
                        results.append((loaded_name, skill))
                    except SkillRegistryError as e:
                        logger.warning(f"从 {path.name} 加载技能失败: {e}")
                elif current_depth < self.max_scan_depth:
                    logger.debug(f"递归进入捆绑目录 {path.name} (深度 {current_depth + 1})")
                    nested = await self._discover_from_dir(
                        path, trust, source_factory,
                        remaining_cap - count, current_depth + 1,
                    )
                    count += len(nested)
                    results.extend(nested)
                continue

            # 情况 2：直接在目录中的扁平 SKILL.md
            if path.is_file() and path.name == "SKILL.md":
                count += 1
                source = source_factory(dir)
                try:
                    loaded_name, skill = await self._load_skill_md(path, trust, source)
                    logger.debug(f"已加载技能: {loaded_name}")
                    results.append((loaded_name, skill))
                except SkillRegistryError as e:
                    logger.warning(f"加载技能失败 {path.name}: {e}")

        return results

    async def _load_skill_md(
            self, path: Path, trust: SkillTrust, source: SkillSource
    ) -> Tuple[str, LoadedSkill]:
        """加载单个 SKILL.md 文件"""
        return await load_and_validate_skill(path, trust, source)

    def _load_bundled_skills(self, seen: Set[str]) -> List[Tuple[str, LoadedSkill]]:
        """从内存内容加载捆绑技能，跳过已看到的名称"""
        results = []
        for name, content in self.bundled_content:
            if name in seen:
                logger.debug(f"跳过捆绑技能 '{name}'（被用户/工作区/安装覆盖）")
                continue
            try:
                loaded_name, skill = _load_from_content(
                    content, SkillTrust.Trusted, SkillSource.Bundled(Path(name)),
                )
                logger.debug(f"已加载捆绑技能: {loaded_name}")
                results.append((loaded_name, skill))
            except SkillRegistryError as e:
                logger.debug(f"跳过捆绑技能 '{name}': {e}")
        return results

    def skills_list(self) -> List[LoadedSkill]:
        """获取所有已加载的技能"""
        return list(self.skills)

    def count(self) -> int:
        """获取已加载技能的数量"""
        return len(self.skills)

    def retain_only(self, names: List[str]) -> None:
        """仅保留名称在给定允许列表中的技能"""
        if not names:
            return
        names_set = set(names)
        self.skills = [s for s in self.skills if s.manifest.name in names_set]

    def has(self, name: str) -> bool:
        """检查给定名称的技能是否已加载"""
        return any(s.manifest.name == name for s in self.skills)

    def find_by_name(self, name: str) -> Optional[LoadedSkill]:
        """按名称查找技能"""
        for s in self.skills:
            if s.manifest.name == name:
                return s
        return None

    async def install_skill(self, content: str) -> str:
        """在运行时从 SKILL.md 内容安装技能"""
        normalized = _normalize_line_endings(content)
        skill_name, install_content = _normalize_install_content(normalized, None)

        if self.has(skill_name):
            raise SkillRegistryError.AlreadyExists(name=skill_name)

        user_dir = self.user_dir
        name, skill = await self.prepare_install_to_disk(user_dir, skill_name, install_content)
        self.commit_install(name, skill)
        return name

    @staticmethod
    async def prepare_install_to_disk(
            install_dir: Path, skill_name: str, normalized_content: str
    ) -> Tuple[str, LoadedSkill]:
        """执行技能安装的磁盘 I/O 和加载"""
        return await SkillRegistry.prepare_install_bundle_to_disk(
            install_dir, skill_name, normalized_content, [], None,
        )

    @staticmethod
    async def prepare_install_bundle_to_disk(
            install_dir: Path,
            skill_name: str,
            normalized_content: str,
            extra_files: List[InstallFile],
            install_metadata: Optional[InstalledSkillMetadata],
    ) -> Tuple[str, LoadedSkill]:
        """执行技能捆绑安装的磁盘 I/O 和加载"""
        skill_dir = install_dir / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)

        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(normalized_content, encoding='utf-8')

        for file in extra_files:
            relative_path = _validate_install_relative_path(file.relative_path)
            absolute_path = skill_dir / relative_path
            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            absolute_path.write_bytes(file.contents)

        if install_metadata is not None:
            meta_path = skill_dir / INSTALL_METADATA_FILE_NAME
            meta_path.write_text(json.dumps(install_metadata.__dict__, indent=2, default=str))

        source = SkillSource.Installed(skill_dir)
        return await load_and_validate_skill(skill_path, SkillTrust.Installed, source)

    def commit_install(self, name: str, skill: LoadedSkill) -> None:
        """将准备好的技能提交到内存注册表中"""
        if self.has(name):
            raise SkillRegistryError.AlreadyExists(name=name)
        self.skills.append(skill)
        logger.debug(f"已安装技能: {name}")

    def validate_remove(self, name: str) -> Path:
        """验证技能可以被移除并返回其文件系统路径"""
        skill = self.find_by_name(name)
        if skill is None:
            raise SkillRegistryError.NotFound(name=name)

        if skill.source.source_type in ("User", "Installed"):
            return skill.source.path
        elif skill.source.source_type == "Workspace":
            raise SkillRegistryError.CannotRemove(
                name=name, reason="工作区技能无法通过此接口移除"
            )
        else:
            raise SkillRegistryError.CannotRemove(
                name=name, reason="捆绑技能无法移除"
            )

    def validate_update(self, name: str) -> Tuple[Path, SkillTrust, SkillSource]:
        """验证技能可以被编辑并返回重新加载所需的上下文"""
        skill = self.find_by_name(name)
        if skill is None:
            raise SkillRegistryError.NotFound(name=name)

        if skill.source.source_type in ("User", "Installed"):
            return (skill.source.path, skill.trust, skill.source)
        elif skill.source.source_type == "Workspace":
            raise SkillRegistryError.CannotUpdate(
                name=name, reason="工作区技能无法通过此接口编辑"
            )
        else:
            raise SkillRegistryError.CannotUpdate(
                name=name, reason="捆绑技能无法编辑"
            )

    @staticmethod
    async def delete_skill_files(path: Path) -> None:
        """从磁盘移除技能文件（异步 I/O）"""
        if path.exists():
            import shutil
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: shutil.rmtree(path))

    def commit_remove(self, name: str) -> None:
        """从内存注册表中移除技能"""
        for i, s in enumerate(self.skills):
            if s.manifest.name == name:
                self.skills.pop(i)
                logger.debug(f"已移除技能: {name}")
                return
        raise SkillRegistryError.NotFound(name=name)

    def commit_update(self, name: str, skill: LoadedSkill) -> None:
        """在磁盘文件已验证和重写后替换已加载的技能"""
        for i, s in enumerate(self.skills):
            if s.manifest.name == name:
                self.skills[i] = skill
                logger.debug(f"已更新技能: {name}")
                return
        raise SkillRegistryError.NotFound(name=name)

    async def remove_skill(self, name: str) -> None:
        """按名称移除技能"""
        path = self.validate_remove(name)
        await self.delete_skill_files(path)
        self.commit_remove(name)

    async def reload(self) -> List[str]:
        """清除所有已加载的技能并从磁盘重新发现"""
        self.skills.clear()
        return await self.discover_all()

    def install_target_dir(self) -> Path:
        """获取新注册表安装应写入的目录"""
        return self.installed_dir if self.installed_dir is not None else self.user_dir

    @staticmethod
    async def read_install_metadata(path: Path) -> Optional[InstalledSkillMetadata]:
        """如果存在，加载技能目录的持久化安装元数据"""
        meta_path = path / INSTALL_METADATA_FILE_NAME
        try:
            loop = asyncio.get_running_loop()
            content = await loop.run_in_executor(None, lambda: meta_path.read_text(encoding='utf-8'))
            data = json.loads(content)
            return InstalledSkillMetadata(**data)
        except Exception:
            return None


# ── 辅助函数 ─────────────────────────────────────────────────

def _normalize_line_endings(content: str) -> str:
    """规范化行尾"""
    return content.replace('\r\n', '\n').replace('\r', '\n')


def _validate_install_relative_path(path: Path) -> Path:
    """验证安装相对路径"""
    path_str = str(path)
    if not path_str or path.is_absolute():
        raise SkillRegistryError.WriteError(
            path=path_str, reason="安装捆绑路径必须是非空相对路径"
        )

    # 检查路径遍历
    for part in path.parts:
        if part == "..":
            raise SkillRegistryError.WriteError(
                path=path_str, reason="安装捆绑路径不能逃逸技能目录"
            )

    return path


def _normalize_install_content(
        normalized_content: str, requested_identifier: Optional[str]
) -> Tuple[str, str]:
    """规范化安装内容"""
    try:
        parsed = parse_skill_md(normalized_content)
        return (parsed.manifest.name, normalized_content)
    except SkillRegistryError as e:
        if "InvalidName" in str(e):
            # 尝试恢复并规范化名称
            parsed = _parse_skill_md_for_install_recovery(normalized_content)
            original_name = parsed.manifest.name
            normalized_name = requested_identifier or _normalize_skill_identifier(original_name)
            if normalized_name is None:
                raise SkillRegistryError.ParseError(
                    name=original_name,
                    reason=f"无效的技能名称 '{original_name}' 无法规范化为安全的安装名称",
                )

            logger.debug(
                f"安装期间规范化无效的技能名称: "
                f"original_name={original_name}, normalized_name={normalized_name}"
            )

            frontmatter, prompt_content = _split_skill_md_frontmatter(normalized_content)
            rewritten_yaml = _rewrite_frontmatter_name(frontmatter, normalized_name, original_name)
            rendered = _assemble_skill_md(rewritten_yaml, prompt_content)
            return (normalized_name, rendered)
        raise


def find_closing_delimiter(content: str) -> Optional[int]:
    """在单独的行上找到闭合 `---` 分隔符的位置。
    返回 `content` 中 `---` 行开头的字符偏移量。

    遍历每一行，当找到只包含 `---`（去除空白后）的行时，
    返回该行在内容中的起始字符偏移量。

    Args:
        content: 要搜索的字符串内容

    Returns:
        闭合 `---` 分隔符的字符偏移量，如果未找到则返回 None
    """
    pos = 0
    for line in content.split('\n'):
        if line.strip() == "---":
            return pos
        pos += len(line) + 1  # +1 用于换行符
    return None


def warn_on_legacy_requires(yaml_str: str, skill_name: str) -> None:
    """如果存在旧的 `metadata.openclaw.requires` 形状则发出警告。

    新的扁平 `requires:` 字段替代了它；YAML 解析器会静默丢弃旧的嵌套键，
    因此没有此警告，技能作者可能认为门控有效而它完全无效。

    Args:
        yaml_str: YAML frontmatter 字符串
        skill_name: 技能名称
    """
    if has_legacy_metadata_openclaw_requires(yaml_str):
        logger.warning(
            f"技能 '{skill_name}' 使用了旧的 `metadata.openclaw.requires` frontmatter 形状，"
            f"该形状被忽略。将要求移到顶层 `requires:` 块（包含 `bins`、`env`、`config`、`skills`），"
            f"以便门控和依赖声明生效。"
        )


def has_legacy_metadata_openclaw_requires(yaml_str: str) -> bool:
    """检测旧的 `metadata.openclaw.requires` SKILL.md frontmatter 形状。
    当存在旧形状时返回 True。

    当反序列化为 `SkillManifest` 时，解析器会静默丢弃这些嵌套字段，
    因此没有此检查，技能作者可能认为他们的门控/依赖要求得到遵守，
    而它们完全无效。

    Args:
        yaml_str: YAML frontmatter 字符串

    Returns:
        如果存在旧的 `metadata.openclaw.requires` 形状则返回 True
    """
    try:
        import yaml
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
    except Exception:
        return False


# 用于验证技能名称的正则表达式：字母数字、连字符、下划线、点。
# 必须以字母数字开头，总长度 1-64 个字符
_SKILL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def validate_skill_name(name: str) -> bool:
    """根据允许的模式验证技能名称。

    技能名称必须以字母数字字符开头，只能包含字母数字、点、连字符和下划线，
    总长度在 1 到 64 个字符之间。

    Args:
        name: 要验证的技能名称

    Returns:
        如果名称有效则返回 True，否则返回 False
    """
    return bool(_SKILL_NAME_PATTERN.match(name))



# 用于验证技能版本的正则表达式：字母数字、点、连字符、加号、下划线、波浪号。
# 长度 1-32 个字符
_SKILL_VERSION_PATTERN = re.compile(r"^[a-zA-Z0-9._\-+~]{1,32}$")


def validate_skill_version(version: str) -> bool:
    """验证技能版本字符串。参见 [`SKILL_VERSION_PATTERN`]。

    版本字符串只能包含字母数字、点、连字符、加号、下划线和波浪号，
    长度在 1 到 32 个字符之间。

    Args:
        version: 要验证的版本字符串

    Returns:
        如果版本有效则返回 True，否则返回 False
    """
    return bool(_SKILL_VERSION_PATTERN.match(version))



def parse_skill_md_impl(content: str, validate_name: bool = True) -> "ParsedSkill":
    """解析 SKILL.md 文件的内部实现

    Args:
        content: SKILL.md 文件的原始内容
        validate_name: 是否验证技能名称

    Returns:
        解析后的技能对象

    Raises:
        SkillParseError: 当解析失败时
    """
    # 在解析之前规范化行尾以处理 CRLF（调用者可能尚未预规范化）。
    # 这也使得 `find_closing_delimiter` 的字符偏移算术正确，
    # 因为它假定单字符 `\n` 分隔符
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    # 剥离可选的 UTF-8 BOM
    content = content.lstrip('\ufeff')

    # 找到第一个 `---` 分隔符（必须在第 1 行）
    trimmed = content.lstrip('\n\r')
    if not trimmed.startswith("---"):
        raise RuntimeError()

    # 找到第二个 `---` 分隔符
    after_first = trimmed[3:]
    # 跳过第一个 `---` 行的其余部分（包括任何尾随字符/换行）
    newline_pos = after_first.find('\n')
    if newline_pos == -1:
        raise RuntimeError()

    after_first_line = after_first[newline_pos + 1:]

    # 在单独的行上找到闭合的 `---`
    yaml_end = find_closing_delimiter(after_first_line)
    if yaml_end is None:
        raise RuntimeError()

    yaml_str = after_first_line[:yaml_end]

    # 解析 YAML frontmatter
    try:
        import yaml as yaml_lib
        manifest_data = yaml_lib.safe_load(yaml_str)
        if not isinstance(manifest_data, dict):
            raise RuntimeError()
    except Exception as e:
        raise RuntimeError()

    # 构建 SkillManifest 对象
    manifest = build_manifest_from_yaml(manifest_data)

    # 检测旧的 `metadata.openclaw.requires` 形状并发出警告。
    # 新的扁平 `requires:` 字段替代了它；YAML 解析器会静默丢弃旧的嵌套键，
    # 因此没有此警告，技能作者可能认为门控有效而它完全无效
    warn_on_legacy_requires(yaml_str, manifest.name)

    # 验证技能名称
    if validate_name and not validate_skill_name(manifest.name):
        raise RuntimeError()

    # 验证技能版本。编排器将此值直接插值到 XML 属性
    # （`<skill version="...">`）中，因此我们拒绝任何可能跳出属性的字符串
    if not validate_skill_version(manifest.version):
        raise RuntimeError()

    # 强制执行激活标准限制
    if hasattr(manifest.activation, 'enforce_limits'):
        manifest.activation.enforce_limits()

    # 强制执行门控要求限制（目前只有 `requires.skills` 被限制以保持
    # 链安装器的队列有界）
    if hasattr(manifest.requires, 'enforce_limits'):
        manifest.requires.enforce_limits()

    # 提取提示内容（闭合 `---` 行之后的所有内容）
    after_yaml = after_first_line[yaml_end:]
    # 跳过 `---` 行本身
    prompt_start = after_yaml.find('\n')
    if prompt_start == -1:
        prompt_start = len(after_yaml)
    else:
        prompt_start += 1
    prompt_content = after_yaml[prompt_start:].lstrip('\n')

    if not prompt_content.strip():
        raise RuntimeError()

    return ParsedSkill(manifest=manifest, prompt_content=prompt_content)

def parse_skill_md(content: str) -> ParsedSkill:
    """解析 SKILL.md 内容"""
    # 简单实现：分割 frontmatter 和内容
    lines = content.split('\n')
    if lines[0].strip() == '---':
        # 查找结束的 ---
        end_idx = 1
        while end_idx < len(lines) and lines[end_idx].strip() != '---':
            end_idx += 1
        if end_idx < len(lines):
            prompt_content = '\n'.join(lines[end_idx + 1:])
        else:
            prompt_content = ''
    else:
        prompt_content = content

    # 模拟解析结果
    class ParsedSkill:
        class Manifest:
            name = "unknown"
            activation = None
            requires = None

        manifest = Manifest()

    return ParsedSkill()


def _parse_skill_md_for_install_recovery(content: str) -> Any:
    """解析 SKILL.md 用于安装恢复"""
    return parse_skill_md(content)


def _normalize_skill_identifier(name: str) -> Optional[str]:
    """规范化技能标识符"""
    if not name:
        return None
    # 转换为小写，替换空格为连字符
    normalized = name.lower().strip()
    normalized = re.sub(r'[^a-z0-9\-_]', '-', normalized)
    normalized = re.sub(r'-+', '-', normalized)
    normalized = normalized.strip('-')
    return normalized if normalized else None


def _split_skill_md_frontmatter(content: str) -> Tuple[str, str]:
    """分割 SKILL.md 的 frontmatter 和内容"""
    lines = content.split('\n')
    if lines[0].strip() == '---':
        end_idx = 1
        while end_idx < len(lines) and lines[end_idx].strip() != '---':
            end_idx += 1
        frontmatter = '\n'.join(lines[1:end_idx])
        prompt_content = '\n'.join(lines[end_idx + 1:]) if end_idx < len(lines) else ''
        return (frontmatter, prompt_content)
    return ('', content)


def _rewrite_frontmatter_name(frontmatter: str, new_name: str, error_label: str) -> str:
    """重写 frontmatter 中的 name 字段"""
    lines = frontmatter.split('\n')
    new_lines = []
    name_replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('name:') and not name_replaced:
            new_lines.append(f"name: {new_name}")
            name_replaced = True
        else:
            new_lines.append(line)
    if not name_replaced:
        new_lines.append(f"name: {new_name}")
    return '\n'.join(new_lines)


def _assemble_skill_md(yaml: str, prompt_content: str) -> str:
    """组装 SKILL.md"""
    rendered = "---\n"
    rendered += yaml
    if not rendered.endswith('\n'):
        rendered += '\n'
    rendered += "---\n\n"
    rendered += prompt_content
    return rendered


def _load_from_content(
        raw_content: str, trust: SkillTrust, source: SkillSource
) -> Tuple[str, LoadedSkill]:
    """从内存内容加载和验证技能（无磁盘 I/O）"""
    if len(raw_content.encode('utf-8')) > MAX_PROMPT_FILE_SIZE:
        raise SkillRegistryError.FileTooLarge(
            name="(bundled)", size=len(raw_content.encode('utf-8')), max_size=MAX_PROMPT_FILE_SIZE
        )

    normalized_content = _normalize_line_endings(raw_content)
    return _build_loaded_skill(normalized_content, "(bundled)", trust, source)


def _build_loaded_skill(
        normalized_content: str, error_label: str, trust: SkillTrust, source: SkillSource
) -> Tuple[str, LoadedSkill]:
    """从规范化内容解析、验证、门控检查和构建 LoadedSkill"""
    parsed = parse_skill_md(normalized_content)
    manifest = parsed.manifest
    prompt_content = getattr(parsed, 'prompt_content', '')

    # 估算 token 数量
    approx_tokens = int(len(prompt_content.encode('utf-8')) * 0.25)
    activation = getattr(manifest, 'activation', None)
    declared = getattr(activation, 'max_context_tokens', 0) if activation else 0
    if declared > 0 and approx_tokens > declared * 2:
        raise SkillRegistryError.TokenBudgetExceeded(
            name=getattr(manifest, 'name', error_label),
            approx_tokens=approx_tokens,
            declared=declared,
        )

    content_hash = compute_hash(prompt_content)
    patterns = getattr(activation, 'patterns', []) if activation else []
    compiled_patterns = LoadedSkill.compile_patterns(patterns)
    keywords = getattr(activation, 'keywords', []) if activation else []
    exclude_keywords = getattr(activation, 'exclude_keywords', []) if activation else []
    tags = getattr(activation, 'tags', []) if activation else []

    name = getattr(manifest, 'name', error_label)
    skill = LoadedSkill(
        manifest=manifest,
        prompt_content=prompt_content,
        trust=trust,
        source=source,
        content_hash=content_hash,
        compiled_patterns=compiled_patterns,
        lowercased_keywords=[k.lower() for k in keywords],
        lowercased_exclude_keywords=[k.lower() for k in exclude_keywords],
        lowercased_tags=[t.lower() for t in tags],
    )

    return name, skill


def load_and_validate_skill(
        path: Path, trust: SkillTrust, source: SkillSource
) -> Tuple[str, LoadedSkill]:
    """从磁盘加载和验证单个 SKILL.md 文件"""
    if path.is_symlink():
        raise SkillRegistryError.SymlinkDetected(path=str(path))

    try:
        raw_bytes = path.read_bytes()
    except OSError as e:
        raise SkillRegistryError.ReadError(path=str(path), reason=str(e))

    if len(raw_bytes) > MAX_PROMPT_FILE_SIZE:
        raise SkillRegistryError.FileTooLarge(
            name=str(path), size=len(raw_bytes), max_size=MAX_PROMPT_FILE_SIZE
        )

    try:
        raw_content = raw_bytes.decode('utf-8')
    except UnicodeDecodeError as e:
        raise SkillRegistryError.ReadError(path=str(path), reason=f"无效的 UTF-8: {e}")

    normalized_content = _normalize_line_endings(raw_content)
    error_label = str(path)

    return _build_loaded_skill(normalized_content, error_label, trust, source)


def compute_hash(content: str) -> str:
    """计算内容的 SHA-256 哈希，格式为 "sha256:hex..." """
    hasher = hashlib.sha256()
    hasher.update(content.encode('utf-8'))
    return f"sha256:{hasher.hexdigest()}"

if __name__ == '__main__':
    path = Path(r"D:\jazz\projects\privateProjects\Eidoteles\delegation.md")
    trust = SkillTrust.Trusted
    source = SkillSource.Workspace(path)
    name, skill = load_and_validate_skill(
        path, trust, source
)
    print()
