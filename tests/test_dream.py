#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""巩固 dream()：孤儿实体页要被清理，DREAMS.md 要能原子落盘。

原来 _persist 只 upsert 从不清理 —— 实体改名、或原子全被归档之后，
旧页会永远留在 entity_pages 和 DREAMS.md 里，越跑越多。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile

import brain_memory as bm

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def page_count(db):
    return int(db.conn.execute("SELECT COUNT(*) FROM entity_pages").fetchone()[0])


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="bm_dream_")
    path = os.path.join(tmp, "mem.db")
    db = bm.MemoryDB(path, {"dreams_path": os.path.join(tmp, "DREAMS.md")})
    try:
        print("1. 先巩固出实体页")
        db.remember("s", "brain_memory 项目的配置必须走环境变量", fact_type="constraint")
        db.remember("s", "brain_memory 项目的配置必须走环境变量，不能硬编码", fact_type="constraint")
        text = db.dream(persist=True)
        n1 = page_count(db)
        print("    [INFO] entity_pages = %d" % n1)
        check(n1 >= 1, "生成了实体页（%d）" % n1)
        check(text.startswith("# DREAMS"), "返回 Markdown")
        check(os.path.exists(os.path.join(tmp, "DREAMS.md")), "DREAMS.md 落盘了")

        print("2. 原子全归档后再巩固，孤儿页要被清掉")
        db.conn.execute("UPDATE atoms SET state = 'archived'")
        db.conn.commit()
        db.dream(persist=True)
        n2 = page_count(db)
        print("    [INFO] entity_pages = %d" % n2)
        check(n2 == 0, "孤儿实体页被清理（%d -> %d）" % (n1, n2))

        print("3. persist=False 不写库")
        db.conn.execute("UPDATE atoms SET state = 'active'")
        db.conn.commit()
        db.dream(persist=False)
        check(page_count(db) == 0, "persist=False 时库没变（仍为 0）")

        print("4. 全量巩固会把页再写回来")
        db.dream(persist=True)
        check(page_count(db) >= 1, "重新生成实体页（%d）" % page_count(db))

        print("5. DREAMS.md 不含临时文件残留")
        leftover = [f for f in os.listdir(tmp) if f.endswith(".tmp")]
        check(not leftover, "没有 .tmp 残留（%s）" % (leftover or "无"))
    finally:
        db.close()

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
