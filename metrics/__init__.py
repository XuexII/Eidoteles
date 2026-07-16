from dataclasses import dataclass


# ── Token 使用量 ─────────────────────────────────────────────

@dataclass
class TokenUsage:
    """单次 LLM 调用的 token 使用量"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # 此次调用的美元成本（如果成本数据可用，由 LlmBackend 填充）
    cost_usd: float = 0.0

    @property
    def total(self) -> int:
        """返回总的 token 使用量"""
        return self.input_tokens + self.output_tokens
