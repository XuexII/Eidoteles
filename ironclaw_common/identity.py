from dataclasses import dataclass
from typing import Optional

# ── 常量 ─────────────────────────────────────────────────────

# [`ExternalThreadId`] 的最大长度，以字节为单位。
# 选择为适应 Slack 的复合 `thread_ts` 标识符、Web UI 生成的 UUID 字符串、
# Telegram 聊天 ID 以及类似频道特定的线程令牌，
# 同时仍然限制我们从外部系统接受的内容
MAX_EXTERNAL_THREAD_ID_LEN = 512


# ── 外部线程 ID 错误 ────────────────────────────────────────

class ExternalThreadIdError(Exception):
    """候选字符串不是有效的外部线程 ID 的原因"""

    @classmethod
    def Empty(cls) -> "ExternalThreadIdError":
        """外部线程 ID 不能为空"""
        return cls("外部线程 ID 不能为空")

    @classmethod
    def TooLong(cls) -> "ExternalThreadIdError":
        """外部线程 ID 超过最大字节数"""
        return cls(f"外部线程 ID 超过 {MAX_EXTERNAL_THREAD_ID_LEN} 字节")

    @classmethod
    def ContainsNul(cls) -> "ExternalThreadIdError":
        """外部线程 ID 不能包含 NUL 字节"""
        return cls("外部线程 ID 不能包含 NUL 字节")


# ── 外部线程 ID ─────────────────────────────────────────────

@dataclass(frozen=True)
class ExternalThreadId:
    """外部（频道提供的）线程标识符 — 例如 Telegram 聊天 ID、Slack `thread_ts`、
    Web UI 生成的 UUID 字符串

    **不是** 内部引擎 `ThreadId(Uuid)`。频道提供其平台使用的任何形状；
    [`ExternalThreadId`] 是类型化的边界表示，安全地跨内部模块边界携带该原始字符串。
    到内部 UUID 的转换在 `SessionManager::resolve_thread` 及等效方法中发生
    """
    _value: str

    def __post_init__(self):
        """验证构造参数"""
        if not self._value:
            raise ExternalThreadIdError.Empty()
        if len(self._value.encode('utf-8')) > MAX_EXTERNAL_THREAD_ID_LEN:
            raise ExternalThreadIdError.TooLong()
        if '\0' in self._value:
            raise ExternalThreadIdError.ContainsNul()

    @classmethod
    def new(cls, raw: str) -> "ExternalThreadId":
        """从任何类字符串值构造，验证长度并禁止 NUL 字节。
        失败时返回 [`ExternalThreadIdError`]。
        长度以字节为单位通过 `str.encode('utf-8')` 的长度测量

        Args:
            raw: 原始字符串值

        Returns:
            验证后的 ExternalThreadId

        Raises:
            ExternalThreadIdError: 当值无效时
        """
        return cls(raw)

    @classmethod
    def validate(cls, s: str) -> None:
        """在不构造的情况下验证候选字符串。
        由 `new`（分配）和 `TryFrom<String>`（消耗拥有的 String 而不重新分配）共享。
        长度以字节为单位通过 `str.encode('utf-8')` 的长度测量

        Args:
            s: 要验证的字符串

        Raises:
            ExternalThreadIdError: 当值无效时
        """
        if not s:
            raise ExternalThreadIdError.Empty()
        if len(s.encode('utf-8')) > MAX_EXTERNAL_THREAD_ID_LEN:
            raise ExternalThreadIdError.TooLong()
        if '\0' in s:
            raise ExternalThreadIdError.ContainsNul()

    @classmethod
    def from_trusted(cls, raw: str) -> "ExternalThreadId":
        """无验证构造。
        用于来自调用者已信任的类型化上游的值 — 数据库行、持久化的挂起门控负载、
        或线协议早于新类型的 `serde(transparent)` 反序列化。
        对于接触外部输入的任何内容优先使用 [`new`]

        Args:
            raw: 受信任的字符串值

        Returns:
            ExternalThreadId 实例
        """
        # 使用 object.__setattr__ 绕过 frozen dataclass 的限制
        instance = object.__new__(cls)
        object.__setattr__(instance, '_value', raw)
        return instance

    def as_str(self) -> str:
        """借用内部字符串"""
        return self._value

    def into_inner(self) -> str:
        """消耗并返回内部 `str`"""
        return self._value

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"ExternalThreadId({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ExternalThreadId):
            return self._value == other._value
        if isinstance(other, str):
            return self._value == other
        return False

    def __hash__(self) -> int:
        return hash(self._value)

    def __lt__(self, other: "ExternalThreadId") -> bool:
        return self._value < other._value

    def __le__(self, other: "ExternalThreadId") -> bool:
        return self._value <= other._value
