from enum import Enum
from typing import Type

class ClassEnum(Enum):
    def __new__(cls, value: Type):
        # 检查 value 是否为类（包括内置类型、自定义类等）
        if not isinstance(value, type):
            raise TypeError(f"Enum value must be a class, got {type(value).__name__}")
        # 调用父类 Enum 的 __new__ 创建枚举成员
        obj = super().__new__(cls, value)
        return obj

    def __call__(self, *args, **kwargs):
        """
        初始化类对象
        """
        cls = self.value
        return cls(*args, **kwargs)
