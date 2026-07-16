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
from tarfile import ReadError
from typing import Optional, List, Dict, Set, Tuple, Any
from enum import Enum
import logging
from .types import (
    GatingRequirements,
    LoadedSkill,
    MAX_PROMPT_FILE_SIZE,
    # skill 来源
    SkillSource,
    SkillSourcedFromWorkspace,
    SkillSourcedFromUser,
    SkillSourcedFromInstalled,
    SkillSourcedFromBundled,
    SkillTrust
)
from .install_metadata import INSTALL_METADATA_FILE_NAME, InstalledSkillMetadata
from .validation import (
    SafeRelativePathError,
    normalize_line_endings,
    normalize_safe_relative_path,
    normalize_skill_identifier
)

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────

# 所有来源可检索到的技能总数上限
# 适用于工作区、用户及已安装目录
# 防止包含大量文件的目录造成资源耗尽
MAX_DISCOVERED_SKILLS = 100

# 捆绑目录扫描的默认递归深度
DEFAULT_MAX_SCAN_DEPTH = 3


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
        registry = (SkillRegistry(user_dir=user_dir).
                    with_bundled_content(self.bundled_content).
                    with_max_scan_depth(self.max_scan_depth))

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
        """
        从所有配置的目录发现并加载技能

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
                self.workspace_dir, SkillTrust.Trusted, SkillSourcedFromWorkspace, cap, 0,
            )
            self._absorb(skills, seen, loaded_names, "workspace")

        # 2. 用户技能
        if len(loaded_names) < MAX_DISCOVERED_SKILLS:
            cap = MAX_DISCOVERED_SKILLS - len(loaded_names)
            skills = await self._discover_from_dir(
                self.user_dir, SkillTrust.Trusted, SkillSource.UserProvenance, cap, 0,
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
                    except Exception as e:
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
                except RuntimeError as e:
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
        normalized = normalize_line_endings(content)
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


# ----------辅助函数----------


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

    return (name, skill)


# 从磁盘加载并校验单个 SKILL.md 文件
#
# 读取文件内容，校验软链接与文件大小限制，随后交由 `build_loaded_skill`
# 完成解析、合法性校验与实例构建工作
async def load_and_validate_skill(
        path: Path, trust: SkillTrust, source: SkillSource
) -> Tuple[str, LoadedSkill]:
    """从磁盘加载和验证单个 SKILL.md 文件"""
    if path.is_symlink():
        raise RuntimeError(f"路径是软链接: {str(path)}")

    try:
        raw_bytes = path.read_bytes()
    except OSError as e:
        raise ReadError(f"读取文件{str(path)}时报错: {e}")

    if len(raw_bytes) > MAX_PROMPT_FILE_SIZE:
        raise ReadError(f"skill文件太大了")

    try:
        raw_content = raw_bytes.decode('utf-8')
    except UnicodeDecodeError as e:
        raise ReadError(f"文件{str(path)}不是'utf-8'编码: {e}")

    normalized_content = normalize_line_endings(raw_content)
    error_label = str(path)

    return _build_loaded_skill(normalized_content, error_label, trust, source)
