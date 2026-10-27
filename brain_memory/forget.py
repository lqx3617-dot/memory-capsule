"""遗忘：quality 打分与判定（Rank-based Forgetting）。

quality = clamp01(feedback * decay * connectivity)
  feedback    : atoms.feedback（used=1.0 / useful=0.8 / noise=0.0）
  decay       : 0.5 ** (age_days / halflife)，半衰期按记忆类型分桶
  connectivity: clamp01(1 + log2(1 + refs))，refs = 被引用次数

判定（默认阈值）：
  低质量 + 低曝光 -> silent（不返回，不删除）
  低质量 + 高曝光 -> archive（不可检索，可恢复）
  绝不自动物理删除；hard 只删 atoms 行，raw_events 永不动。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

from .config import MemoryConfig
from .types import HALFLIFE_BUCKET, ForgetDecision, parse_utc, utc_now

if TYPE_CHECKING:
    from .db import MemoryDB

_CONTENT_PREVIEW = 120


def half_life_days(cfg: MemoryConfig, fact_type: str) -> float:
    bucket = HALFLIFE_BUCKET.get(fact_type, "semantic")
    if bucket == "episodic":
        return cfg.halflife_episodic_d
    if bucket == "procedural":
        return cfg.halflife_procedural_d
    return cfg.halflife_semantic_d


def compute_quality(
    *,
    cfg: MemoryConfig,
    feedback: float,
    fact_type: str,
    last_active: datetime | None,
    now: datetime | None = None,
    refs: int = 0,
    access_count: int = 0,
) -> float:
    now = now or datetime.now(timezone.utc)
    if last_active is None:
        decay = 1.0
    else:
        age_days = max(0.0, (now - last_active).total_seconds() / 86400.0)
        decay = 0.5 ** (age_days / half_life_days(cfg, fact_type))
    connectivity = 1.0 + math.log2(1.0 + max(0, refs) + max(0, access_count))
    quality = feedback * decay * connectivity
    return max(0.0, min(1.0, quality))


def classify(cfg: MemoryConfig, quality: float, access_count: int) -> tuple[str, str]:
    """返回 (action, reason)；action 属于 keep/silent/archive。"""
    if quality < cfg.silent_quality and access_count < cfg.silent_access_max:
        return (
            "silent",
            "quality %.3f < %.2f 且曝光 %d < %d"
            % (quality, cfg.silent_quality, access_count, cfg.silent_access_max),
        )
    if quality < cfg.archive_quality and access_count >= cfg.archive_access_min:
        return (
            "archive",
            "quality %.3f < %.2f 且曝光 %d >= %d"
            % (quality, cfg.archive_quality, access_count, cfg.archive_access_min),
        )
    return "keep", "保留"


def _ref_counts(conn) -> dict[int, int]:
    rows = conn.execute(
        "SELECT supersedes_id, COUNT(*) FROM atoms "
        "WHERE supersedes_id IS NOT NULL GROUP BY supersedes_id"
    ).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


def scan(
    db: "MemoryDB",
    *,
    limit: int | None = None,
    older_than_days: float | None = None,
) -> list[ForgetDecision]:
    """只读扫描，不写库。dry_run 与真实执行共用这一份判定。"""
    cfg = db.config
    sql = (
        "SELECT id, content, quality, access_count, feedback, fact_type, "
        "created_at, last_accessed_at, state FROM atoms "
        "WHERE state IN ('active', 'silent', 'archived')"
    )
    params: list[object] = []
    if older_than_days is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400.0
        sql += " AND created_at <= ?"
        params.append(
            datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    sql += " ORDER BY id"
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))

    now = datetime.now(timezone.utc)
    out: list[ForgetDecision] = []
    with db._read_conn() as conn:
        refs = _ref_counts(conn)
        rows = conn.execute(sql, tuple(params)).fetchall()
        for row in rows:
            fact_type = row["fact_type"]
            feedback = float(row["feedback"])
            last = parse_utc(row["last_accessed_at"]) or parse_utc(row["created_at"])
            quality = compute_quality(
                cfg=cfg,
                feedback=feedback,
                fact_type=fact_type,
                last_active=last,
                now=now,
                refs=refs.get(int(row["id"]), 0),
                access_count=int(row["access_count"]),
            )
            action, reason = classify(cfg, quality, int(row["access_count"]))
            if action == "keep":
                continue
            content = str(row["content"])
            if len(content) > _CONTENT_PREVIEW:
                content = content[:_CONTENT_PREVIEW] + "…"
            age_days = 0.0
            created = parse_utc(row["created_at"])
            if created is not None:
                age_days = round((now - created).total_seconds() / 86400.0, 2)
            out.append(
                ForgetDecision(
                    atom_id=int(row["id"]),
                    content=content,
                    quality=round(quality, 4),
                    access_count=int(row["access_count"]),
                    age_days=age_days,
                    action=action,
                    reason=reason,
                )
            )
    return out


def apply(db: "MemoryDB", decisions: Iterable[ForgetDecision]) -> int:
    """把 silent/archive 落到 atoms.state；不物理删除。"""
    items = list(decisions)
    if not items:
        return 0
    now = utc_now()
    changed = 0
    with db._write_tx() as conn:
        for d in items:
            if d.action not in ("silent", "archive"):
                continue
            cur = conn.execute(
                "UPDATE atoms SET state = ?, state_changed_at = ?, quality = ? "
                "WHERE id = ? AND state != ?",
                (d.action, now, d.quality, d.atom_id, d.action),
            )
            if cur.rowcount:
                changed += int(cur.rowcount)
                db._journal(conn, d.action, "atoms", d.atom_id, {"reason": d.reason})
    return changed


def unlink(db: "MemoryDB", decisions: Iterable[ForgetDecision], confirm: bool) -> int:
    """物理删除 atoms 行（raw_events 永久保留）。必须 confirm=True。"""
    if not confirm:
        raise ValueError("hard 删除必须同时传 confirm=True")
    if not db.config.allow_hard_delete:
        raise ValueError("配置 allow_hard_delete=False，禁止物理删除")
    items = [d for d in decisions if d.action in ("silent", "archive", "unlink")]
    if not items:
        return 0
    removed = 0
    with db._write_tx() as conn:
        for d in items:
            row = conn.execute("SELECT content FROM atoms WHERE id = ?", (d.atom_id,)).fetchone()
            if row is None:
                continue
            try:
                conn.execute("DELETE FROM atoms_fts WHERE rowid = ?", (d.atom_id,))
            except Exception:
                pass
            conn.execute("DELETE FROM atoms WHERE id = ?", (d.atom_id,))
            removed += 1
            db._journal(
                conn,
                "unlink",
                "atoms",
                d.atom_id,
                {"content_digest": str(row[0])[:200], "quality": d.quality},
            )
    return removed
