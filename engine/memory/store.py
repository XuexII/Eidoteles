# 项目范围内的记忆文档操作。

import logging
from dataclasses import dataclass
from typing import Optional, List

from ..traits.store import Store
from ..types.memory import DocId, DocType, MemoryDoc
from ..types.project import ProjectId
from ..types.thread import ThreadId

logger = logging.getLogger(__name__)


@dataclass
class MemoryStore:
    """对 [`Store`] trait 的薄包装，用于项目范围的文档操作"""
    store: Store

    async def create_doc(
            self,
            project_id: ProjectId,
            user_id: str,
            doc_type: DocType,
            title: str,
            content: str,
    ) -> MemoryDoc:
        """创建新的记忆文档"""
        doc = MemoryDoc.new(project_id, user_id, doc_type, title, content)
        await self.store.save_memory_doc(doc)
        return doc

    async def create_doc_from_thread(
            self,
            project_id: ProjectId,
            user_id: str,
            doc_type: DocType,
            title: str,
            content: str,
            source_thread_id: ThreadId,
    ) -> MemoryDoc:
        """创建链接到源线程的文档"""
        doc = MemoryDoc.new(project_id, user_id, doc_type, title, content)
        doc = doc.with_source_thread(source_thread_id)
        await self.store.save_memory_doc(doc)
        return doc

    async def get_doc(self, id: DocId) -> Optional[MemoryDoc]:
        """按 ID 加载单个文档"""
        return await self.store.load_memory_doc(id)

    async def list_docs(
            self,
            project_id: ProjectId,
            user_id: str,
            doc_type: Optional[DocType] = None,
    ) -> List[MemoryDoc]:
        """列出项目中的所有文档，可选按类型过滤"""
        all_docs = await self.store.list_memory_docs(project_id, user_id)
        if doc_type is not None:
            return [d for d in all_docs if d.doc_type == doc_type]
        return all_docs
