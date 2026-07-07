"""
工作区与记忆系统（灵感源自 OpenClaw）。

工作区为智能体提供持久化记忆，采用灵活的文件系统式结构。
智能体可以创建任意的 Markdown 文件层级，这些文件将被建立索引以支持全文与语义搜索。

# 类似文件系统的 API

```text
workspace/
├── README.md              <- 根目录操作手册/索引
├── MEMORY.md              <- 长期整理记忆
├── HEARTBEAT.md           <- 周期性检查清单
├── context/               <- 身份与上下文
│   ├── vision.md
│   └── priorities.md
├── daily/                 <- 每日日志
│   ├── 2024-01-15.md
│   └── 2024-01-16.md
├── projects/              <- 任意结构
│   └── alpha/
│       ├── README.md
│       └── notes.md
└── ...
```

# 主要操作

- `read(path)` - 读取文件
- `write(path, content)` - 创建或更新文件
- `append(path, content)` - 追加内容到文件
- `list(dir)` - 列出目录内容
- `delete(path)` - 删除文件
- `search(query)` - 在所有文件中进行全文与语义搜索

# 关键模式

1. **记忆即持久化**：如果你想记住某件事，就把它写下来
2. **灵活的结构**：创建你需要的任何目录/文件层级
3. **自我文档化**：使用 README.md 文件来描述目录结构
4. **混合搜索**：通过 RRF 结合向量相似度与 BM25 全文搜索
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
import logging
from workspace.document import paths, MemoryDocument
from db.base import Database
from workspace.layer import MemoryLayer
from workspace.search import SearchConfig
import json

logger = logging.getLogger(__name__)


@dataclass
class Workspace:
    """
    作区为智能体提供基于数据库的记忆存储。

    每个工作区限定于一个用户（以及可选的智能体）。
    文档持久化到数据库并建立索引以供搜索。
    同时支持 PostgreSQL（通过 Repository）和 libSQL（通过 Database 特质）。

    ## 多作用域读取

    默认情况下，工作区从单个 `user_id` 读取和写入。
    通过 `with_additional_read_scopes`，读取操作（搜索、读取、列出）
    可以跨越多个用户作用域，而写入操作仍隔离于主 `user_id`。
    这实现了跨租户的读取访问（例如，用户同时从自己的工作区和“共享”工作区读取）。
    """

    # 作用: 所有写操作的目标 scope，即"这个 workspace 属于谁"
    # 设计原因: workspace 是按用户隔离的，写操作必须有一个明确的归属 scope，防止跨用户数据污染。
    #          所有 write()、append()、delete() 都使用 user_id 作为数据库查询的 user_id 字段
    user_id: str
    # 读操作的用户标识符。包含 `user_id` 作为第一个元素，
    # 加上通过 `with_additional_read_scopes` 添加的任何额外作用域
    # 数据库存储后端
    db: Database
    # 作用：读操作（read()、search()、list_all()）可以跨越多个 scope。
    # 第一个元素始终是 user_id，后续元素是通过 with_additional_read_scopes() 添加的额外 scope
    # 设计原因：支持"共享工作区"场景——Alice 可以读取自己的文档，同时也能读取 shared scope 的团队文档，
    # 但写操作仍然只写入 Alice 自己的 scope
    read_user_ids: List[str] = field(default_factory=list)
    # 作用：可选的 agent 隔离维度。当设置后，所有数据库查询都额外带上 agent_id 过滤条件，
    # 实现同一用户下不同 agent 的数据隔离。
    # 设计原因：多 agent 场景下（如 CEO assistant + 子 agent），
    # 每个 agent 需要独立的 workspace 命名空间，
    # 防止 agent A 的文档被 agent B 读到。默认为 None（单 agent 模式）
    agent_id: Optional[str] = None

    # 语义搜索的嵌入提供者
    embeddings: Optional["EmbeddingProvider"] = None
    # 作用：一次性标志位。当 seed_if_empty() 为全新 workspace 创建了 BOOTSTRAP.md 时，
    # 将此标志设为 true。agent loop 调用 take_bootstrap_pending()（原子 swap，读后清零）
    # 检测到 true 时，发送主动问候消息。
    #
    # 设计原因：seed_if_empty() 和 agent loop 是异步解耦的，需要一个进程内信号来传递"这是全新用户，
    # 需要发送欢迎语"这一事件，而不必再次查询数据库。
    # 使用 AtomicBool 而非普通 bool 是因为 Workspace 在多个异步任务间共享（Arc<Workspace>）
    bootstrap_pending: bool = False

    # 作用：安全网标志。当用户完成 onboarding（profile_onboarding_completed 设置被写入）后，此标志设为 true。
    # 即使 BOOTSTRAP.md 文件仍然存在于数据库中，system_prompt_for_context_inner() 也会跳过注入，
    # 防止重复触发首次运行仪式。
    #
    # 设计原因：LLM 可能完成了 onboarding 但忘记删除 BOOTSTRAP.md（或删除失败）。
    # 这个标志提供了一个独立于文件存在性的"已完成"信号，避免用户每次对话都重复经历 onboarding 流程。
    bootstrap_completed: bool = False
    # 作用: 所有搜索查询的默认配置，包含

    # 设计原因：搜索参数在全局配置（环境变量/数据库设置）中统一配置，而不是每次调用时传入。
    # search() 直接使用 search_defaults，search_with_config() 允许覆盖。
    # 这样运维人员可以通过配置调整搜索行为，无需修改代码。
    search_defaults: SearchConfig = field(default_factory=lambda: SearchConfig())
    # 作用：workspace 可访问的内存层列表。每个 MemoryLayer 有：
    #   name：层名称（如 "private"、"household"、"finance"）
    #   scope：对应的数据库 user_id（层的实际存储位置）
    #   writable：是否可写
    #   sensitivity：Private（私密）或 Shared（共享）

    # 设计原因：支持家庭/团队场景，不同类型的数据存储在不同的逻辑层中。
    # 例如，家庭 workspace 可以有 private（个人私密）和 household（家庭共享）两层，
    # LLM 根据内容类型选择写入哪一层。默认只有一个 private 层。
    memory_layers: List[MemoryLayer] = field(default_factory=list)
    # 作用：可选的隐私分类器。当向 Shared 层写入时，分类器检测内容是否敏感（如医疗信息、电话号码）。
    # 若敏感，自动将写操作重定向到 Private 层，防止私密信息泄漏到共享层。
    #
    # 设计原因：LLM 通过系统提示指导选择正确的层，但可能判断失误。
    # 分类器作为安全网，在 LLM 误判时自动保护隐私。默认为 None（不启用），
    # 因为正则分类器在家庭场景中误报率较高（"doctor"、"therapy" 等词不一定是私密内容）。
    privacy_classifier: Optional["PrivacyClassifier"] = None
    # 作用：多租户模式下，是否从 __admin__ scope 读取 SYSTEM.md 并注入系统提示。
    # 由 WorkspacePool 在多租户模式下设置为 true。
    #
    # 设计原因：SaaS 部署场景中，管理员需要向所有用户的 LLM 注入统一的系统指令（如合规要求、品牌语调）。
    # 通过 __admin__ 这个保留 scope（双下划线前缀防止与真实用户 ID 冲突）存储，
    # 所有用户的 workspace 都能读取，但只有管理员能写入。
    admin_prompt_enabled: bool = False
    # 作用：多租户模式下，所有用户的 workspace 共享同一个 admin 系统提示缓存。
    # WorkspacePool 创建一个 Arc<RwLock<Option<String>>> 并注入到每个用户的 workspace 中。
    # 第一次读取时从数据库加载并写入缓存，后续直接从内存读取。
    #
    # 设计原因：多租户部署中，每次对话都从数据库读取 SYSTEM.md 会产生大量重复 I/O。
    # 通过共享缓存，N 个并发用户只需一次数据库读取。使用 RwLock 允许多个读者并发，写者（缓存填充）独占
    admin_prompt_cache: Optional[Any] = None

    def __post_init__(self):
        if not self.read_user_ids:
            self.read_user_ids = [self.user_id]

        self.memory_layers.append(MemoryLayer(self.user_id))

    async def read_primary(self, path: str) -> "MemoryDocument":
        """从**仅主要作用域**读取文件，忽略额外的读作用域。
        用于标识和配置文件（AGENTS.md、SOUL.md、USER.md、IDENTITY.md、TOOLS.md、BOOTSTRAP.md），
        其中从另一个作用域继承内容将是正确性/安全问题"""
        path = normalize_path(path)
        return await self.db.get_document_by_path(self.user_id, self.agent_id, path)

    async def seed_if_empty(self) -> int:
        """播种工作区中任何缺失的核心标识文件

        每次启动时调用。仅创建尚不存在的文件，因此用户编辑永远不会被覆盖。
        返回创建的文件数（如果所有核心文件已存在则返回 0）
        """

        seed_files: List[Tuple[str, str]] = [
            ("README.md", README_SEED),
            ("MEMORY.md", MEMORY_SEED),
            ("IDENTITY.md", IDENTITY_SEED),
            ("SOUL.md", SOUL_SEED),
            ("AGENTS.md", AGENTS_SEED),
            ("USER.md", USER_SEED),
            ("HEARTBEAT.md", HEARTBEAT_SEED),
            ("TOOLS.md", TOOLS_SEED),
            (".system/gateway/README.md", FRONTEND_SEED),
        ]

        # 在播种标识文件之前检查新鲜度，否则播种的文件会使工作区看起来不新鲜，
        # BOOTSTRAP.md 永远不会被创建
        is_fresh_workspace = False
        try:
            await self.read_primary("BOOTSTRAP.md")
            # BOOTSTRAP 已存在
        except Exception:
            agents_res = None
            soul_res = None
            user_res = None
            try:
                agents_res = await self.read_primary("AGENTS.md")
            except Exception:
                pass
            try:
                soul_res = await self.read_primary("SOUL.md")
            except Exception:
                pass
            try:
                user_res = await self.read_primary("USER.md")
            except Exception:
                pass

            is_fresh_workspace = (
                    isinstance(agents_res, DocumentNotFoundError)
                    and isinstance(soul_res, DocumentNotFoundError)
                    and isinstance(user_res, DocumentNotFoundError)
            )

        count = 0
        for path, content in seed_files:
            # 跳过主要作用域中已存在的文件（永不覆盖用户编辑）。
            # 使用 read_primary 以避免来自次要作用域的误报 —
            # 另一个作用域中的文件不应抑制此作用域中的播种
            try:
                await self.read_primary(path)
                continue
            except Exception:
                pass
            except Exception as e:
                logger.debug(f"检查 {path} 失败: {e}")
                continue

            try:
                await self.write(path, content)
                count += 1
            except Exception as e:
                logger.debug(f"播种 {path} 失败: {e}")

        # 为卫生默认值播种文件夹级别的 .config 文档
        config_seeds: list[tuple[str, dict]] = [
            (
                "daily/.config",
                {
                    "hygiene": {"enabled": True, "retention_days": 30},
                    "skip_versioning": True,
                },
            ),
            (
                "conversations/.config",
                {
                    "hygiene": {"enabled": True, "retention_days": 7},
                    "skip_versioning": True,
                },
            ),
            (
                ".system/gateway/.config",
                {"skip_indexing": True},
            ),
        ]

        for config_path, metadata_value in config_seeds:
            try:
                await self.read_primary(config_path)
                continue  # 已存在，不覆盖
            except DocumentNotFoundError:
                pass
            except Exception as e:
                logger.debug(f"检查 {config_path} 失败: {e}")
                continue

            # 创建带有元数据的空文档
            try:
                doc = await self.db.get_or_create_document_by_path(
                    self.user_id, self.agent_id, config_path,
                )
                await self.db.update_document_metadata(doc.id, metadata_value)
                count += 1
            except Exception as e:
                logger.debug(f"在 {config_path} 上设置元数据失败: {e}")

        # BOOTSTRAP.md 仅在真正新鲜的工作区上播种（播种前不存在标识文件）
        # 且尚不存在个人资料时（用户可能已有来自先前安装的个人资料，不需要引导）。
        # 这防止现有用户在升级后获得虚假的首次运行仪式。
        # 使用 read_primary() 以避免来自次要作用域的误报
        has_profile = False
        try:
            doc = await self.read_primary("context/profile.json")
            if doc.content.strip():
                try:
                    profile = json.loads(doc.content)
                    if isinstance(profile, dict):
                        has_profile = True
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass

        if is_fresh_workspace and not has_profile:
            try:
                await self.write("BOOTSTRAP.md", BOOTSTRAP_SEED)
                self.bootstrap_pending = True
                count += 1
            except Exception as e:
                logger.warning(f"播种 BOOTSTRAP.md 失败: {e}")

        if count > 0:
            logger.debug(f"播种了 {count} 个工作区文件")
        return count

    def take_bootstrap_pending(self) -> bool:
        """如果 `seed_if_empty()` 为新工作区创建了 BOOTSTRAP.md 则返回 `True`（一次）。
        标志在读取时清除，因此调用者只执行一次"""
        with self._bootstrap_lock:
            was_pending = self.bootstrap_pending
            self.bootstrap_pending = False
            return was_pending

    def mark_bootstrap_completed(self) -> None:
        """将引导标记为已完成。设置后，即使文件仍存在于工作区中，BOOTSTRAP.md 注入也被抑制"""
        self.bootstrap_completed = True

    def is_bootstrap_completed(self) -> bool:
        """检查引导安全网标志是否已设置"""
        return self.bootstrap_completed

    def with_agent(self, agent_id: uuid.UUID) -> "Workspace":
        """使用特定代理 ID 创建工作区"""
        self.agent_id = agent_id
        return self

    def with_embeddings(self, provider: "EmbeddingProvider") -> "Workspace":
        """为语义搜索设置嵌入提供者。提供者自动包装在 CachedEmbeddingProvider 中，使用默认缓存大小"""
        self.embeddings = CachedEmbeddingProvider(provider, EmbeddingCacheConfig())
        return self

    def with_embeddings_cached(
            self, provider: "EmbeddingProvider", cache_config: "EmbeddingCacheConfig"
    ) -> "Workspace":
        """使用自定义缓存配置设置嵌入提供者"""
        self.embeddings = CachedEmbeddingProvider(provider, cache_config)
        return self

    def with_embeddings_uncached(self, provider: "EmbeddingProvider") -> "Workspace":
        """设置嵌入提供者**不带**缓存（用于测试）"""
        self.embeddings = provider
        return self

    def with_search_config(self, config: "WorkspaceSearchConfig") -> "Workspace":
        """从工作区搜索配置设置默认搜索配置"""
        self.search_defaults = SearchConfig(
            fusion_strategy=config.fusion_strategy,
            rrf_k=config.rrf_k,
            fts_weight=config.fts_weight,
            vector_weight=config.vector_weight,
        )
        return self

    def with_memory_layers(self, layers: List["MemoryLayer"]) -> "Workspace":
        """为此工作区配置内存层。同时更新 read_user_ids 以包含所有层作用域"""
        for layer in layers:
            if layer.scope not in self.read_user_ids:
                self.read_user_ids.append(layer.scope)
        self.memory_layers = layers
        return self

    def with_privacy_classifier(self, classifier: "PrivacyClassifier") -> "Workspace":
        """为共享层写入设置隐私分类器。设置后，对共享层的写入会根据分类器检查，
        如果检测到敏感内容则重定向到私有层"""
        self.privacy_classifier = classifier
        return self

    def with_admin_prompt(self) -> "Workspace":
        """启用从 `__admin__` 作用域读取管理员系统提示。
        启用后，`system_prompt_for_context_inner()` 从 `__admin__` 作用域读取 `SYSTEM.md`
        并在标识文件之前注入。仅在多租户模式下设置（通过 `WorkspacePool`）"""
        self.admin_prompt_enabled = True
        return self

    def with_admin_prompt_cache(self, cache: Any) -> "Workspace":
        """设置共享的管理员提示缓存（来自 `WorkspacePool`）"""
        self.admin_prompt_cache = cache
        return self

    def memory_layers_list(self) -> List["MemoryLayer"]:
        """获取配置的内存层"""
        return list(self.memory_layers)

    def with_additional_read_scopes(self, scopes: List[str]) -> "Workspace":
        """为读操作添加额外的用户作用域。主要 `user_id` 始终包含在内。
        额外作用域允许读操作（搜索、读取、列表）跨多个租户，
        而写入保持隔离到主要作用域。重复作用域被忽略"""
        for scope in scopes:
            if scope not in self.read_user_ids:
                self.read_user_ids.append(scope)
        return self

    def scoped_to_user(self, user_id: str) -> "Workspace":
        """为不同的主要用户作用域克隆工作区配置。
        保留搜索配置、嵌入、共享读作用域、内存层和隐私分类器，
        同时将主要读/写作用域切换为 `user_id`"""
        private_source_scopes = [
            layer.scope for layer in self.memory_layers
            if layer.sensitivity == LayerSensitivity.Private
        ]
        non_private_source_scopes = [
            layer.scope for layer in self.memory_layers
            if layer.sensitivity != LayerSensitivity.Private
        ]

        memory_layers = [layer.clone() for layer in self.memory_layers]
        for layer in memory_layers:
            if layer.sensitivity == LayerSensitivity.Private:
                layer.scope = user_id

        read_user_ids = [user_id]
        for scope in self.read_user_ids:
            used_by_non_private_layer = scope in non_private_source_scopes
            old_primary_private_scope = scope == self.user_id and not used_by_non_private_layer
            private_layer_source_scope = scope in private_source_scopes and not used_by_non_private_layer

            if not old_primary_private_scope and not private_layer_source_scope and scope not in read_user_ids:
                read_user_ids.append(scope)

        for scope in MemoryLayer.read_scopes(memory_layers):
            if scope not in read_user_ids:
                read_user_ids.append(scope)

        preserve_flags = user_id == self.user_id
        return Workspace(
            user_id=user_id,
            read_user_ids=read_user_ids,
            agent_id=self.agent_id,
            db=self.db.clone() if hasattr(self.db, 'clone') else self.db,
            embeddings=self.embeddings,
            bootstrap_pending=self.bootstrap_pending if preserve_flags else False,
            bootstrap_completed=self.bootstrap_completed if preserve_flags else False,
            search_defaults=self.search_defaults,
            memory_layers=memory_layers,
            privacy_classifier=self.privacy_classifier,
            admin_prompt_enabled=self.admin_prompt_enabled,
            admin_prompt_cache=self.admin_prompt_cache,
        )

    def is_multi_scope(self) -> bool:
        """此工作区是否有多个读作用域"""
        return len(self.read_user_ids) > 1

    # ==================== 文件操作 ====================

    async def read(self, path: str) -> "MemoryDocument":
        """按路径读取文件。返回文档（如果存在），如果未找到则返回错误"""
        path = normalize_path(path)
        if self.is_multi_scope() and is_identity_path(path):
            return await self.db.get_document_by_path(self.user_id, self.agent_id, path)
        elif self.is_multi_scope():
            return await self.db.get_document_by_path_multi(self.read_user_ids, self.agent_id, path)
        else:
            return await self.db.get_document_by_path(self.user_id, self.agent_id, path)

    async def get_or_create(self, path: str) -> "MemoryDocument":
        """获取或创建给定路径的文档。如果不存在则创建空内容的文档。不触发重新索引或版本控制"""
        path = normalize_path(path)
        return await self.db.get_or_create_document_by_path(self.user_id, self.agent_id, path)

    async def update_metadata(self, id: uuid.UUID, metadata: dict) -> None:
        """按 ID 更新文档的元数据 JSON（完全替换）"""
        await self.db.update_document_metadata(id, metadata)

    async def prune_versions(self, document_id: uuid.UUID, keep_count: int) -> int:
        """修剪文档的旧版本，仅保留最近的 `keep_count` 个。返回删除的版本数"""
        return await self.db.prune_versions(document_id, keep_count)

    async def find_config_documents(self) -> List["MemoryDocument"]:
        """查找此工作区作用域中的所有 `.config` 文档"""
        return await self.db.find_config_documents(self.user_id, self.agent_id)

    async def resolve_metadata(self, path: str) -> "DocumentMetadata":
        """解析主要作用域中文档路径的有效元数据。
        解析链：文档自己的元数据 → 最近的祖先 `.config` → 默认值"""
        return await self.resolve_metadata_in_scope(self.user_id, path)

    async def resolve_metadata_in_scope(self, scope: str, path: str) -> "DocumentMetadata":
        """解析特定作用域中文档路径的有效元数据。用于层感知写入"""
        path = normalize_path(path)
        doc_meta = None
        try:
            doc = await self.db.get_document_by_path(scope, self.agent_id, path)
            doc_meta = doc.metadata
        except Exception:
            pass

        config_meta = None
        try:
            configs = await self.db.find_config_documents(scope, self.agent_id)
            config_meta = find_nearest_config(path, configs)
        except Exception:
            pass

        base = config_meta if config_meta is not None else {}
        overlay = doc_meta if doc_meta is not None else {}
        merged = DocumentMetadata.merge(base, overlay)
        return DocumentMetadata.from_value(merged)

    async def list_versions(self, document_id: uuid.UUID, limit: int) -> List["VersionSummary"]:
        """列出文档的版本（最新的在前）"""
        return await self.db.list_versions(document_id, limit)

    async def get_version(self, document_id: uuid.UUID, version: int) -> "DocumentVersion":
        """获取文档的特定版本"""
        return await self.db.get_version(document_id, version)

    async def maybe_save_version(
            self,
            document_id: uuid.UUID,
            current_content: str,
            metadata: "DocumentMetadata",
            changed_by: Optional[str] = None,
    ) -> Optional[int]:
        """如果当前内容与最新版本不同，则保存为版本。返回新版本号，如果跳过则返回 None"""
        if not current_content:
            return None
        if metadata.skip_versioning is True:
            return None

        hash_val = content_sha256(current_content)
        try:
            versions = await self.db.list_versions(document_id, 1)
            if versions and versions[0].content_hash == hash_val:
                return None
        except Exception:
            pass

        return await self.db.save_version(document_id, current_content, hash_val, changed_by)

    async def patch(
            self, path: str, old_string: str, new_string: str, replace_all: bool
    ) -> "PatchResult":
        """对工作区文档应用搜索替换补丁"""
        if not old_string:
            raise WorkspaceError(f"补丁失败: old_string 不能为空 (path={path})")
        path = normalize_path(path)
        doc = await self.db.get_document_by_path(self.user_id, self.agent_id, path)

        if old_string not in doc.content:
            raise WorkspaceError(f"补丁失败: 在文档中未找到 old_string (path={path})")

        if replace_all:
            count = doc.content.count(old_string)
            new_content = doc.content.replace(old_string, new_string)
        else:
            count = 1
            new_content = doc.content.replace(old_string, new_string, 1)

        if is_system_prompt_file(path) and new_content:
            reject_if_injected(path, new_content)

        metadata = await self.resolve_metadata(path)
        if metadata.schema is not None:
            validate_content_against_schema(path, new_content, metadata.schema)

        await self.maybe_save_version(doc.id, doc.content, metadata, self.user_id)
        await self.db.update_document(doc.id, new_content)
        await self.reindex_document_with_metadata(doc.id, metadata)
        updated = await self.db.get_document_by_id(doc.id)
        return PatchResult(document=updated, replacements=count)

    async def write(self, path: str, content: str) -> "MemoryDocument":
        """写入（创建或更新）文件。隐式创建父目录。写入后为搜索重新索引文档"""
        path = normalize_path(path)
        if is_system_prompt_file(path) and content:
            reject_if_injected(path, content)

        doc = await self.db.get_or_create_document_by_path(self.user_id, self.agent_id, path)

        if is_engine_runtime_path(path):
            # 一次性清理：删除在此守卫存在之前创建的任何块
            try:
                await self.db.delete_chunks(doc.id)
            except Exception:
                pass

            if doc.content == content:
                return doc
            skip_meta = DocumentMetadata(skip_indexing=True, skip_versioning=True)
            await self.maybe_save_version(doc.id, doc.content, skip_meta, self.user_id)
            await self.db.update_document(doc.id, content)
            return await self.db.get_document_by_id(doc.id)

        if doc.content == content:
            metadata = await self.resolve_metadata(path)
            await self.reindex_document_with_metadata(doc.id, metadata)
            return doc

        metadata = await self.resolve_metadata(path)
        if metadata.schema is not None:
            validate_content_against_schema(path, content, metadata.schema)

        await self.maybe_save_version(doc.id, doc.content, metadata, self.user_id)
        await self.db.update_document(doc.id, content)
        await self.reindex_document_with_metadata(doc.id, metadata)
        return await self.db.get_document_by_id(doc.id)

    async def append(self, path: str, content: str) -> None:
        """追加内容到文件。如果不存在则创建。使用单个 `\\n` 分隔符"""
        path = normalize_path(path)
        if is_system_prompt_file(path) and content:
            reject_if_injected(path, content)

        doc = await self.db.get_or_create_document_by_path(self.user_id, self.agent_id, path)
        new_content = content if not doc.content else f"{doc.content}\n{content}"

        if is_system_prompt_file(path) and new_content:
            reject_if_injected(path, new_content)

        metadata = await self.resolve_metadata(path)
        if metadata.schema is not None:
            validate_content_against_schema(path, new_content, metadata.schema)

        await self.maybe_save_version(doc.id, doc.content, metadata, self.user_id)
        await self.db.update_document(doc.id, new_content)
        await self.reindex_document_with_metadata(doc.id, metadata)

    async def exists(self, path: str) -> bool:
        """检查文件是否存在。配置多作用域读取时，检查所有读作用域"""
        path = normalize_path(path)
        try:
            if self.is_multi_scope() and is_identity_path(path):
                await self.db.get_document_by_path(self.user_id, self.agent_id, path)
            elif self.is_multi_scope():
                await self.db.get_document_by_path_multi(self.read_user_ids, self.agent_id, path)
            else:
                await self.db.get_document_by_path(self.user_id, self.agent_id, path)
            return True
        except DocumentNotFoundError:
            return False

    async def delete(self, path: str) -> None:
        """删除文件。同时删除关联的块"""
        path = normalize_path(path)
        await self.db.delete_document_by_path(self.user_id, self.agent_id, path)

    async def list(self, directory: str) -> List["WorkspaceEntry"]:
        """列出路径中的文件和目录。返回直接子项（非递归）"""
        directory = normalize_directory(directory)
        if self.is_multi_scope():
            primary = await self.db.list_directory(self.user_id, self.agent_id, directory)
            all_entries = list(primary)
            for scope in self.read_user_ids[1:]:
                entries = await self.db.list_directory(scope, self.agent_id, directory)
                all_entries.extend(e for e in entries if not is_identity_path(e.path))
            return merge_workspace_entries(all_entries)
        else:
            return await self.db.list_directory(self.user_id, self.agent_id, directory)

    async def list_all(self) -> List[str]:
        """递归列出所有文件（所有路径的扁平列表）"""
        if self.is_multi_scope():
            all_paths = await self.db.list_all_paths(self.user_id, self.agent_id)
            for scope in self.read_user_ids[1:]:
                paths = await self.db.list_all_paths(scope, self.agent_id)
                all_paths.extend(p for p in paths if not is_identity_path(p))
            all_paths = sorted(set(all_paths))
            return all_paths
        else:
            return await self.db.list_all_paths(self.user_id, self.agent_id)

    # ==================== 便捷方法 ====================

    async def memory(self) -> "MemoryDocument":
        """获取主要的 MEMORY.md 文档（长期精选记忆）。如果不存在则创建"""
        return await self.read_or_create("MEMORY.md")

    async def today_log(self) -> "MemoryDocument":
        """获取今天的每日日志。每日日志仅追加，按日期索引"""
        today = date.today()
        return await self.daily_log(today)

    async def daily_log(self, dt: date) -> "MemoryDocument":
        """获取特定日期的每日日志"""
        path = f"daily/{dt.strftime('%Y-%m-%d')}.md"
        return await self.read_or_create(path)

    async def heartbeat_checklist(self) -> Optional[str]:
        """获取心跳清单（HEARTBEAT.md）。返回数据库存储的清单（如果存在），否则回退到内存种子模板"""
        try:
            doc = await self.read_primary("HEARTBEAT.md")
            return doc.content
        except DocumentNotFoundError:
            return HEARTBEAT_SEED

    async def read_or_create(self, path: str) -> "MemoryDocument":
        """读取或创建文件的辅助方法。多作用域读取时，在创建之前检查所有读作用域"""
        if self.is_multi_scope():
            try:
                return await self.db.get_document_by_path_multi(
                    self.read_user_ids, self.agent_id, path,
                )
            except DocumentNotFoundError:
                pass
        return await self.db.get_or_create_document_by_path(self.user_id, self.agent_id, path)

    # ==================== 内存操作 ====================

    async def append_memory(self, entry: str) -> None:
        """向主要的 MEMORY.md 文档追加条目。用于值得长期记住的重要事实、决策和偏好"""
        doc = await self.db.get_or_create_document_by_path(
            self.user_id, self.agent_id, "MEMORY.md",
        )
        new_content = entry if not doc.content else f"{doc.content}\n\n{entry}"
        metadata = await self.resolve_metadata("MEMORY.md")
        await self.maybe_save_version(doc.id, doc.content, metadata, self.user_id)
        await self.db.update_document(doc.id, new_content)
        await self.reindex_document_with_metadata(doc.id, metadata)

    async def append_daily_log(self, entry: str) -> None:
        """向今天的每日日志追加条目"""
        await self.append_daily_log_tz(entry, "UTC")

    async def append_daily_log_tz(self, entry: str, tz: str) -> str:
        """使用给定时区向今天的每日日志追加条目。返回写入的路径"""
        from datetime import datetime, timezone as dt_timezone
        now = datetime.now(dt_timezone.utc)
        today = now.date()
        path = f"daily/{today.strftime('%Y-%m-%d')}.md"
        timestamp = now.strftime("%H:%M:%S")
        timestamped_entry = f"[{timestamp}] {entry}"
        await self.append(path, timestamped_entry)
        return path

    # ==================== 系统提示 ====================

    async def system_prompt(self) -> str:
        """从标识文件构建系统提示"""
        return await self.system_prompt_for_context(False)

    async def system_prompt_for_context_tz(self, is_group_chat: bool, tz: str) -> str:
        """使用时区感知的每日日志日期构建系统提示"""
        return await self.system_prompt_for_context_inner(is_group_chat, tz)

    async def system_prompt_for_context(self, is_group_chat: bool) -> str:
        """构建系统提示，可选排除个人记忆。当 `is_group_chat` 为 true 时，排除 MEMORY.md"""
        return await self.system_prompt_for_context_inner(is_group_chat, None)

    async def read_admin_prompt(self) -> Optional[str]:
        """读取管理员系统提示，如果可用则使用共享缓存"""
        if self.admin_prompt_cache is not None:
            cached = await self.admin_prompt_cache.read()
            if cached is not None:
                return cached if cached else None

        try:
            doc = await self.db.get_document_by_path("__admin__", None, "SYSTEM.md")
            content = doc.content if doc.content else None
        except DocumentNotFoundError:
            content = None
        except Exception as e:
            content = None

        if self.admin_prompt_cache is not None:
            await self.admin_prompt_cache.write(content or "")

        return content

    async def system_prompt_for_context_inner(
            self, is_group_chat: bool, tz: Optional[str] = None
    ) -> str:
        """系统提示构建的内部实现"""
        parts = []

        # 引导注入
        bootstrap_injected = False
        if self.is_bootstrap_completed():
            try:
                doc = await self.read_primary("BOOTSTRAP.md")
                if doc.content:
                    pass  # 引导已完成，跳过
            except Exception:
                pass
        else:
            try:
                doc = await self.read_primary("BOOTSTRAP.md")
                if doc.content:
                    parts.append(f"## 首次运行引导\n\n{doc.content}")
                    bootstrap_injected = True
            except Exception:
                pass

        # 管理员系统提示
        if self.admin_prompt_enabled:
            admin_content = await self.read_admin_prompt()
            if admin_content:
                parts.append(f"## 系统指令\n\n{admin_content}")

        # 标识文件
        identity_files = [
            ("AGENTS.md", "## 代理指令"),
            ("SOUL.md", "## 核心价值观"),
            ("USER.md", "## 用户上下文"),
            ("IDENTITY.md", "## 身份"),
        ]
        for path, header in identity_files:
            try:
                doc = await self.read_primary(path)
                if doc.content:
                    parts.append(f"{header}\n\n{doc.content}")
            except Exception:
                pass

        # 工具备注
        try:
            doc = await self.read_primary("TOOLS.md")
            if doc.content:
                parts.append(f"## 工具备注\n\n{doc.content}")
        except Exception:
            pass

        # MEMORY.md（仅直接/主会话，非群聊）
        if not is_group_chat:
            try:
                doc = await self.read("MEMORY.md")
                if doc.content:
                    parts.append(f"## 长期记忆\n\n{doc.content}")
            except Exception:
                pass

        # 每日日志
        today = date.today()
        yesterday = today.replace(day=today.day - 1) if today.day > 1 else today

        for dt, header in [(today, "## 今日笔记"), (yesterday, "## 昨日笔记")]:
            try:
                doc = await self.daily_log(dt)
                if doc.content:
                    parts.append(f"{header}\n\n{doc.content}")
            except Exception:
                pass

        # 个人资料个性化（非群聊）
        if not is_group_chat:
            has_profile_doc = False
            try:
                doc = await self.read("context/profile.json")
                if doc.content:
                    import json
                    profile = json.loads(doc.content)
                    has_profile_doc = True
                    if profile.get("is_populated"):
                        cohort = profile.get("cohort", {}).get("cohort", "unknown")
                        tone = profile.get("communication", {}).get("tone", "balanced")
                        detail = profile.get("communication", {}).get("detail_level", "moderate")
                        proactivity = profile.get("assistance", {}).get("proactivity", "reactive")
                        parts.append(
                            f"## 交互风格\n\n{cohort} | {tone} 语气 | {detail} 细节 | {proactivity} 主动性"
                        )
            except Exception:
                pass

            if bootstrap_injected and not has_profile_doc:
                parts.append(
                    "个人资料分析框架:\n...\n个人资料 JSON 模式:\n使用 `memory_write` 写入 `context/profile.json`..."
                )

            try:
                doc = await self.read("context/assistant-directives.md")
                if doc.content:
                    parts.append(doc.content)
            except Exception:
                pass

        return "\n\n---\n\n".join(parts)

    async def sync_profile_documents(self) -> bool:
        """从心理画像同步派生的标识文档。如果文档已同步则返回 True，如果跳过则返回 False"""
        try:
            doc = await self.read("context/profile.json")
            if not doc.content:
                return False
        except Exception:
            return False

        import json
        try:
            profile = json.loads(doc.content)
        except json.JSONDecodeError:
            return False

        if not profile.get("is_populated"):
            return False

        new_profile_content = profile.get("to_user_md", "")
        try:
            existing = await self.read("USER.md")
            merged = merge_profile_section(existing.content, new_profile_content)
        except Exception:
            merged = f"<!-- PROFILE_START -->\n{new_profile_content}\n<!-- PROFILE_END -->"

        await self.write("USER.md", merged)

        directives = profile.get("to_assistant_directives", "")
        await self.write("context/assistant-directives.md", directives)

        try:
            await self.read("HEARTBEAT.md")
        except Exception:
            heartbeat = profile.get("to_heartbeat_md", "")
            await self.write("HEARTBEAT.md", heartbeat)

        return True


def normalize_path(path: str) -> str:
    """规范化文件路径（移除首尾斜杠，合并连续的 //）

    Args:
        path: 要规范化的文件路径

    Returns:
        规范化后的路径字符串
    """
    path = path.strip().strip('/')

    # 合并连续的斜杠
    result = []
    last_was_slash = False
    for c in path:
        if c == '/':
            if not last_was_slash:
                result.append(c)
            last_was_slash = True
        else:
            result.append(c)
            last_was_slash = False

    return ''.join(result)
