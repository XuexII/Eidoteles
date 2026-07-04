# 用于持久化工作区记忆的记忆工具。
#
# 这些工具允许智能体：
# - 搜索过去的记忆、决策和上下文
# - 在工作区中读写文件
#
# # 使用方式
#
# 在回答有关先前工作、决策、日期、人员、偏好或待办事项的问题之前，智能体应使用 `memory_search`。
#
# 使用 `memory_write` 来持久化应在多次会话中记住的重要事实。


import asyncio
import time
import logging
from pathlib import Path, PurePath
from typing import Optional, List

logger = logging.getLogger(__name__)


# ── 常量 ─────────────────────────────────────────────────────

# 推理 LLM 调用的速率限制：每分钟最多 10 次，每小时最多 60 次
REASONING_RATE_LIMIT_REQUESTS_PER_MINUTE = 10
REASONING_RATE_LIMIT_REQUESTS_PER_HOUR = 60

# 推理 LLM 调用的超时时间（防止无界等待）
REASONING_LLM_TIMEOUT_SECS = 15

# 受保护的前缀列表
_PROTECTED_PREFIXES = [".system/engine/orchestrator", "engine/orchestrator"]


# ── WorkspaceResolver ──────────────────────────────────────────────

class WorkspaceResolver:
    """为给定用户 ID 解析工作区

    在单用户模式下，始终返回相同的工作区。
    在多租户模式下，按需创建每用户工作区
    """

    async def resolve(self, user_id: str) -> "Workspace":
        """解析用户对应的工作区"""
        raise NotImplementedError


class FixedWorkspaceResolver(WorkspaceResolver):
    """无论用户 ID 如何都返回固定工作区（单用户模式）"""

    def __init__(self, workspace: "Workspace"):
        self.workspace: Workspace = workspace

    async def resolve(self, user_id: str) -> "Workspace":
        return self.workspace


# ── 路径规范化与安全检查 ─────────────────────────────────────

def normalize_workspace_path(path: str) -> Optional[str]:
    """为安全检查规范化工作区路径

    剥离 `.` 和空（`//`）段，并彻底拒绝任何 `..` 遍历组件。
    当输入包含 `..` 时返回 `None`，以便调用者可以将遍历尝试视为受保护的（或拒绝它们）

    示例：
    - `engine/./orchestrator/v3.py`      → `"engine/orchestrator/v3.py"`
    - `engine//orchestrator/v3.py`       → `"engine/orchestrator/v3.py"`
    - `engine/knowledge/../orchestrator` → `None`（拒绝 — 遍历）
    - `./foo/bar`                        → `"foo/bar"`
    """
    segments = []
    for seg in path.split('/'):
        if not seg or seg == ".":
            continue
        if seg == "..":
            return None
        segments.append(seg)
    return "/".join(segments)


def is_protected_orchestrator_path(path: str) -> bool:
    """检查路径是否控制执行循环或系统提示

    当 `ORCHESTRATOR_SELF_MODIFY` 禁用时写入被阻止。
    涵盖引擎使用的逻辑别名（`orchestrator:*`、`prompt:*`）和
    这些文档持久化的物理工作区路径。输入首先被规范化，
    因此点/双斜杠/遍历组件无法绕过守卫
    （例如 `engine/./orchestrator/v3.py` 或 `engine/knowledge/../orchestrator/v3.py`）

    遍历尝试（`..` 段）被视为受保护的 — 即使可能的目标与编排器无关，门控也会触发，
    因此调用者可以用清晰的错误拒绝写入
    """
    # 逻辑引擎别名 — 区分大小写，纯字符串匹配
    if path.startswith("orchestrator:") or path.startswith("prompt:"):
        return True

    # 物理工作区路径 — 在匹配之前规范化，以便遍历和点组件技巧无法绕过检查
    canonical = normalize_workspace_path(path)
    if canonical is None:
        # 遍历尝试（`..`）— 视为受保护，以便调用者阻止或通过批准门控路由
        return True

    # v2 引擎持久化编排器和提示覆盖文档的规范物理路径。
    # 保留当前的 `.system/engine/` 根和 #2049 之前的旧 `engine/` 根
    for prefix in _PROTECTED_PREFIXES:
        if canonical == prefix or canonical.startswith(f"{prefix}/"):
            return True
    return False


