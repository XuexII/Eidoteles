from dataclasses import dataclass, field
from typing import List, Dict, Optional
from uuid import UUID
from channels.channel import Channel, IncomingMessage, MessageStream
import asyncio
import logging
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

class HttpConfig(BaseModel):
    host: str
    port: int
    webhook_secret: Optional[str] = None
    user_id: str