"""记忆胶囊 异常层级。

设计原则：降级（WAL 失败 / sqlite-vec 不可用 / LLM 不可用）**不抛异常**，
只写日志 + 记录可观测字段；只有调用方显式要求（require_wal / require_vector）时才抛。
参数非法一律用 Python 惯例的 ValueError，不包进本层级。
"""
from __future__ import annotations


class BrainMemoryError(Exception):
    """本库所有异常的基类。"""


class ConfigError(BrainMemoryError):
    """配置项类型或取值非法。"""


class DatabaseError(BrainMemoryError):
    """数据库打开失败、磁盘满、库损坏、事务失败等。"""


class SchemaVersionError(BrainMemoryError):
    """库内 PRAGMA user_version 高于本库支持的版本（拒绝擅自降级）。"""


class ClosedError(BrainMemoryError):
    """实例已 close() 之后继续调用。"""


class LockTimeoutError(BrainMemoryError):
    """锁等待超过 busy_timeout（意味明确失败，而不是死锁）。"""


class LockOrderError(BrainMemoryError):
    """锁序错误：持有读锁的线程再申请写锁会自锁，直接报错而不是挂住。"""


class ExtensionError(BrainMemoryError):
    """显式 require_vector=True 而扩展不可用。"""


class EmbeddingDimensionError(ExtensionError):
    """向量维度与已建 vec0 表不一致。"""
