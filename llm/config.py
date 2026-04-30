from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict
from pathlib import Path
from llm.session import SessionConfig
import logging

logger = logging.getLogger(__name__)

class LlmConfig(BaseModel):

    # (e.g., "nearai", "openai", "groq", "tinfoil")
    backend: str
    # 会话管理器的配置（认证 URL、令牌持久化路径）。
    # 由 NearAI 提供商用于 OAuth/会话令牌认证。
    session: SessionConfig