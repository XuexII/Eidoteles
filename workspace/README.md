# 工作区与记忆系统

受 [OpenClaw](https://github.com/openclaw/openclaw) 启发，工作区为智能体提供持久化记忆，采用灵活的文件系统式结构。

## 关键原则

1. **记忆是数据库，不是内存** —— 如果你想记住某件事，就明确地写下来
2. **灵活的结构** —— 创建你需要的任何目录/文件层级
3. **自我文档化** —— 使用 README.md 文件来描述目录结构
4. **混合搜索** —— 通过倒数排名融合（RRF）结合全文搜索（关键词）和向量搜索（语义）

## 文件系统结构

```
workspace/
├── README.md              <- 根目录操作手册/索引
├── MEMORY.md              <- 长期整理记忆
├── HEARTBEAT.md           <- 周期性检查清单
├── IDENTITY.md            <- 智能体名称、性质、风格
├── SOUL.md                <- 核心价值观
├── AGENTS.md              <- 行为指令
├── USER.md                <- 用户上下文
├── TOOLS.md               <- 特定环境工具说明
├── BOOTSTRAP.md           <- 首次运行仪式（引导完成后删除）
├── context/               <- 身份相关文档
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

## 使用工作区

```rust
use std::sync::Arc;
use crate::workspace::{Workspace, paths};
use ironclaw_embeddings::{create_provider, EmbeddingCacheConfig, ProviderDeps};

// 通过工厂构造嵌入提供者 —— 具体的提供者类型
// （OpenAI、NEAR AI、Ollama、Bedrock）是 crate 私有的，必须通过
// `create_provider` 访问。工厂会应用基础的纵深防御
// URL 检查（拒绝云元数据 IP 和非 http(s) 方案）
// 并在嵌入功能被禁用或配置错误时返回 `None`。完整的
// 操作员可调 SSRF 策略位于二进制文件的解析器中，位于
// `src/config/embeddings.rs::resolve_embeddings_config`，该函数在填充
// `EmbeddingsConfig` 之前会对环境驱动的 URL 调用 `validate_operator_base_url`。
let embeddings = create_provider(
    &config.embeddings,
    ProviderDeps { session, bedrock_setup: None },
).await;

let mut workspace = Workspace::new("user_123", pool);
if let Some(emb) = embeddings {
    let cache = EmbeddingCacheConfig { max_entries: 1024 };
    workspace = workspace.with_embeddings_cached(emb, cache);
}

// 对于测试：跳过缓存层并使用确定性模拟
// （由 `ironclaw_embeddings` 上的 `testing` 特性门控）。
// use ironclaw_embeddings::MockEmbeddings;
// let workspace = Workspace::new("user_123", pool)
//     .with_embeddings_uncached(Arc::new(MockEmbeddings::new(1536)));

// 读写任意路径
let doc = workspace.read("projects/alpha/notes.md").await?;
workspace.write("context/priorities.md", "# 优先级\n\n1. 功能 X").await?;
workspace.append("daily/2024-01-15.md", "完成任务 X").await?;

// 常用文件的便捷方法
workspace.append_memory("用户偏好深色模式").await?;
workspace.append_daily_log("会话笔记").await?;

// 列出目录内容
let entries = workspace.list("projects/").await?;

// 搜索（混合 FTS + 向量）
let results = workspace.search("深色模式偏好", 5).await?;

// 从身份文件获取系统提示词
let prompt = workspace.system_prompt().await?;
```

## 记忆工具

供大语言模型使用的四个工具：

- **`memory_search`** —— 混合搜索，在回答有关先前工作的问题之前必须先调用
- **`memory_write`** —— 写入任意路径（memory、daily_log 或自定义路径）
- **`memory_read`** —— 按路径读取任意文件
- **`memory_tree`** —— 以树形结构查看工作区结构（depth 参数，默认为 1）

## 混合搜索（RRF）

使用倒数排名融合结合全文搜索和向量相似度：

```
score(d) = Σ 1/(k + rank(d))（对于每种 d 出现的方法）
```

默认 k=60。两种方法的结果被合并，同时在两种方法中出现文档会获得更高的分数。

**后端差异：**
- **PostgreSQL：** 使用 `ts_rank_cd` 进行全文搜索，使用 pgvector 余弦距离进行向量搜索，完整的 RRF
- **libSQL：** 使用 FTS5 进行关键词搜索 + 通过 `libsql_vector_idx` 进行向量搜索（维度在启动时由 `ensure_vector_index()` 动态设置）

## 多作用域读取与身份隔离

当工作区具有额外的读取作用域（通过 `with_additional_read_scopes`）时，读取操作可以跨越多个用户作用域 —— 具有 `["alice", "shared"]` 作用域的用户可以从两者读取文档。

**身份文件不受多作用域读取影响。** 系统提示词从**主作用域**（`read_primary()`）读取身份和配置文件，绝不会从次级作用域读取：

| 文件 | 读取方法 | 理由 |
|------|---------|------|
| AGENTS.md | `read_primary()` | 智能体指令是每个用户独立的 |
| SOUL.md | `read_primary()` | 核心价值是每个用户独立的 |
| USER.md | `read_primary()` | 用户上下文是每个用户独立的 |
| IDENTITY.md | `read_primary()` | 身份是每个用户独立的 |
| TOOLS.md | `read_primary()` | 工具配置是每个用户独立的 |
| BOOTSTRAP.md | `read_primary()` | 引导是每个用户独立的 |
| MEMORY.md | `read()` | 共享记忆是一个特性 |
| daily/*.md | `read()` | 共享每日日志是一个特性 |

**原因：** 如果不这样做，一个具有其他作用域读取权限的用户，如果自己的副本缺失，就会静默继承该作用域的身份。智能体会错误地表现自己的身份 —— 这是一个正确性和安全性问题。

**设计规则：** 如果你希望跨用户共享身份，请在设置时向每个用户的作用域中植入相同的内容。不要依赖多作用域回退来读取身份文件。

**嵌入提供者：**
- **NEAR AI** —— 复用会话认证路径
- **OpenAI** —— 使用 `OPENAI_API_KEY`
- **Ollama** —— 本地嵌入服务器
- **AWS Bedrock** —— Titan Text Embeddings V2，使用 Bedrock 区域/配置文件认证

## 心跳系统

主动周期性执行（默认：30 分钟）：

1. 读取 `HEARTBEAT.md` 检查清单
2. 使用检查清单提示词运行智能体轮次
3. 如果有发现，通过频道发送通知
4. 如果没有，智能体回复 "HEARTBEAT_OK"（不发送通知）

```rust
use crate::agent::{HeartbeatConfig, spawn_heartbeat};

let config = HeartbeatConfig::default()
    .with_interval(Duration::from_secs(60 * 30))
    .with_notify("user_123", "telegram");

spawn_heartbeat(config, workspace, llm, response_tx);
```

## 分块策略

文档被分块以用于搜索索引：
- 默认：每块 800 个单词（英文约为 800 个令牌）
- 块间 15% 重叠以保留上下文
- 最小块大小：50 个单词（过小的尾块会合并到前一块）