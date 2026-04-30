from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

class AgentConfig(BaseModel):
    name: str