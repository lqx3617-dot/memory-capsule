"""记忆胶囊（brain_memory）—— 可嵌入的长期记忆库。

纯 stdlib + sqlite3，零外部依赖。L0-L4 五层记忆、混合检索（FTS5 + 向量 RRF）、
三态遗忘、真实巩固（L3 实体页 + DREAMS.md）。

宿主接入有两种方式：mcp_server.py（工具调用）与 memory_proxy.py（全自动注入）。
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

__version__ = "1.6.0"
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
