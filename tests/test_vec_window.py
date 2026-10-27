#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-2 向量候选窗口：超过上限条数后，最新记忆仍必须进候选。

原实现 LIMIT 无 ORDER BY，SQLite 按 rowid 取「最老的那批」，
库一过 pure_fallback_max_atoms 就永久失忆 —— 本测试用 cap=3 + 8 条原子钉死它。
"""
import sqlite3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain_memory.config import MemoryConfig
from brain_memory.vec import HashEmbedder, VectorStore, pack_vector


def main() -> int:
    cfg = MemoryConfig.resolve({"pure_fallback_max_atoms": 3})
    embedder = HashEmbedder(dim=64)
    store = VectorStore(cfg, embedder)
    store.backend = "pure"

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE atoms(id INTEGER PRIMARY KEY, state TEXT, embedding BLOB, content TEXT)"
    )
    for i in range(1, 9):
        text = "填充记忆 %d 号" % i
        conn.execute(
            "INSERT INTO atoms(id, state, embedding, content) VALUES (?,?,?,?)",
            (i, "active", pack_vector(embedder.embed([text])[0]), text),
        )
    conn.execute("INSERT INTO atoms(id, state, embedding, content) VALUES (9,'active',?,?)",
                 (pack_vector(embedder.embed(["爸爸最喜欢的框架是 PySide6"])[0]),
                  "爸爸最喜欢的框架是 PySide6"))

    pairs = store.search(conn, "最喜欢的框架", 3)
    ids = [a for a, _ in pairs]
    print("[INFO] 候选: %s" % pairs)
    if 9 not in ids:
        print("[FAIL] 最新原子 id=9 不在候选窗内 —— 大库静默失忆")
        return 1
    print("[OK]   最新原子进入了候选窗")
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
