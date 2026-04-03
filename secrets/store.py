from abc import ABC, abstractmethod
from typing import Optional

class SecretsStore(ABC):
    """
    密钥存储接口，对应 Rust trait SecretsStore。
    实现类需提供线程安全的密钥存取方法（Python 对象默认引用计数，无需显式 Send+Sync）。
    """
    pass