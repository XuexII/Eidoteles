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


from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
import logging
from workspace.document import paths, MemoryDocument

logger = logging.getLogger(__name__)


# 工作区为智能体提供基于数据库的记忆存储。

# 每个工作区限定于一个用户（以及可选的智能体）。
# 文档持久化到数据库并建立索引以供搜索。
# 同时支持 PostgreSQL（通过 Repository）和 libSQL（通过 Database 特质）。
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

    # 用户标识符（来自频道）。所有写入都进入此作用域
    user_id: str
    # 读操作的用户标识符。包含 `user_id` 作为第一个元素，
    # 加上通过 `with_additional_read_scopes` 添加的任何额外作用域
    read_user_ids: List[str] = field(default_factory=list)
    # 可选的多代理隔离代理 ID
    agent_id: Optional[str] = None
    # 数据库存储后端
    storage: "WorkspaceStorage" = None
    # 语义搜索的嵌入提供者
    embeddings: Optional["EmbeddingProvider"] = None
    # 由 `seed_if_empty()` 在新生成 BOOTSTRAP.md 时设置。
    # 代理循环检查并清除此标志以发送主动问候
    bootstrap_pending: bool = False
    # 安全网：为 true 时，即使文件仍然存在，BOOTSTRAP.md 注入也被抑制。
    # 从 `profile_onboarding_completed` 设置中设置
    bootstrap_completed: bool = False
    # 应用于所有查询的默认搜索配置
    search_defaults: "SearchConfig" = field(default_factory=lambda: SearchConfig())
    # 此工作区可访问的内存层
    memory_layers: List["MemoryLayer"] = field(default_factory=list)
    # 可选的共享层写入隐私分类器。
    # 为 None 时，写入精确到达请求的位置 — 无静默重定向
    privacy_classifier: Optional["PrivacyClassifier"] = None
    # 为 true 时，系统提示包含来自 `__admin__` 作用域的管理员定义指令。
    # 在多租户模式下由 `WorkspacePool` 设置
    admin_prompt_enabled: bool = False
    # 管理员系统提示的共享缓存。当为 `Some` 时，工作区从此缓存读取，
    # 而不是在每个轮次都访问数据库。在多租户模式下由 `WorkspacePool` 填充
    admin_prompt_cache: Optional[Any] = None  # Arc<RwLock<Option<String>>>


    def __post_init__(self):
        if not self.read_user_ids:
            self.read_user_ids = [self.user_id]

    async def read(self, path: str) -> MemoryDocument:
        """
        按路径读取文件。
        如果文档存在则返回，否则返回错误。

        # 示例
        ```ignore
        let doc = workspace.read("context/vision.md").await?;
        println!("{}", doc.content);
        ```
        """

        return await self.storage.get_document_by_path(self.user_id, self.agent_id, path)

    async def system_prompt_for_context_tz(self, is_group_chat: bool, tz):
        """
        构建系统提示词，使用带时区信息的每日日志日期。

        使用给定的时区来确定“今天”和“昨天”，用于注入每日日志。
        """

        return await self.system_prompt_for_context_inner(is_group_chat, tz)

    async def system_prompt_for_context_inner(self, is_group_chat: bool, tz):
        """
        用于构建系统提示词的内部实现。
        """
        parts = []

        # 引导仪式：当 BOOTSTRAP.md 存在时将其注入（仅首次运行时）。
        # 智能体必须完成仪式并随后删除此文件。
        #
        # 注意：BOOTSTRAP.md 特意不设写保护，以便智能体在完成引导后能够删除它。
        # 这意味着提示注入攻击可能会写入该文件，但该文件仅会在下一次会话时被注入
        # （而非当前会话），从而限制了影响范围。
        if doc := await self.read(paths.BOOTSTRAP):
            parts.append(
                "## 首次运行引导\n\n"
                "工作区中存在 BOOTSTRAP.md 文件。请阅读并遵循其中指示，"
                "完成后将其删除。\n\n"
                f"{doc.content}"
            )

        # 按重要程度加载身份文件。
        identity_files = [
            (paths.AGENTS, "## Agent Instructions"),
            (paths.SOUL, "## Core Values"),
            (paths.USER, "## User Context"),
            (paths.IDENTITY, "## Identity"),
        ]
        for (path, header) in identity_files:
            if doc := await self.read(path):
                parts.append(f"{header}\n\n{doc}")

        # 工具说明：代理或用户编写的特定环境指南。
        # TOOLS.md 文件不控制工具的可用性；它仅供参考。
        if doc := await self.read(paths.TOOLS):
            parts.append(f"## Tool Notes\n\n{doc}")

        # 仅在主会话中加载 MEMORY.md（切勿在群聊中加载）
        if (not is_group_chat) and (doc := await self.read(paths.MEMORY)):
            parts.append(f"## Long-Term Memory\n\n{doc}")

        # 添加今天的内存上下文（最近两天的每日日志）

        return "\n\n---\n\n".join(parts)
