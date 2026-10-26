"""记忆胶囊（brain_memory）—— 可嵌入的长期记忆库。

Phase 1 完成：errors / config / types / locks / db / schema / gate / promote / recall / forget / dream(桩)。
"""
from __future__ import annotations

from .config import MemoryConfig
from .db import MemoryDB
from .errors import (
    BrainMemoryError,
    ClosedError,
    ConfigError,
    DatabaseError,
    EmbeddingDimensionError,
    ExtensionError,
    LockOrderError,
    LockTimeoutError,
    SchemaVersionError,
)
from .types import ForgetDecision, Memory, Stats, utc_now

__version__ = "0.1.0"
__display_name__ = "记忆胶囊"

# ---- 兼容别名：同一对象引用，零包装零转发开销 ----
Database = MemoryDB        # Database is MemoryDB -> True
Config = MemoryConfig      # Config   is MemoryConfig -> True

__all__ = [
    "MemoryDB",
    "MemoryConfig",
    "Memory",
    "Stats",
    "ForgetDecision",
    "utc_now",
    "Database",
    "Config",
    "BrainMemoryError",
    "ConfigError",
    "DatabaseError",
    "SchemaVersionError",
    "ClosedError",
    "LockTimeoutError",
    "LockOrderError",
    "ExtensionError",
    "EmbeddingDimensionError",
    "__display_name__",
    "__version__",
]