def is_protected_py_path(path: str) -> bool:
    """当规范化路径解析为编排器目录内的 `.py` 文件时返回 True。
    用于在 `MemoryWriteTool::execute()` 中门控语法验证 —
    存储级验证器仅在 `save_memory_doc` 路径（引擎文档写入）上触发，
    但 `memory_write` 直接通过 Workspace 写入，因此我们也必须在此处验证
    """
    canonical = normalize_workspace_path(path)
    if canonical is None:
        return False
    if not canonical.endswith(".py"):
        return False
    return canonical.startswith(".system/engine/orchestrator/") or canonical.startswith("engine/orchestrator/")


def looks_like_filesystem_path(path: str) -> bool:
    """检测明显是本地文件系统引用而不是工作区内存文档的路径

    示例：
    - `/Users/.../file.md`（Unix 绝对路径）
    - `C:\\Users\\...` 或 `D:/work/...`（Windows 绝对路径）
    - `~/notes.md`（Home 目录展开简写）
    """
    if not path:
        return False

    if Path(path).is_absolute() or path.startswith("~/"):
        return True

    # Windows 绝对路径检测：驱动器字母后跟 `:\` 或 `:/`
    if (len(path) >= 3
            and path[0].isalpha()
            and path[1] == ':'
            and path[2] in ('\\', '/')):
        return True

    return False


def map_write_err(error: "WorkspaceError") -> "ToolError":
    """将工作区写入错误映射为工具错误，对注入拒绝使用 `NotAuthorized`，
    以便 LLM 获得清晰的停止信号
    """
    error_str = str(error)
    if "InjectionRejected" in error_str or "injection" in error_str.lower():
        return ToolError.NotAuthorized(f"内容被拒绝: 检测到提示注入")
    return ToolError.ExecutionFailed(f"写入失败: {error}")


def self_modify_enabled() -> bool:
    """重新导出引擎的进程级 self-modify 快照，以便工具读取与
    引擎循环、存储门控和自我改进任务相同的值。
    参见 `ironclaw_engine::runtime::self_modify_enabled` 了解原理
    （单次 OnceLock 支持的快照，无法在运行时翻转）
    """
    return ironclaw_engine.runtime.self_modify_enabled()


# ── MemorySearchTool ─────────────────────────────────────────

