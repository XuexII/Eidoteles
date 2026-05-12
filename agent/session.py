from datetime import datetime

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Union, Any, Set
import logging
from uuid import uuid4

logger = logging.getLogger(__name__)

class Thread(BaseModel):
    id: str = Field(default_factory=lambda _: str(uuid4()))
    # 所属的session id
    session_id: str

class Session(BaseModel):
    # session id
    id: str = Field(default_factory=lambda _: str(uuid4()))
    user_id: str
    active_thread: Optional[str] = None
    threads: Dict[str, Thread] = Field(default_factory=dict)
    created_at:datetime = Field(default_factory=datetime.now)
    # session最后一次激活的时间
    last_active_at: datetime = Field(default_factory=datetime.now)
    metadata: Dict = Field(default_factory=dict)
    # 已为此会话自动批准的工具（“始终批准”）。
    auto_approved_tools: Set[str] = Field(default_factory=set)

    def create_thread(self) -> Thread:
        """
        在这个session中创建一个新的线程
        """
        thread = Thread(session_id=self.id)
        thread_id = thread.id
        self.active_thread = thread_id
        self.last_active_at = datetime.now()
        if thread_id not in self.threads:
            self.threads[thread_id] = thread
            # 如果新插入，返回新线程
            return thread
        else:
            # 如果已存在，返回已存在的线程
            return self.threads[thread_id]