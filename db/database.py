import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class Database(ABC):

    @abstractmethod
    async def get_document_by_path(self):
        pass


