#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""升级路径：老库能不能原地升上来、数据会不会丢。

前七轮一次都没验过「把 v1 的库交给 1.3.x 打开会怎样」。这里手工造一个 v1 库
（atoms 没有 embedding 列、没有 atoms_fts、没有新索引），然后用当前代码打开，
检查：列补上了、索引补上了、旧数据还在、还能被检索到。

顺带验三件向后兼容的事：未来版本的库要拒绝、删掉的配置项要报错、extract_llm_* 要能回落。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3
import tempfile

import brain_memory as bm
from brain_memory.db import SCHEMA_VERSION
from brain_memory.config import MemoryConfig
from brain_memory.errors import ConfigError, SchemaVersionError
from brain_memory.gate import _LlmClient

FAILS = []

V1_SCHEMA = chr(10).join([
    "CREATE TABLE raw_events(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
    "role TEXT NOT NULL, content TEXT NOT NULL, meta_json TEXT, created_at TEXT NOT NULL);",
    "CREATE TABLE candidates(id INTEGER PRIMARY KEY AUTOINCREMENT, raw_event_id INTEGER NOT NULL, "
    "fact_type TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
    "confidence REAL NOT NULL DEFAULT 0.5, entity_hint TEXT, created_at TEXT NOT NULL, resolved_at TEXT);",
    "CREATE TABLE entities(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
    "type TEXT NOT NULL DEFAULT 'concept', aliases_json TEXT, created_at TEXT NOT NULL);",
    "CREATE TABLE atoms(id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id INTEGER, fact_type TEXT NOT NULL, "
    "content TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.5, quality REAL NOT NULL DEFAULT 1.0, "
    "access_count INTEGER NOT NULL DEFAULT 0, last_accessed_at TEXT, created_at TEXT NOT NULL, "
    "supersedes_id INTEGER, state TEXT NOT NULL DEFAULT 'active', state_changed_at TEXT, "
    "source_event_id INTEGER, source_candidate_id INTEGER, feedback REAL NOT NULL DEFAULT 1.0);",
    "CREATE TABLE entity_pages(id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id INTEGER NOT NULL UNIQUE, "
    "markdown TEXT NOT NULL DEFAULT '', dirty INTEGER NOT NULL DEFAULT 1, updated_at TEXT);",
    "CREATE TABLE journal(id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, "
    "target_table TEXT, target_id INTEGER, detail_json TEXT, created_at TEXT NOT NULL);",
    "PRAGMA user_version = 1;",
])


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def make_v1(path):
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO raw_events(session_id, role, content, created_at) "
                 "VALUES('s','user','v1 时代写下的旧事实', '2026-10-01T00:00:00Z')")
    conn.execute("INSERT INTO atoms(fact_type, content, confidence, quality, created_at, state, feedback) "
                 "VALUES('fact','v1 时代的旧原子：数据库统一用 WAL 模式',0.9,0.9,'2026-10-01T00:00:00Z','active',1.0)")
    conn.commit()
    conn.close()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="bm_mig_")
    path = os.path.join(tmp, "v1.db")
    make_v1(path)
    print("1. 造了一个 v1 库（atoms 无 embedding、无 atoms_fts、无新索引）")
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(atoms)")}
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    check("embedding" not in cols, "确认 v1 的 atoms 没有 embedding 列")
    check(ver == 1, "user_version = 1（实际 %s）" % ver)

    print("2. 用当前代码打开，应当自动迁移")
    db = bm.MemoryDB(path)
    try:
        conn = db.conn
        cols = {r[1] for r in conn.execute("PRAGMA table_info(atoms)")}
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        check("embedding" in cols, "embedding 列补上了")
        check("is_core" in cols, "is_core 列补上了")
        check(ver == SCHEMA_VERSION,
              "user_version 升到 %d（实际 %s）" % (SCHEMA_VERSION, ver))

        print("3. 索引与 FTS 是否补齐")
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'").fetchall()}
        check("idx_atoms_state_id" in idx, "新索引 idx_atoms_state_id 补上了")
        fts = conn.execute("SELECT name FROM sqlite_master WHERE name='atoms_fts'").fetchone()
        check(fts is not None, "atoms_fts 建好了")

        print("4. 旧数据还在，而且 FTS 索引真的重建了")
        st = db.stats()
        check(st.counts["l2_active"] >= 1, "旧原子还在（l2_active=%d）" % st.counts["l2_active"])
        # 不能只靠 recall：默认 enable_like_fallback=True，FTS 空着也能用 LIKE 兜回来，
        # 还有向量路 —— 那样「迁移时 rebuild 失败」这个回归根本测不出来。
        # 直接查 FTS5 MATCH，绕过两条兜底。
        rows = db.conn.execute(
            "SELECT rowid FROM atoms_fts WHERE atoms_fts MATCH ?", ('"WAL"',)
        ).fetchall()
        check(bool(rows), "atoms_fts 里能 MATCH 到旧原子（%d 行）" % len(rows))
        hits = db.recall("数据库统一用 WAL", top_k=3)
        check(bool(hits), "recall 也能拿到（%d 条）" % len(hits))

        print("5. 迁移后还能正常写入新数据")
        db.remember("s", "迁移之后新写入的一条记忆", fact_type="fact")
        st2 = db.stats()
        check(st2.counts["l2_active"] > st.counts["l2_active"],
              "新数据写得进去（%d -> %d）" % (st.counts["l2_active"], st2.counts["l2_active"]))
    finally:
        db.close()

    print("6. 未来版本的库要拒绝，不能瞎改")
    future = os.path.join(tmp, "future.db")
    conn = sqlite3.connect(future)
    conn.executescript(V1_SCHEMA)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    try:
        bm.MemoryDB(future)
        check(False, "user_version=99 应当拒绝")
    except SchemaVersionError:
        check(True, "user_version=99 抛 SchemaVersionError")

    print("7. 已移除的配置项要明确报错，不能静默无效")
    for key in ("commit_batch", "raise_on_llm_error",
                "max_candidates_per_ingest", "numpy_fallback_max_atoms"):
        try:
            MemoryConfig.resolve({key: 1})
            check(False, "%s 应当抛 ConfigError" % key)
        except ConfigError:
            check(True, "%s 抛 ConfigError" % key)

    print("8. extract_llm_* 回落：配了就优先，没配就用主 llm_*")
    cfg = MemoryConfig.resolve({"llm_endpoint": "http://main/v1", "llm_model": "main-model"})
    client = _LlmClient(cfg)
    check(client._ep == "http://main/v1" and client._model == "main-model",
          "只配 llm_* 时沿用主模型")
    cfg2 = MemoryConfig.resolve({"llm_endpoint": "http://main/v1", "llm_model": "main-model",
                                 "extract_llm_endpoint": "http://cheap/v1",
                                 "extract_llm_model": "cheap-model"})
    client2 = _LlmClient(cfg2)
    check(client2._ep == "http://cheap/v1" and client2._model == "cheap-model",
          "配了 extract_llm_* 时优先用便宜模型")

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
