import aiorwlock
from typing import List, Dict, Set, Tuple, Union, Optional


class RWLockDict(dict):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = aiorwlock.RWLock()
