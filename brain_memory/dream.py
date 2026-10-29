"""巩固 dream()：L2 -> L3 实体页 + DREAMS.md。

Phase 2 实现：
1. 扫 quality >= dream_min_quality 的活跃原子（quality 现场重算，不用缓存值）
2. 按 entity 分组；没有 entity 的按 fact_type 兜底分组
3. 组内按内容相似度聚类
4. 每个簇生成一段 Markdown，写 entity_pages（dirty=0）
5. 汇总写 DREAMS.md
6. 返回本次巩固了哪些实体、哪些记忆

不做的事：不改模型权重，不决定删除，不碰 recall 热路径。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .forget import compute_quality
from .promote import similarity
from .types import parse_utc, utc_now

if TYPE_CHECKING:
    from .db import MemoryDB

_PREVIEW = 160


def _cluster(items: list[dict[str, Any]], threshold: float) -> list[list[dict[str, Any]]]:
    """贪心聚类：跟簇里第一条够像就并进去，否则新开一簇。"""
    clusters: list[list[dict[str, Any]]] = []
    for item in items:
        placed = False
        for cluster in clusters:
            if similarity(item["content"], cluster[0]["content"]) >= threshold:
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])
    return clusters


def _entity_names(conn) -> dict[int, str]:
    try:
        rows = conn.execute("SELECT id, name FROM entities").fetchall()
    except Exception:
        return {}
    return {int(r[0]): str(r[1]) for r in rows}


def _build_page(title: str, kind: str, members: list[dict[str, Any]]) -> str:
    label = "%d 条" % len(members) if kind == "entity" else "%s，%d 条" % (kind, len(members))
    lines = ["## %s（%s）" % (title, label), ""]
    for item in members:
        content = item["content"].replace("\n", " ")
        if len(content) > _PREVIEW:
            content = content[:_PREVIEW] + "…"
        lines.append(
            "- [q=%.2f, atom#%d] %s" % (item["quality"], item["atom_id"], content)
        )
    lines.append("")
    return "\n".join(lines)


def dream(db: "MemoryDB", *, persist: bool = True, max_atoms: int | None = None) -> str:
    cfg = db.config
    now_dt = datetime.now(timezone.utc)
    limit = int(max_atoms) if max_atoms and max_atoms > 0 else 100000

    with db._read_conn() as conn:
        names = _entity_names(conn)
        rows = conn.execute(
            "SELECT id, content, entity_id, fact_type, quality, confidence, "
            "feedback, access_count, created_at, last_accessed_at FROM atoms "
            "WHERE state = 'active' ORDER BY quality DESC LIMIT ?",
            (limit,),
        ).fetchall()

    atoms: list[dict[str, Any]] = []
    for row in rows:
        quality = compute_quality(
            cfg=cfg,
            feedback=float(row["feedback"]),
            fact_type=str(row["fact_type"]),
            last_active=parse_utc(row["last_accessed_at"]) or parse_utc(row["created_at"]),
            now=now_dt,
            access_count=int(row["access_count"]),
        )
        if quality < cfg.dream_min_quality:
            continue
        atoms.append(
            {
                "atom_id": int(row["id"]),
                "content": str(row["content"]),
                "entity_id": row["entity_id"],
                "fact_type": str(row["fact_type"]),
                "quality": quality,
            }
        )

    # 分组：有实体按实体，没实体按 fact_type
    groups: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    for atom in atoms:
        if atom["entity_id"]:
            key: tuple[str, Any] = ("entity", int(atom["entity_id"]))
        else:
            key = ("type", atom["fact_type"])
        groups.setdefault(key, []).append(atom)

    pages: list[dict[str, Any]] = []
    for (kind, key_value), items in groups.items():
        if len(pages) >= cfg.dream_max_clusters:
            break
        if kind == "entity":
            title = names.get(int(key_value), "entity#%s" % key_value)
        else:
            title = str(key_value)
        for cluster in _cluster(items, cfg.dream_cluster_similarity):
            if len(pages) >= cfg.dream_max_clusters:
                break
            pages.append(
                {
                    "kind": kind,
                    "key": key_value,
                    "title": title,
                    "members": cluster,
                    "markdown": _build_page(
                        title, "entity" if kind == "entity" else str(key_value), cluster
                    ),
                }
            )

    header = [
        "# DREAMS",
        "",
        "- 执行时间：" + utc_now(),
        "- 扫描到高质量原子：%d 条（quality >= %.2f）" % (len(atoms), cfg.dream_min_quality),
        "- 生成实体页：%d 个" % len(pages),
        "- 涉及实体：%s"
        % (", ".join(sorted({p["title"] for p in pages})) if pages else "（无）"),
        "",
        "## 巩固详情",
        "",
    ]
    body = [p["markdown"] for p in pages]
    footer = [
        "",
        "## 本轮没做的事",
        "",
        "- 未改动任何模型权重（本库只做外部存储的晋升/摘要/归档）",
        "- 未删除任何原子（保留与否由 quality 决定，见 forget）",
        "- 未在 recall 热路径中执行巩固",
    ]
    text = "\n".join(header + body + footer)

    if persist:
        # 只有全量扫描、且没被 dream_max_clusters 截断，才能断定「没出现在 pages 里的实体页该删」
        prune = max_atoms is None and len(pages) < int(cfg.dream_max_clusters)
        _persist(db, pages, text, len(atoms), cfg, prune=prune)

    return text


def _persist(
    db: "MemoryDB",
    pages: list[dict[str, Any]],
    text: str,
    atom_count: int,
    cfg: Any,
    prune: bool = True,
) -> None:
    written = 0
    with db._write_tx() as conn:
        for page in pages:
            if page["kind"] != "entity":
                continue
            try:
                conn.execute(
                    "INSERT INTO entity_pages(entity_id, markdown, dirty, updated_at) "
                    "VALUES (?, ?, 0, ?) "
                    "ON CONFLICT(entity_id) DO UPDATE SET "
                    "markdown = excluded.markdown, dirty = 0, updated_at = excluded.updated_at",
                    (int(page["key"]), page["markdown"], utc_now()),
                )
                written += 1
            except Exception:
                continue
        # ⚠ 2026-10-29：原来只 upsert，从不清理 —— 实体改名或原子全被归档之后，
        # 旧页会永远留在 entity_pages 和 DREAMS.md 里，越跑越多。
        # 只有「全量扫描且没被 dream_max_clusters 截断」时才敢清理：
        # 否则本次没生成的页不代表它该消失。
        pruned = 0
        if prune:
            keep = [int(p["key"]) for p in pages if p["kind"] == "entity"]
            if keep:
                marks = ",".join("?" * len(keep))
                cur = conn.execute(
                    "DELETE FROM entity_pages WHERE entity_id NOT IN (" + marks + ")",
                    tuple(keep),
                )
            else:
                cur = conn.execute("DELETE FROM entity_pages")
            pruned = int(cur.rowcount or 0)
        db._journal(
            conn,
            "dream",
            detail={
                "phase": 2,
                "stub": False,
                "atoms": atom_count,
                "pages": len(pages),
                "entity_pages_written": written,
                "entity_pages_pruned": pruned,
                "chars": len(text),
            },
        )

    path = cfg.dreams_path
    if not path and db.path and db.path != ":memory:":
        path = os.path.join(os.path.dirname(os.path.abspath(db.path)), "DREAMS.md")
    if path:
        # 先写临时文件再 rename：直接 open(path, "w") 覆盖，写到一半崩掉
        # 会把上一份完好的 DREAMS.md 留成半截。
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
