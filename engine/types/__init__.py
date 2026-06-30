"""
引擎的核心类型定义。
所有数据结构都位于此处。不包含异步、不包含 I/O——仅包含类型和验证逻辑。

- capability.py
    - 表示权限和能力单元
- conversation.py
    - 对话界面——UI 层，与执行层分离
- error.py
    - 定义引擎错误类型
- event.py
     - 用于事件溯源
- memory.py
    - 表示持久化知识对象
- message.py
    - 表示线程中的消息
- mission.py
    - 表示长期目标和触发器
- project.py
    - 表示上下文容器，组织相关的线程、任务和内存文档
- provenance.py
    - 用于数据来源追踪
- step.py
    - 表示单次执行轮次
- thread.py
    - 表示有状态执行单元
"""

from dataclasses import dataclass
from enum import Enum

# ── 常量 ─────────────────────────────────────────────────────

LEGACY_SHARED_OWNER_ID: str = "system"
SHARED_OWNER_ID: str = "__shared__"


# ── 所有者 ID ────────────────────────────────────────────────

class OwnerIdType(Enum):
    # 系统共享的资源
    Shared = "Shared"
    # 私有用户资源
    User = "User"


@dataclass
class OwnerId:
    """所有者标识符，可以是共享的或属于特定用户的"""
    # 所有者类型
    type: OwnerIdType
    user_id: str | None = None

    @classmethod
    def from_user_id(cls, user_id: str) -> "OwnerId":
        """从用户 ID 创建 OwnerId"""
        if is_shared_owner(user_id):
            return cls(OwnerIdType.Shared)
        else:
            return cls(OwnerIdType.User, user_id)

    @property
    def is_shared(self) -> bool:
        """是否为共享所有者"""
        return self.type == OwnerIdType.Shared

    def matches_user(self, user_id: str) -> bool:
        """检查是否匹配指定用户"""
        return self.type == OwnerIdType.User and self.user_id == user_id

    def as_user_id(self) -> str:
        """以字符串形式返回用户 ID"""
        if self.type == OwnerIdType.Shared:
            return shared_owner_id()
        else:
            return self.user_id


# ── 辅助函数 ─────────────────────────────────────────────────

def default_user_id() -> str:
    """为添加多租户隔离之前创建的记录的向后兼容反序列化提供默认 user_id"""
    return "legacy"


def shared_owner_id() -> str:
    """返回共享所有者 ID"""
    return SHARED_OWNER_ID


def slugify_simple(name: str) -> str:
    """简单的 slug 派生：小写，将非字母数字的连续字符替换为单个破折号，
    去除首尾破折号

    用于从人类可读的项目名称派生 `projects/<slug>/` 的目录段。
    必须是纯函数（无 UUID，无随机性），以便 `slugify_simple(name)`
    是工作区布局的可靠逆向
    """
    out = []
    prev_dash = True  # 将开头视为破折号之后，以便合并前导破折号

    for c in name.lower():
        if c.isascii() and c.isalnum():
            out.append(c)
            prev_dash = False
        elif not prev_dash:
            out.append('-')
            prev_dash = True

    result = ''.join(out).rstrip('-')
    return result


def is_shared_owner(user_id: str) -> bool:
    """检查用户 ID 是否为共享所有者"""
    return user_id == SHARED_OWNER_ID or user_id == LEGACY_SHARED_OWNER_ID


def shared_owner_candidates() -> list:
    """返回共享所有者候选列表"""
    return [SHARED_OWNER_ID, LEGACY_SHARED_OWNER_ID]
