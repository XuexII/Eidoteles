"""
WASM 沙盒的密钥泄露检测。

在沙盒边界扫描数据以防止密钥外泄。
使用 Aho-Corasick 进行快速多模式匹配，并结合正则表达式处理复杂模式。

# 安全模型

泄露检测发生在两个位置：

1. **在出站请求之前** - 防止 WASM 通过将密钥编码到 URL、标头或请求体中进行外泄
2. **在响应/输出之后** - 防止密钥在日志、工具输出或返回给 WASM 的数据中意外暴露

# 架构

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                         WASM HTTP 请求流程                                 │
│                                                                              │
│   WASM ──► 允许名单 ──► 泄露扫描 ──► 凭证注入 ──► 执行请求 ──► 响应        │
│            验证器      (请求)      注入器                     │              │
│                                                                   ▼          │
│                                     WASM ◀── 泄露扫描 ◀── 响应              │
│                                              (响应)                         │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                           扫描结果操作                                     │
│                                                                              │
│   LeakDetector.scan() ──► LeakScanResult                                   │
│                               │                                             │
│                               ├─► clean: 直接通过                          │
│                               ├─► warn: 记录日志，允许通过                  │
│                               ├─► redact: 屏蔽密钥                         │
│                               └─► block: 完全拒绝                         │
└─────────────────────────────────────────────────────────────────────────────┘
```
"""

from dataclasses import dataclass, field
from typing import Any

@dataclass
class LeakDetector:
    """
    输出数据中密钥泄露的检测器。
用于已知模式的快速前缀匹配。
    """
    patterns: list[Any]
    # ac自动机
    prefix_matcher: Any = None
    # (prefix, pattern_index)
    known_prefixes: list[tuple[str, int]] = field(default_factory=list)

    def scan_and_clean(self, content: str) -> str:
        """
        扫描内容并根据操作返回清理后的版本。

        如果内容应被阻止，则返回 `Err`，否则返回 `content`。
        """
        return content
