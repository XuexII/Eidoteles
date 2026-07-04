# 大语言模型提供商的运行时热重载支持。
#
# 当大语言模型设置发生变化时，核心提供商链会从配置重新构建。
# [`SwappableLlmProvider`] 包装了 `Arc<dyn LlmProvider>`，使得外层句柄在重建过程中保持稳定，应用程序的其他部分无需重新订阅。
# [`LlmReloadHandle`] 将主提供商和廉价提供商绑定在一起，并对重叠的重载进行序列化。
#
# ## 设计说明
#
# - **单一快照锁。** 所有缓存的元数据（`model_name`、`active_model_name`、成本、缓存乘数以及内部提供商本身）都存放在一个 `RwLock<ProviderSnapshot>` 中。读取者始终观察到一致的一个提供商的快照——绝不会在交换后看到新旧混合的状态。
# - **无无限泄漏。** `model_name()` 返回 `&'static str`，因为特质要求如此；我们通过全局的 `Mutex<HashMap>` 对每个不同的名称进行字符串驻留，因此泄漏量受限于进程所见的不同模型名称集合（通常只有几个）。
# - **`set_model()` 是易失性的。** 运行时的模型切换仅转发给当前的内部提供商。下一次成功的 [`LlmReloadHandle::reload`] 会从配置重建链并丢弃覆盖设置。依赖模型覆盖的调用者必须通过常规的设置路径来持久化它。



import asyncio
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Dict, Any, List
import logging

logger = logging.getLogger(__name__)


# ── 常量 ─────────────────────────────────────────────────────

# 进程生命周期内驻留的不同模型名称的最大数量。
# 有界以防止如果 `set_model` 被恶意或格式错误的输入
# （例如 LLM 工具调用输出）调用时 `Box::leak` 无限增长
INTERN_MAX_ENTRIES = 1024

# 单个驻留模型名称的最大长度（字节）。任何更长的内容被视为格式错误 —
# 真实模型标识符远低于此值
# （GPT-4o: 6 字节，`anthropic.claude-opus-4-6-v1`: ~28 字节）
INTERN_MAX_LEN = 256

# 当名称超过长度限制或不同条目上限填满时使用的回退驻留字符串。
# 选择为在日志中明显错误，以便操作员注意到回退而不是静默地错误归因成本
INTERN_OVERFLOW_SENTINEL = "<model-name-overflow>"

# 进程级模型名称驻留器
_intern_lock = threading.Lock()
_intern_map: Dict[str, str] = {}


# ── 模型名称驻留 ─────────────────────────────────────────────

def intern_model_name(name: str) -> str:
    """驻留模型名称字符串，使其可以通过 trait 的 `fn model_name(&self) -> &str` 契约返回，
    而不会在每次交换时泄漏

    泄漏通过两种方式有界：通过 [`INTERN_MAX_LEN`] 的每条目限制和通过
    [`INTERN_MAX_ENTRIES`] 的总数限制。达到任一上限时以 `warning` 级别记录日志，
    并返回静态哨兵，因此进程不能被强制通过重复使用新字符串调用 `set_model` 实现无界内存增长
    """
    return intern_into(name, INTERN_MAX_ENTRIES, INTERN_MAX_LEN)


def intern_into(name: str, max_entries: int, max_len: int) -> str:
    """[`intern_model_name`] 的无锁核心，拆分出来以便上限逻辑可以针对本地字典进行单元测试，
    而不会污染其他测试读取的进程级驻留器
    """
    if len(name.encode('utf-8')) > max_len:
        logger.warning(
            f"模型名称超过驻留器长度限制 (len={len(name)}, max={max_len})；使用溢出哨兵"
        )
        return INTERN_OVERFLOW_SENTINEL

    with _intern_lock:
        if name in _intern_map:
            return _intern_map[name]

        if len(_intern_map) >= max_entries:
            logger.warning(
                f"模型名称驻留器已满 (entries={len(_intern_map)}, max={max_entries})；使用溢出哨兵"
            )
            return INTERN_OVERFLOW_SENTINEL

        _intern_map[name] = name
        return name


# ── 提供者快照 ───────────────────────────────────────────────

@dataclass
class ProviderSnapshot:
    """提供者的缓存快照，包含所有同步可访问的元数据"""
    inner: Any  # LlmProvider
    model_name: str
    active_model_name: str
    cost_per_token: Tuple[Decimal, Decimal]
    cache_write_multiplier: Decimal
    cache_read_discount: Decimal

    @classmethod
    def capture(cls, provider: Any) -> "ProviderSnapshot":
        """捕获提供者的当前快照"""
        model_name = intern_model_name(provider.model_name())
        active_model_name = provider.active_model_name()
        cost_per_token = provider.cost_per_token()
        cache_write_multiplier = provider.cache_write_multiplier()
        cache_read_discount = provider.cache_read_discount()
        return cls(
            inner=provider,
            model_name=model_name,
            active_model_name=active_model_name,
            cost_per_token=cost_per_token,
            cache_write_multiplier=cache_write_multiplier,
            cache_read_discount=cache_read_discount,
        )

    def __repr__(self) -> str:
        return (
            f"ProviderSnapshot(model_name={self.model_name!r}, "
            f"active_model_name={self.active_model_name!r}, ...)"
        )


