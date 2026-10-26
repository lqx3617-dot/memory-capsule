"""记忆胶囊 —— brain_memory 的中文别名入口。

    import 记忆胶囊 as bm
    db = bm.MemoryDB("mem.db")

等价于 import brain_memory as bm。包本身仍是 brain_memory：
pip 分发名、目录名、环境变量前缀（BRAIN_MEMORY_*）都不变，只是多一个中文入口。
"""
from __future__ import annotations

from brain_memory import *          # noqa: F401,F403
from brain_memory import (          # noqa: F401
    __all__,
    __display_name__,
    __version__,
)
