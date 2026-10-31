"""公开数据类型与枚举常量。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------- 枚举常量

LAYERS = ("L0", "L1", "L2", "L3")
ROLES = ("user", "assistant", "system", "tool")
DEFAULT_ROLE = "user"

# 注意：convention（项目惯例）是门控 SIGNAL_WORDS 会产出的类型，
# 早先漏在白名单外，导致 remember(fact_type="convention") 被抹成 fact。
FACT_TYPES = ("fact", "decision", "task", "preference", "constraint",
              "convention", "bugroot")
CANDIDATE_STATUS = ("pending", "promoted", "rejected", "merged")
ATOM_STATES = ("active", "silent", "archived", "superseded")
FORGET_ACTIONS = ("silent", "archive", "unlink")
SIGNALS = ("used", "useful", "noise")

JOURNAL_ACTIONS = (
    "ingest",
    "gate_reject",
    "gate_llm_fallback",
    "promote",
    "merge",
    "conflict",
    "core_protect",    # 核心事实挡住了覆盖请求
    "hormone_shift",   # 内分泌被事件扰动
    "silent",          # 与 ATOM_STATES / FORGET_ACTIONS 对齐；原来写成 silence，从未产生
    "archive",
    "revive",
    "unlink",
    "dream",
    "maintenance",
    "degrade_wal",
    "degrade_vec",
    "vector_ready",
    "schema_migrate",
    "role_unknown",
)

# 记忆类型 -> 衰减半衰期桶（规则：情景约 7 天 / 语义约 90 天 / 程序约 365 天）
HALFLIFE_BUCKET = {
    "fact": "semantic",
    "decision": "semantic",
    "constraint": "semantic",
    "convention": "semantic",
    "bugroot": "procedural",
    "task": "episodic",
    "preference": "procedural",
}


def utc_now() -> str:
    """当前 UTC 时间，ISO8601 秒级精度，带 Z 后缀。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str | None) -> datetime | None:
    """解析本库写出的 ISO8601 时间；解析失败返回 None（不抛）。"""
    if not value:
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True, slots=True)
class Memory:
    """recall 返回的单条记忆。

    契约保证字段：content / source_event_id / confidence / quality。
    其余为附加字段，允许后续增列而不破坏兼容。
    """

    atom_id: int
    content: str
    source_event_id: int | None
    confidence: float
    quality: float
    fact_type: str = "fact"
    entity_id: int | None = None
    access_count: int = 0
    created_at: str | None = None
    last_accessed_at: str | None = None
    score: float = 0.0
    layer: str = "L2"
    truncated: bool = False
    record_id: int | None = None   # 当前层的主键：L2=atom_id，L1=candidate.id，L0=raw_event.id
    is_core: bool = False          # 核心事实：冲突时受保护，新事实不许覆盖它
    mood: float = 0.0              # 写入时的心情值（-1~1），供 f_endo 做同向放大

    def as_dict(self) -> dict[str, Any]:
        return {
            "atom_id": self.atom_id,
            "content": self.content,
            "source_event_id": self.source_event_id,
            "confidence": self.confidence,
            "quality": self.quality,
            "fact_type": self.fact_type,
            "entity_id": self.entity_id,
            "access_count": self.access_count,
            "created_at": self.created_at,
            "last_accessed_at": self.last_accessed_at,
            "score": self.score,
            "layer": self.layer,
            "truncated": self.truncated,
            "record_id": self.record_id,
            "is_core": self.is_core,
            "mood": self.mood,
        }


@dataclass(frozen=True, slots=True)
class ForgetDecision:
    """forget 的判定结果；对外以 dict 形态返回。"""

    atom_id: int
    content: str
    quality: float
    access_count: int
    age_days: float
    action: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "atom_id": self.atom_id,
            "content": self.content,
            "quality": self.quality,
            "access_count": self.access_count,
            "age_days": self.age_days,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Stats:
    """stats() 的返回快照。"""

    counts: dict[str, int] = field(default_factory=dict)
    avg_quality: float = 0.0
    avg_confidence: float = 0.0
    silent_count: int = 0
    archived_count: int = 0
    journal_mode: str = "unknown"
    vector_backend: str = "none"
    llm_mode: str = "off"
    fts_tokenizer: str = "none"
    db_bytes: int = 0
    sqlite_version: str = ""
    pending_access_flush: int = 0
    sync_mode: str = "unknown"
    closed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "avg_quality": self.avg_quality,
            "avg_confidence": self.avg_confidence,
            "silent_count": self.silent_count,
            "archived_count": self.archived_count,
            "journal_mode": self.journal_mode,
            "vector_backend": self.vector_backend,
            "llm_mode": self.llm_mode,
            "fts_tokenizer": self.fts_tokenizer,
            "db_bytes": self.db_bytes,
            "sqlite_version": self.sqlite_version,
            "pending_access_flush": self.pending_access_flush,
            "sync_mode": self.sync_mode,
            "closed": self.closed,
        }
