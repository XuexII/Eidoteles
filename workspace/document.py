from pydantic import BaseModel, Field
from dataclasses import dataclass
import logging
from typing import List, Dict, Optional, Any
from uuid import uuid4
from datetime import datetime

logger = logging.getLogger(__name__)

@dataclass
class paths:
    # 长期整理后的记忆。
    MEMORY: str = "MEMORY.md"
    # 代理人身份（姓名、性格、气质）。
    IDENTITY: str = "IDENTITY.md"
    # 核心价值观和原则。
    SOUL: str = "SOUL.md"
    # 行为指导。
    AGENTS: str = "AGENTS.md"
    # 用户信息（姓名、偏好）。
    USER: str = "USER.md"
    # 心跳定期检查清单。
    HEARTBEAT: str = "HEARTBEAT.md"
    # 根目录 runbook/readme。
    README: str = "README.md"
    # 每日日志目录。
    DAILY_DIR: str = "daily/"
    # 上下文目录（用于存放与身份相关的文档）。
    CONTEXT_DIR: str = "context/"
    # 用户可编辑的针对特定环境的工具使用指南注释。
    TOOLS: str = "TOOLS.md"
    # 首次运行的仪式文件会在引导完成后自动删除。
    BOOTSTRAP: str = "BOOTSTRAP.md"


class MemoryDocument(BaseModel):
    id: str = Field(default_factory=lambda : str(uuid4()))
    user_id: str
    agent_id: Optional[str] = None
    # 工作区内的文件路径（例如 "context/vision.md"）。
    path: str
    # 完整的文档内容。
    content: str
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    metadata: Any