"""混合检索：FTS5 关键词召回 + quality / recency 重排 + token 预算截断。

Phase 1 只走 FTS5；sqlite-vec 不可用时就是这条路径（降级不是异常分支，而是默认分支）。
recall 全程零写：命中只累加到内存访问计数，由 flusher 异步刷盘。
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

from .forget import compute_quality
from .types import Memory, parse_utc

if TYPE_CHECKING:
    from .db import MemoryDB

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_SEG_SPLIT_RE = re.compile(r"[\s,，。；;！!？?、:：/\\|\-—_（）()\[\]{}<>《》\"'“”]+")

STOPWORDS = frozenset(
    """
    的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有
    看 好 自己 这 那 这个 那个 什么 怎么 为 与 及 或 但是 然后 所以 因为 如果
    可以 应该 需要 现在 还有 就是 不是 我们 你们 他们 它 吗 呢 吧 啊 呀
    """.split()
)


def tokenize(query: str) -> list[str]:
    """英文/数字整词 + 中文二元组，去停用词。"""
    out: list[str] = []
    for m in _WORD_RE.finditer(query):
        tok = m.group(0).lower()
        if len(tok) >= 2 and tok not in STOPWORDS:
            out.append(tok)
    for m in _CJK_RE.finditer(query):
        seg = m.group(0)
        if len(seg) == 1:
            out.append(seg)
            continue
        for i in range(len(seg) - 1):
            bigram = seg[i : i + 2]
            if bigram not in STOPWORDS:
                out.append(bigram)
    seen: set[str] = set()
    deduped: list[str] = []
    for tok in out:
        if tok not in seen:
            seen.add(tok)
            deduped.append(tok)
    return deduped


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def build_match_expr(query: str, tokenizer: str) -> str:
    """构造安全的 FTS5 MATCH 表达式。trigram 用分段短语，unicode61 用 bigram。"""
    text = query.strip()
    if not text:
        return ""
    if tokenizer == "trigram":
        segments = [s.strip() for s in _SEG_SPLIT_RE.split(text) if len(s.strip()) >= 2]
        if not segments:
            segments = [text]
        return " OR ".join(_quote(s) for s in segments[:6])
    toks = tokenize(text)[:12]
    if not toks:
        return ""
    return " OR ".join(_quote(t) for t in toks)


_SELECT = (
    "SELECT a.id AS id, a.content AS content, a.source_event_id AS source_event_id, "
    "a.confidence AS confidence, a.quality AS quality, a.fact_type AS fact_type, "
    "a.entity_id AS entity_id, a.access_count AS access_count, a.feedback AS feedback, "
    "a.created_at AS created_at, a.last_accessed_at AS last_accessed_at, "
    "bm25(atoms_fts) AS rank "
    "FROM atoms_fts JOIN atoms a ON a.id = atoms_fts.rowid "
    "WHERE atoms_fts MATCH ? AND a.state = 'active'"
)


def _recency(created_at: str | None, last_accessed_at: str | None) -> float:
    dt = parse_utc(last_accessed_at) or parse_utc(created_at)
    if dt is None:
        return 0.5
    age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
    return 0.5 ** (age_days / 90.0)


def _estimate_tokens(text: str, chars_per_token: float) -> int:
    return max(1, int(math.ceil(len(text) / max(0.1, chars_per_token))))


def _like_fallback(conn, query: str, limit: int, min_quality: float) -> list:
    toks = tokenize(query)[:3]
    if not toks:
        return []
    where = " OR ".join(["content LIKE ?"] * len(toks))
    params = ["%" + t + "%" for t in toks]
    sql = (
        "SELECT id, content, source_event_id, confidence, quality, fact_type, "
        "entity_id, access_count, feedback, created_at, last_accessed_at, 0.0 AS rank "
        "FROM atoms WHERE state = 'active' AND quality >= ? AND (" + where + ") "
        "ORDER BY quality DESC LIMIT ?"
    )
    return conn.execute(sql, tuple([min_quality] + params + [limit])).fetchall()


def _rrf(rankings: list[list[int]], k: int) -> dict[int, float]:
    """Reciprocal Rank Fusion：只看名次，不看分数量纲。"""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, atom_id in enumerate(ranking, start=1):
            scores[atom_id] = scores.get(atom_id, 0.0) + 1.0 / (k + rank)
    return scores


_ROW_SQL = (
    "SELECT id, content, source_event_id, confidence, quality, fact_type, "
    "entity_id, access_count, feedback, created_at, last_accessed_at FROM atoms "
    "WHERE state = 'active' AND id IN ({marks})"
)


def live_quality(cfg, row: dict) -> float:
    """现场重算 quality —— 存储值只是写入时的快照，会随时间失真。

    atoms.quality 写入时 = confidence，之后不会自己衰减；而 forget/dream
    判定用的是 compute_quality()。两套口径会让 recall 的 min_quality 过滤形同虚设
    （半年前的记忆存储值仍是 0.7）。这里统一成「现算」，与 forget/dream 同源。
    """
    try:
        return compute_quality(
            cfg=cfg,
            feedback=float(row.get("feedback") if row.get("feedback") is not None else 1.0),
            fact_type=str(row.get("fact_type") or "fact"),
            last_active=parse_utc(row.get("last_accessed_at")) or parse_utc(row.get("created_at")),
            refs=0,
            access_count=int(row.get("access_count") or 0),
        )
    except Exception:
        return float(row.get("quality") or 0.0)


def _fetch_rows(conn, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(_ROW_SQL.format(marks=marks), tuple(ids)).fetchall()
    return {int(r["id"]): dict(r) for r in rows}



# ---------------------------------------------------------------- 低置信度下探


def _token_overlap(tokens: list[str], text: str) -> float:
    """命中词数 / 总词数。下探打分不需要精细，只要能把明显相关的挑出来。"""
    if not tokens:
        return 0.0
    low = text.lower()
    hit = sum(1 for tok in tokens if tok in low)
    return hit / len(tokens)


def _escalate(
    db: "MemoryDB",
    query: str,
    need: int,
    budget_left: int,
    cfg,
) -> list[Memory]:
    """L2 置信度不足时向下找：先 L1 候选，再 L0 原始事件。

    两条硬约束：
    1. L0 只看最近 escalate_l0_limit 条，绝不整表扫。
    2. 总条数与预算都由调用方给，超了就停。
    """
    tokens = tokenize(query)[:8]
    if not tokens or need <= 0 or budget_left <= 0:
        return []

    out: list[Memory] = []
    used = 0
    seen_events: set[int] = set()
    marks = " OR ".join(["content LIKE ?"] * len(tokens))
    params = ["%" + tok + "%" for tok in tokens]

    # ---- L1：候选事实 ----
    rows = []
    try:
        with db._read_conn() as conn:
            rows = conn.execute(
                "SELECT id, raw_event_id, content, confidence, fact_type FROM candidates "
                "WHERE (" + marks + ") AND status IN ('pending','promoted') "
                "ORDER BY id DESC LIMIT ?",
                tuple(params + [int(cfg.escalate_max_results) * 3]),
            ).fetchall()
    except Exception:
        rows = []

    scored = []
    for row in rows:
        sim = _token_overlap(tokens, str(row["content"]))
        if sim > 0:
            scored.append((sim, dict(row)))
    scored.sort(key=lambda item: item[0], reverse=True)

    for sim, row in scored:
        if len(out) >= need:
            break
        content = str(row["content"])
        cost = _estimate_tokens(content, cfg.chars_per_token)
        if used + cost > budget_left:
            break
        used += cost
        if row["raw_event_id"]:
            seen_events.add(int(row["raw_event_id"]))
        out.append(
            Memory(
                atom_id=0,
                record_id=int(row["id"]),
                content=content,
                source_event_id=row["raw_event_id"],
                confidence=float(row["confidence"] or 0.0),
                quality=0.0,
                fact_type=str(row["fact_type"] or "fact"),
                layer="L1",
                score=round(sim * 0.6, 6),
            )
        )

    # ---- L0：原始事件（只看最近 N 条）----
    rows = []
    try:
        with db._read_conn() as conn:
            rows = conn.execute(
                "SELECT id, content, role, created_at FROM raw_events "
                "WHERE (" + marks + ") ORDER BY id DESC LIMIT ?",
                tuple(params + [int(cfg.escalate_l0_limit)]),
            ).fetchall()
    except Exception:
        rows = []

    scored = []
    for row in rows:
        sim = _token_overlap(tokens, str(row["content"]))
        if sim > 0:
            scored.append((sim, dict(row)))
    scored.sort(key=lambda item: item[0], reverse=True)

    for sim, row in scored:
        if len(out) >= need:
            break
        if int(row["id"]) in seen_events:
            continue          # L1 已经给过同一条原文，不重复返回
        content = str(row["content"])
        cost = _estimate_tokens(content, cfg.chars_per_token)
        if used + cost > budget_left:
            break
        used += cost
        out.append(
            Memory(
                atom_id=0,
                record_id=int(row["id"]),
                content=content,
                source_event_id=int(row["id"]),
                confidence=0.3,
                quality=0.0,
                fact_type="raw",
                layer="L0",
                score=round(sim * 0.4, 6),
                created_at=row["created_at"],
            )
        )
    return out


def search(
    db: "MemoryDB",
    query: str,
    top_k: int = 5,
    budget_tokens: int = 2000,
    *,
    min_quality: float | None = None,
    fact_types: Iterable[str] | None = None,
    session_id: str | None = None,
    layer: str = "L2",
    escalate: bool | None = None,
) -> list[Memory]:
    """FTS5 + 向量双路召回，RRF 融合，quality 过滤，token 预算截断。

    escalate=None 用全局配置；显式 True/False 只影响本次调用
    （评论这种大批量场景可以常驻关，私信按需开）。
    """
    cfg = db.config
    if not isinstance(query, str):
        raise ValueError("query 需要字符串")
    if budget_tokens <= 0:
        raise ValueError("budget_tokens 必须大于 0")
    if top_k <= 0:
        return []

    text = query.strip()
    if not text:
        return []

    top_k = min(int(top_k), cfg.top_k_max)
    floor_quality = cfg.recall_min_quality if min_quality is None else float(min_quality)
    types = [t for t in (fact_types or []) if t]

    if layer != "L2" and not cfg.enable_escalation:
        logging.getLogger("brain_memory").debug(
            "recall: layer=%s 未启用 escalation，按 L2 处理", layer
        )

    match_expr = build_match_expr(text, db.fts_tokenizer)
    fts_ranked: list[int] = []
    vec_ranked: list[int] = []
    vec_sims: dict[int, float] = {}
    rows_by_id: dict[int, dict] = {}
    used_fts = False
    used_vec = False
    candidate_limit = max(top_k * cfg.vec_candidate_multiplier, top_k)

    with db._read_conn() as conn:
        # ---- 路 1：FTS5 ----
        if match_expr:
            sql = _SELECT
            params: list[object] = [match_expr]
            # 不在这里按 a.quality 预过滤：存储值是写入快照，会让过滤形同虚设。
            # 统一在下面的预算循环里用 live_quality() 判定。
            if types:
                sql += " AND a.fact_type IN (" + ",".join("?" * len(types)) + ")"
                params.extend(types)
            if session_id:
                sql += " AND a.source_event_id IN (SELECT id FROM raw_events WHERE session_id = ?)"
                params.append(session_id)
            sql += " ORDER BY rank LIMIT ?"
            params.append(candidate_limit)
            try:
                rows = conn.execute(sql, tuple(params)).fetchall()
            except Exception:
                rows = []
            for row in rows:
                atom_id = int(row["id"])
                if atom_id in rows_by_id:
                    continue
                rows_by_id[atom_id] = dict(row)
                fts_ranked.append(atom_id)
            used_fts = bool(rows)

        # ---- 路 1b：FTS 零命中时 LIKE 兜底 ----
        if not fts_ranked and cfg.enable_like_fallback:
            try:
                rows = _like_fallback(conn, text, candidate_limit, floor_quality)
            except Exception:
                rows = []
            for row in rows:
                atom_id = int(row["id"])
                if atom_id in rows_by_id:
                    continue
                rows_by_id[atom_id] = dict(row)
                fts_ranked.append(atom_id)

        # ---- 路 2：向量 ----
        store = getattr(db, "vector_store", None)
        if store is not None and store.backend != "none":
            try:
                pairs = store.search(conn, text, candidate_limit)
            except Exception as exc:
                logging.getLogger("brain_memory").warning(
                    "recall: 向量检索异常，只用 FTS5: %s", exc
                )
                pairs = []
            if pairs:
                used_vec = True
                vec_sims.update({int(a): float(s) for a, s in pairs})
                for atom_id, _sim in pairs:
                    vec_ranked.append(atom_id)
                missing = [i for i in vec_ranked if i not in rows_by_id]
                rows_by_id.update(_fetch_rows(conn, missing))

    do_escalate = cfg.enable_escalation if escalate is None else bool(escalate)

    if not rows_by_id and not do_escalate:
        # 没候选且下探关闭：直接空返回，绝不做额外查询
        return []

    # ---- 融合 ----
    ranked_items: list[tuple[int, float]] = []
    if vec_ranked and fts_ranked:
        fused = _rrf([fts_ranked, vec_ranked], int(cfg.rrf_k))
        ceiling = 2.0 / (int(cfg.rrf_k) + 1)     # 两路都第一时的理论最大值
        for atom_id, value in fused.items():
            if atom_id in rows_by_id:
                ranked_items.append((atom_id, min(1.0, value / ceiling if ceiling else 0.0)))
        ranked_items.sort(key=lambda item: item[1], reverse=True)
    elif vec_ranked:
        # 只有向量：用相似度本身，再乘一点 quality
        for atom_id in vec_ranked:
            if atom_id not in rows_by_id:
                continue
            quality = live_quality(cfg, rows_by_id[atom_id])
            sim = vec_sims.get(atom_id, 0.0)
            ranked_items.append((atom_id, min(1.0, 0.8 * sim + 0.2 * quality)))
        ranked_items.sort(key=lambda item: item[1], reverse=True)
    else:
        # 只有 FTS：Phase 1 的打分（bm25 归一 + quality + recency）
        ranks = [float(rows_by_id[i].get("rank", 0.0)) for i in fts_ranked]
        lo, hi = (min(ranks), max(ranks)) if ranks else (0.0, 0.0)
        span = hi - lo
        for atom_id in fts_ranked:
            row = rows_by_id[atom_id]
            rank = float(row.get("rank", 0.0))
            norm = 1.0 if span <= 1e-9 else (hi - rank) / span
            quality = live_quality(cfg, row)
            recency = _recency(row.get("created_at"), row.get("last_accessed_at"))
            score = cfg.w_bm25 * norm + cfg.w_quality * quality + cfg.w_recency * recency
            ranked_items.append((atom_id, score))
        ranked_items.sort(key=lambda item: item[1], reverse=True)

    # ---- 预算截断 ----
    out: list[Memory] = []
    used_tokens = 0
    for atom_id, score in ranked_items:
        row = rows_by_id[atom_id]
        quality = live_quality(cfg, row)
        if quality < floor_quality:
            continue
        if types and str(row.get("fact_type")) not in types:
            continue
        content = str(row.get("content") or "")
        need = _estimate_tokens(content, cfg.chars_per_token)
        truncated = False
        if used_tokens + need > budget_tokens:
            remain = budget_tokens - used_tokens
            if remain <= 0:
                break
            keep_chars = max(1, int(remain * cfg.chars_per_token))
            content = content[:keep_chars]
            truncated = True
            need = remain
        used_tokens += need
        out.append(
            Memory(
                atom_id=atom_id,
                content=content,
                source_event_id=row.get("source_event_id"),
                confidence=float(row.get("confidence") or 0.0),
                quality=quality,
                fact_type=str(row.get("fact_type") or "fact"),
                entity_id=row.get("entity_id"),
                access_count=int(row.get("access_count") or 0),
                created_at=row.get("created_at"),
                last_accessed_at=row.get("last_accessed_at"),
                score=round(float(score), 6),
                layer="L2",
                truncated=truncated,
            )
        )
        if len(out) >= top_k:
            break

    # ---- 低置信度下探（默认关）----
    escalated = 0
    if do_escalate and len(out) < top_k:
        best = max((m.score for m in out), default=0.0)
        if not out or best < cfg.escalate_threshold:
            extra = _escalate(
                db, text, top_k - len(out), budget_tokens - used_tokens, cfg
            )
            if extra:
                escalated = len(extra)
                out.extend(extra)

    for m in out:
        if m.layer == "L2" and m.atom_id:
            db.note_access(m.atom_id)

    logging.getLogger("brain_memory").debug(
        "recall: fts=%d vec=%d fused=%d escalated=%d returned=%d",
        len(fts_ranked), len(vec_ranked), len(ranked_items), escalated, len(out),
    )
    return out
