from aiorwlock import RWLock, _ReaderLock, _WriterLock
from typing import List, Dict, Set, Tuple, Union, Optional


def with_rwlock(cls):
    class RWLocked(cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._lock = RWLock()   # 暂不创建

        @property
        def read(self) -> _ReaderLock:
            return self._lock.reader_lock

        @property
        def write(self) -> _WriterLock:
            return self._lock.writer_lock

    return RWLocked


@with_rwlock
class RWLockDict(dict):

    def __init__(self):
        super().__init__()


@with_rwlock
class RWLockSet(set):

    def __init__(self):
        super().__init__()

