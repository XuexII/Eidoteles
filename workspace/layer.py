import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple
from abc import ABC, abstractmethod
from enum import Enum, auto

logger = logging.getLogger(__name__)


class LayerSensitivity(Enum):
    Private = auto()
    Shared = auto()

@dataclass
class MemoryLayer:
    # scope：对应的数据库 user_id（层的实际存储位置）
    scope: str
    # name：层名称（如 "private"、"household"、"finance"）
    name: str = "private"
    # writable：是否可写
    writable: bool = True
    # sensitivity：Private（私密）或 Shared（共享）
    sensitivity: LayerSensitivity = LayerSensitivity.Private