class MemorySearchTool:
    """搜索工作区内存的工具

    在所有内存文档中执行混合搜索（FTS + 语义）。
    代理在回答有关先前工作、决策、偏好或任何历史上下文的问题之前应调用此工具
    """

    # 推理合成的系统提示 — 在实际部署中从文件加载
    MEMORY_REASONING_SYNTHESIS_PROMPT = (
        "你是一个记忆合成助手。根据提供的记忆片段，"
        "为用户的查询生成一个简洁、连贯的摘要。"
        "优先考虑最相关和最新的信息。"
    )

    def __init__(self, resolver: WorkspaceResolver):
        self.resolver: WorkspaceResolver = resolver
        self.llm: Optional["LlmProvider"] = None
        self.reasoning_enabled: bool = False
        self.reasoning_limiter: "RateLimiter" = RateLimiter()

    @classmethod
    def from_workspace(cls, workspace: "Workspace") -> "MemorySearchTool":
        """从固定工作区创建（向后兼容）"""
        return cls(resolver=FixedWorkspaceResolver(workspace))

    def with_reasoning(
            self,
            llm: Optional["LlmProvider"],
            reasoning_enabled: bool,
    ) -> "MemorySearchTool":
        """创建带有可选推理增强召回的记忆搜索工具"""
        self.llm = llm
        self.reasoning_enabled = reasoning_enabled
        return self

    def name(self) -> str:
        return "memory_search"

    def description(self) -> str:
        return (
            "搜索过去的记忆、决策和上下文。在回答有关先前工作、决策、日期、"
            "人员、偏好或待办事项的问题之前必须调用。"
            "返回带有相关性分数的相关片段。"
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询。使用自然语言描述你要查找的内容。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回的最大结果数（默认：5，最大：20）",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
                "reasoning": {
                    "type": "boolean",
                    "description": "为 true 时，使用 LLM 推理将搜索结果合成为连贯摘要。默认：由 SEARCH_REASONING_ENABLED 配置控制。",
                    "default": False,
                },
            },
            "required": ["query"],
        }

    async def execute(
            self,
            params: dict,
            ctx: "JobContext",
    ) -> "ToolOutput":
        """执行记忆搜索"""
        start = time.monotonic()

        query = _require_str(params, "query")
        limit = min(params.get("limit", 5), 20)

        workspace = await self.resolver.resolve(ctx.user_id)
        try:
            results = await workspace.search(query, limit)
        except Exception as e:
            raise ToolError.ExecutionFailed(f"搜索失败: {e}")

        result_count = len(results)
        use_reasoning = params.get("reasoning", self.reasoning_enabled)

        # 如果启用推理且 LLM 可用且有结果
        if use_reasoning and self.llm is not None and results:
            # 在发起 LLM 调用之前检查每用户速率限制
            rate_check = await self.reasoning_limiter.check_and_record(
                ctx.user_id,
                "memory_search_reasoning",
                ToolRateLimitConfig(
                    requests_per_minute=REASONING_RATE_LIMIT_REQUESTS_PER_MINUTE,
                    requests_per_hour=REASONING_RATE_LIMIT_REQUESTS_PER_HOUR,
                ),
            )
            if not rate_check.is_allowed():
                logger.debug(f"推理 LLM 调用被速率限制 (user_id={ctx.user_id})，返回原始结果")
            else:
                fragments = "\n\n".join(
                    f"[{i + 1}] (path: {r.document_path}, score: {r.score:.2f})\n{r.content}"
                    for i, r in enumerate(results)
                )

                llm_messages = [
                    ChatMessage.system(self.MEMORY_REASONING_SYNTHESIS_PROMPT),
                    ChatMessage.user(f"查询: {query}\n\n记忆片段:\n{fragments}"),
                ]

                request = CompletionRequest(llm_messages)
                request.max_tokens = 500

                try:
                    response = await asyncio.wait_for(
                        self.llm.complete(request),
                        timeout=REASONING_LLM_TIMEOUT_SECS,
                    )
                    # 记忆片段可能包含攻击者控制的文本，流经合成并返回未来的 LLM 上下文。
                    # 匹配 SessionSummaryHook 的清理
                    sanitizer = Sanitizer()
                    sanitized = sanitizer.sanitize(response.content.strip())
                    if sanitized.was_modified:
                        logger.debug(
                            f"推理合成包含可疑模式；内容已被清理 "
                            f"(user_id={ctx.user_id}, warnings={len(sanitized.warnings)})"
                        )
                    synthesis = sanitized.content

                    output = {
                        "query": query,
                        "synthesis": synthesis,
                        "results": [
                            {
                                "content": r.content,
                                "score": r.score,
                                "path": r.document_path,
                                "document_id": str(r.document_id),
                                "is_hybrid_match": r.is_hybrid(),
                            }
                            for r in results
                        ],
                        "result_count": result_count,
                        "reasoning_used": True,
                    }
                    duration_ms = int((time.monotonic() - start) * 1000)
                    return ToolOutput.success(output, duration_ms)
                except asyncio.TimeoutError:
                    logger.debug(
                        f"推理合成在 {REASONING_LLM_TIMEOUT_SECS}s 后超时，返回原始结果"
                    )
                except Exception as e:
                    logger.debug(f"推理合成失败，返回原始结果: {e}")

        # 构建不含推理的输出
        output = {
            "query": query,
            "results": [
                {
                    "content": r.content,
                    "score": r.score,
                    "path": r.document_path,
                    "document_id": str(r.document_id),
                    "is_hybrid_match": r.is_hybrid(),
                }
                for r in results
            ],
            "result_count": result_count,
        }

        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolOutput.success(output, duration_ms)

    def requires_sanitization(self) -> bool:
        # 内部记忆，受信任内容 — 不需要清理
        return False


# ── 辅助函数 ─────────────────────────────────────────────────

def _require_str(params: dict, key: str) -> str:
    """从参数字典中提取必需的字符串值"""
    value = params.get(key)
    if value is None:
        raise ToolError.InvalidParameters(f"缺少必需参数 '{key}'")
    if not isinstance(value, str):
        raise ToolError.InvalidParameters(f"参数 '{key}' 必须是字符串")
    return value