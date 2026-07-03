# 线程树——父子关系追踪。

from engine.types.thread import ThreadId

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ThreadTree:
    """管理父子线程关系

    简单的内存树。线程形成一个森林（多个根）
    """
    # child → parent
    parents: Dict[ThreadId, ThreadId] = field(default_factory=dict)
    # parent → children（按插入顺序排列）
    children: Dict[ThreadId, List[ThreadId]] = field(default_factory=dict)

    def add_child(self, parent_id: ThreadId, child_id: ThreadId) -> None:
        """注册父子关系"""
        self.parents[child_id] = parent_id
        if parent_id not in self.children:
            self.children[parent_id] = []
        self.children[parent_id].append(child_id)

    def parent_of(self, thread_id: ThreadId) -> Optional[ThreadId]:
        """获取线程的父线程（如果有）"""
        return self.parents.get(thread_id)

    def children_of(self, thread_id: ThreadId) -> List[ThreadId]:
        """获取线程的子线程"""
        return self.children.get(thread_id, [])

    def ancestors(self, thread_id: ThreadId) -> List[ThreadId]:
        """向上遍历树以收集所有祖先（父、祖父...）"""
        result = []
        current = thread_id
        while current in self.parents:
            parent = self.parents[current]
            result.append(parent)
            current = parent
        return result

    def remove(self, thread_id: ThreadId) -> None:
        """从树中移除线程。不移除其子线程"""
        if thread_id in self.parents:
            parent = self.parents.pop(thread_id)
            if parent in self.children:
                siblings = self.children[parent]
                if thread_id in siblings:
                    siblings.remove(thread_id)
        # 孤立任何子线程（它们的 parent_id 条目变为过时）
        self.children.pop(thread_id, None)

    def is_root(self, thread_id: ThreadId) -> bool:
        """检查线程是否为根（无父线程）"""
        return thread_id not in self.parents