# ── 可交换 LLM 提供者 ───────────────────────────────────────

class SwappableLlmProvider:
    """一个提供者包装器，其内部提供者可以在运行时交换

    保证模块级文档中描述的不变量
    """

    def __init__(self, inner: Any):  # LlmProvider
        self._state = ProviderSnapshot.capture(inner)
        self._lock = threading.RLock()

    def swap(self, inner: Any) -> None:
        """用新构建的提供者替换内部提供者链。
        元数据在同一临界区内原子刷新
        """
        with self._lock:
            self._state = ProviderSnapshot.capture(inner)

    def _current(self) -> Any:
        """获取当前内部提供者"""
        with self._lock:
            return self._state.inner

    def model_name(self) -> str:
        """获取模型名称"""
        with self._lock:
            return self._state.model_name

    def cost_per_token(self) -> Tuple[Decimal, Decimal]:
        """获取每 token 成本"""
        with self._lock:
            return self._state.cost_per_token

    def cache_write_multiplier(self) -> Decimal:
        """获取缓存写入乘数"""
        with self._lock:
            return self._state.cache_write_multiplier

    def cache_read_discount(self) -> Decimal:
        """获取缓存读取折扣"""
        with self._lock:
            return self._state.cache_read_discount

    def active_model_name(self) -> str:
        """获取活跃模型名称"""
        with self._lock:
            return self._state.active_model_name

    def set_model(self, model: str) -> None:
        """设置模型。在委托调用和快照刷新期间持有写锁，
        以便并发的 `swap()` 不能用从旧提供者捕获的快照覆盖刚更新的内部提供者。
        内部 `set_model` 实现是同步的（无 `.await`），因此在调用期间持有锁是安全的
        """
        with self._lock:
            self._state.inner.set_model(model)
            self._state = ProviderSnapshot.capture(self._state.inner)

    async def complete(self, request: Any) -> Any:
        """完成请求"""
        return await self._current().complete(request)

    async def complete_with_tools(self, request: Any) -> Any:
        """带工具完成请求"""
        return await self._current().complete_with_tools(request)

    async def list_models(self) -> List[str]:
        """列出模型"""
        return await self._current().list_models()

    async def model_metadata(self) -> Any:
        """获取模型元数据"""
        return await self._current().model_metadata()

    def effective_model_name(self, requested_model: Optional[str] = None) -> str:
        """获取有效模型名称"""
        return self._current().effective_model_name(requested_model)

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"SwappableLlmProvider(model_name={self._state.model_name!r}, "
                f"active_model_name={self._state.active_model_name!r}, ...)"
            )


# ── LLM 重载句柄 ────────────────────────────────────────────

class LlmReloadHandle:
    """主要/廉价提供者链的稳定热重载句柄

    持有启动时创建的两个 [`SwappableLlmProvider`] 包装器，
    并通过内部互斥锁序列化并发重载，以便快速设置更改不会触发重叠的链重建
    （这将重做潜在的昂贵工作，如 OAuth 刷新和 HTTP 探测）
    """

    def __init__(
            self,
            primary: SwappableLlmProvider,
            cheap: Optional[SwappableLlmProvider] = None,
    ):
        self._primary = primary
        self._cheap = cheap
        # 序列化并发的 `reload()` 调用，以便快速设置切换不会触发重叠的链重建
        # （每次重建可能涉及 OAuth 刷新和 HTTP 探测；让它们堆积会浪费上游配额
        # 并使包装器短暂指向半构建的链）
        self._reload_lock = asyncio.Lock()

    def primary_provider(self) -> Any:
        """获取主要提供者"""
        return self._primary

    def cheap_provider(self) -> Optional[Any]:
        """获取廉价提供者"""
        return self._cheap

    async def reload(self, config: Any, session: Any) -> None:
        """从 `config` 重建提供者链，并原子替换主要（和廉价的，如果存在）包装器的内部提供者

        重载被序列化，因此两个并发调用者不能竞争
        """
        async with self._reload_lock:
            components = await build_provider_chain_components(config, session)

            self._primary.swap(components.primary)

            if self._cheap is not None:
                new_cheap = components.cheap if components.cheap is not None else self._primary
                self._cheap.swap(new_cheap)
            elif components.cheap is not None:
                # 不对称：启动时未分配廉价包装器，因此新配置的廉价模型
                # 无法通过热重载激活。通过追踪显示此信息，
                # 以便操作员不会认为交换静默生效
                logger.warning(
                    "llm 热重载: 廉价提供者现已配置但在启动时未配置；"
                    "它仅在完全重启后生效"
                )