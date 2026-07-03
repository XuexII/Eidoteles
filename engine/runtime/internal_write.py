# 受信内部写入作用域标记。
#
# 某些 Store 实现会对安全敏感文档（编排器代码、提示覆盖层）的 `save_memory_doc` 操作进行门控限制。
# 该门控会豁免*系统内部*写入——例如在项目引导期间植入编译好的编排器 v0——使其不受大语言模型写入规则的约束。
#
# 仅凭文档内容来区分“系统内部”与“大语言模型编写”是不安全的：大语言模型工具调用可以构造一个包含门控所检查的任何标记的有效载荷。
# 相反，受信写入的调用点在调用 `save_memory_doc` 之前会进入此任务本地作用域，而 Store 实现会读取 `is_trusted_internal_write_active()`。
# 该标志限定于当前异步任务，并且**不会**跨 `tokio::spawn` 传播，因此不可信代码无法继承它。
#
# ## 用法
#
# ```ignore
# use ironclaw_engine::runtime::internal_write::with_trusted_internal_writes;
#
# with_trusted_internal_writes(async {
#     store.save_memory_doc(&seed_doc).await?;
#     Ok(())
# }).await
# ```
#
# 在闭包内部，存储的门控会看到 `is_trusted_internal_write_active() == true`，
# 并允许写入，即使自我修改功能在其他情况下被禁用。


import os
import threading
from contextlib import contextmanager
from typing import Optional, Generator

# ── 受信任内部写入 ───────────────────────────────────────────

# 当前异步任务是否在 `with_trusted_internal_writes` 作用域内的线程局部变量
_trusted_internal_write = threading.local()


@contextmanager
def with_trusted_internal_writes() -> Generator[None, None, None]:
    """在设置了受信任内部写入标志的情况下运行代码块"""
    old_value = getattr(_trusted_internal_write, 'value', False)
    _trusted_internal_write.value = True
    try:
        yield
    finally:
        _trusted_internal_write.value = old_value


def is_trusted_internal_write_active() -> bool:
    """如果当前异步任务在 `with_trusted_internal_writes` 作用域内，则返回 True"""
    return getattr(_trusted_internal_write, 'value', False)


# ── 自我修改开关 ────────────────────────────────────────────

# 在首次调用时每个进程读取一次的 `ORCHESTRATOR_SELF_MODIFY` 快照。
# 在每次门控检查时读取环境变量是脆弱的：环境变量是全局可变状态，
# 未来的沙箱逃逸（或行为异常的进程内调用者）可能在执行中途翻转安全门控。
# 此缓存捕获首次查询时的值，并在进程生命周期的其余时间返回相同的答案，
# 因此所有调用者 — 引擎循环、自我改进任务和主机存储 — 看到一致的标志
_self_modify_cache: Optional[bool] = None
_self_modify_lock = threading.Lock()

# 仅在调试/测试构建中，测试可以通过 `set_self_modify_for_test()` 覆盖快照
# 以练习两个代码路径。覆盖路径在发布构建中被编译排除，
# 因此生产代码在任何情况下都不能在运行时翻转门控
_self_modify_test_override: Optional[bool] = None
_self_modify_test_lock = threading.Lock()
# 用于测试序列化的互斥锁
_self_modify_test_serializer = threading.Lock()


def self_modify_enabled() -> bool:
    """返回是否启用了编排器自我修改"""
    # 仅在调试构建中检查测试覆盖
    if __debug__:
        with _self_modify_test_lock:
            if _self_modify_test_override is not None:
                return _self_modify_test_override

    with _self_modify_lock:
        global _self_modify_cache
        if _self_modify_cache is None:
            val = os.environ.get("ORCHESTRATOR_SELF_MODIFY", "")
            _self_modify_cache = val in ("true", "1")
        return _self_modify_cache


def set_self_modify_for_test(value: Optional[bool]) -> None:
    """仅测试用的 `self_modify_enabled()` 覆盖。

    生产代码从由 `ORCHESTRATOR_SELF_MODIFY` 播种的进程级缓存读取值。
    测试否则将无法翻转标志（缓存永久锁定第一个读者的视图），
    因此此辅助函数提供优先的单独覆盖层。传递 `None` 以清除

    在发布构建中不执行任何操作
    """
    if __debug__:
        with _self_modify_test_lock:
            global _self_modify_test_override
            _self_modify_test_override = value


class SelfModifyTestGuard:
    """`self_modify_enabled()` 的作用域覆盖，在释放时恢复先前的值，
    即使测试发生 panic。强烈优先于裸设置器

    测试序列化：此守卫持有互斥锁用于其生命周期，
    因此并发测试线程排队而不是竞争。
    不涉及自我修改的测试不付出任何成本；
    涉及自我修改的测试一次运行一个，这是可接受的，因为此类测试很少
    """

    def __init__(self, value: bool):
        if __debug__:
            self._serializer = _self_modify_test_serializer
            self._serializer.acquire()
            self._previous = _self_modify_test_override
            set_self_modify_for_test(value)
        else:
            self._serializer = None
            self._previous = None

    @classmethod
    def enable(cls) -> "SelfModifyTestGuard":
        """启用自我修改的测试守卫"""
        return cls(True)

    @classmethod
    def disable(cls) -> "SelfModifyTestGuard":
        """禁用自我修改的测试守卫"""
        return cls(False)

    def __del__(self):
        """释放守卫时恢复先前的值"""
        if __debug__:
            set_self_modify_for_test(self._previous)
            if self._serializer is not None:
                self._serializer.release()