from enum import Enum
from typing import Type


class ClassEnum(Enum):
    def __new__(cls, value: Type):
        # 检查 value 是否为类（包括内置类型、自定义类等）
        if not isinstance(value, type):
            raise TypeError(f"Enum value must be a class, got {type(value).__name__}")

        obj = object.__new__(cls)  # 不调用 Enum.__new__
        obj._value_ = value
        return obj

    def __call__(self, *args, **kwargs):
        """
        初始化类对象
        """
        cls = self.value
        return cls(*args, **kwargs)
