"""记忆胶囊命令行入口：python -m brain_memory <command> ...

CLI 是附带品，不是主交付物。App 里永远走 import，不要 fork 子进程调 CLI。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from . import __version__
from .config import MemoryConfig
from .db import MemoryDB
from .errors import BrainMemoryError
from .types import FACT_TYPES, ROLES

_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _parse_kv(pairs: Sequence[str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise ValueError(what + " 需要 K=V 形式，收到: " + item)
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m brain_memory",
        description="可嵌入长期记忆库的命令行工具（调试/运维用）",
    )
    parser.add_argument("--db", required=True, help="SQLite 文件路径")
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="K=V",
        help="覆盖配置项，可重复，例如 --config journal_mode_target=DELETE",
    )
    parser.add_argument("--log-level", default=None, choices=_LOG_LEVELS)
    parser.add_argument("--version", action="version", version="记忆胶囊 (brain_memory) " + __version__)

    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="写入一条原始事件")
    p_ingest.add_argument("content_pos", nargs="?", metavar="CONTENT", default=None,
                          help="内容（位置参数，等价于 --content）")
    p_ingest.add_argument("--session", "-s", default="cli", help="会话 ID（默认 cli）")
    p_ingest.add_argument("--role", "-r", default="user", choices=list(ROLES))
    p_ingest.add_argument("--content", "-c", default=None,
                          help="内容；写 - 表示从 stdin 读")
    p_ingest.add_argument("--meta", action="append", default=[], metavar="K=V")
    p_ingest.add_argument("--no-promote", action="store_true", help="只落 L0/L1，不晋升 L2")

    p_recall = sub.add_parser("recall", help="检索记忆")
    p_recall.add_argument("query", help="查询词")
    p_recall.add_argument("--top-k", type=int, default=5)
    p_recall.add_argument("--budget", type=int, default=2000, help="token 预算")
    p_recall.add_argument("--min-quality", type=float, default=None)
    p_recall.add_argument("--fact-type", action="append", default=[], choices=list(FACT_TYPES))
    p_recall.add_argument("--session", default=None)
    p_recall.add_argument("--layer", default="L2")
    p_recall.add_argument("--json", action="store_true")

    p_forget = sub.add_parser("forget", help="遗忘扫描（默认 dry_run）")
    p_forget.add_argument("--apply", action="store_true", help="真的写静默/归档状态")
    p_forget.add_argument("--hard", action="store_true", help="物理删除 atoms 行（需 --confirm）")
    p_forget.add_argument("--confirm", action="store_true")
    p_forget.add_argument("--limit", type=int, default=None)
    p_forget.add_argument("--older-than-days", type=float, default=None)
    p_forget.add_argument("--json", action="store_true")

    p_stats = sub.add_parser("stats", help="打印各层计数与降级状态")
    p_stats.add_argument("--json", action="store_true")

    p_dream = sub.add_parser("dream", help="执行一次巩固（生成 L3 实体页 + DREAMS.md）")
    p_dream.add_argument("--no-persist", action="store_true")
    p_dream.add_argument("--max-atoms", type=int, default=None)

    p_maint = sub.add_parser("maintain", help="PRAGMA optimize / checkpoint / VACUUM")
    p_maint.add_argument("--level", default="normal", choices=["light", "normal", "full"])

    return parser


# ---------------------------------------------------------------- 各子命令


def _cmd_ingest(db: MemoryDB, args: argparse.Namespace) -> int:
    raw = args.content if args.content is not None else args.content_pos
    if raw is None:
        raise ValueError('必须提供内容：ingest "文本"、--content "文本"，或 -c - 从 stdin 读')
    content = sys.stdin.read() if raw == "-" else raw
    meta = _parse_kv(args.meta, "--meta") or None
    event_id = db.ingest(
        args.session, args.role, content, meta=meta, promote=not args.no_promote
    )
    st = db.stats()
    print("event_id     = " + str(event_id))
    print(
        "L0 事件 = %d | L1 待审 = %d | L2 活跃 = %d"
        % (st.counts["l0_events"], st.counts["l1_pending"], st.counts["l2_active"])
    )
    return 0


def _cmd_recall(db: MemoryDB, args: argparse.Namespace) -> int:
    hits = db.recall(
        args.query,
        args.top_k,
        args.budget,
        min_quality=args.min_quality,
        fact_types=args.fact_type,
        session_id=args.session,
        layer=args.layer,
    )
    if args.json:
        print(json.dumps([m.as_dict() for m in hits], ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print("（无命中）")
        return 0
    for index, m in enumerate(hits, 1):
        print(
            "[%d] atom#%-4d score=%.4f quality=%.3f conf=%.3f src=%s%s"
            % (
                index,
                m.atom_id,
                m.score,
                m.quality,
                m.confidence,
                m.source_event_id,
                "  (截断)" if m.truncated else "",
            )
        )
        print("    " + m.content.replace("\n", " ")[:220])
    return 0


def _cmd_forget(db: MemoryDB, args: argparse.Namespace) -> int:
    items = db.forget(
        dry_run=not args.apply,
        hard=args.hard,
        confirm=args.confirm,
        limit=args.limit,
        older_than_days=args.older_than_days,
    )
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    if not items:
        print("（没有需要处理的条目）")
        return 0
    for it in items:
        print(
            "atom#%-4d action=%-7s q=%.3f acc=%-3d age=%.1fd  %s"
            % (
                it["atom_id"],
                it["action"],
                it["quality"],
                it["access_count"],
                it["age_days"],
                it["content"][:60],
            )
        )
    print()
    print("共 %d 条" % len(items))
    if not args.apply:
        print("dry_run：库内未改动。加 --apply 才写入状态。")
    return 0


def _cmd_stats(db: MemoryDB, args: argparse.Namespace) -> int:
    st = db.stats()
    if args.json:
        print(json.dumps(st.as_dict(), ensure_ascii=False, indent=2))
        return 0
    print("counts:")
    for key, value in st.counts.items():
        print("  %-16s %d" % (key, value))
    print("avg_quality      %.4f" % st.avg_quality)
    print("avg_confidence   %.4f" % st.avg_confidence)
    print("silent / archived  %d / %d" % (st.silent_count, st.archived_count))
    print("journal_mode     " + st.journal_mode)
    print("sync_mode        " + st.sync_mode)
    print("fts_tokenizer    " + st.fts_tokenizer)
    print("vector_backend   " + st.vector_backend)
    print("llm_mode         " + st.llm_mode)
    print("sqlite_version   " + st.sqlite_version)
    print("db_bytes         " + str(st.db_bytes))
    print("pending_access   " + str(st.pending_access_flush))
    return 0


def _cmd_dream(db: MemoryDB, args: argparse.Namespace) -> int:
    print(db.dream(persist=not args.no_persist, max_atoms=args.max_atoms))
    return 0


def _cmd_maintain(db: MemoryDB, args: argparse.Namespace) -> int:
    result = db.maintenance(args.level)
    for key, value in result.items():
        print("%-18s %s" % (key, value))
    return 0


_DISPATCH = {
    "ingest": _cmd_ingest,
    "recall": _cmd_recall,
    "forget": _cmd_forget,
    "stats": _cmd_stats,
    "dream": _cmd_dream,
    "maintain": _cmd_maintain,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    overrides = _parse_kv(args.config, "--config")
    if args.log_level:
        overrides["log_level"] = args.log_level

    try:
        config = MemoryConfig.resolve(overrides or None)
        db = MemoryDB(args.db, config)
    except (BrainMemoryError, ValueError) as exc:
        print("初始化失败: " + str(exc), file=sys.stderr)
        return 1

    try:
        handler = _DISPATCH[args.command]
        return handler(db, args)
    except (BrainMemoryError, ValueError) as exc:
        print("执行失败: " + str(exc), file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
