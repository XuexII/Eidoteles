from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID, uuid4
import logging
from typing import Optional, List, Dict, Any, Set
from datetime import datetime

logger = logging.getLogger(__name__)

class Session(BaseModel):
    # session id
    id: UUID = Field(default_factory=uuid4)
    # 拥有此会话的用户 ID。
    user_id: str
    # 激活的线程id
    active_thread: Optional[UUID] = None
    # session中的所有线程
    threads: Dict[UUID, Thread] = {}
    created_at: datetime = Field(default_factory=datetime.now)
    # 最后激活时间
    last_active_at: datetime = Field(default_factory=datetime.now)
    # 任意 JSON 兼容的数据
    metadata: Any = None
    # 已为此会话自动批准的工具（“始终批准”）。
    auto_approved_tools: Set[str] = Field(default_factory=set)



class ThreadState:
    pass
