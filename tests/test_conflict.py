#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冲突判据：既要抓住「数字被改写」，又不能误伤正常数据。

背景：为了修「十点下班 vs 九点下班 并存」的漏杀，加过一条「极性相同 + 数字不相交 + 句干相似」
的规则。结果两条随机内容（a1b2c3d4-alpha / e5f6a7b8-beta）去掉数字后都退化成 #a#b#c#d 的
同形骨架，被判成冲突 —— 40 条正常记忆里 32 条被下线。这个测试把两侧都钉住。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain_memory as bm

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def fresh():
    return bm.MemoryDB(":memory:")


def superseded_count(db):
    row = db.conn.execute(
        "SELECT COUNT(*) FROM atoms WHERE state = 'superseded'").fetchone()
    return int(row[0])


def main() -> int:
    print("1. 该抓的：数字改写的同一件事")
    db = fresh()
    try:
        db.remember("s", "十点下班", fact_type="task", confidence=0.9)
        db.remember("s", "九点下班", fact_type="task", confidence=0.9)
        check(superseded_count(db) == 1, "十点/九点被判为同一条事实的改写（superseded=%d）" % superseded_count(db))
    finally:
        db.close()

    print("2. 不该抓的：内容不同但都带数字")
    db = fresh()
    try:
        samples = [
            "a1b2c3d4e5f6a7b8-alpha",
            "e5f6a7b8c9d0e1f2-beta",
            "9f8e7d6c5b4a3f21-gamma",
            "1a2b3c4d5e6f7a8b-delta",
        ]
        for s in samples:
            db.remember("s", s, fact_type="fact", confidence=0.9)
        active = db.stats().counts["l2_active"]
        check(active == len(samples), "四条都还在（active=%d，期望 %d）" % (active, len(samples)))
        check(superseded_count(db) == 0, "没有一条被误判成冲突（superseded=%d）" % superseded_count(db))
    finally:
        db.close()

    print("3. 该抓的：极性相反且高度相似")
    db = fresh()
    try:
        db.remember("s", "数据库统一用 SQLite 做存储", fact_type="decision", confidence=0.9)
        db.remember("s", "数据库不用 SQLite 做存储", fact_type="decision", confidence=0.9)
        check(superseded_count(db) == 1, "极性相反的被判冲突（superseded=%d）" % superseded_count(db))
    finally:
        db.close()

    print("4. 不该抓的：只是长度相近的无关内容")
    db = fresh()
    try:
        db.remember("s", "周日加班到下午三点", fact_type="task", confidence=0.9)
        db.remember("s", "周五团建在下午六点", fact_type="task", confidence=0.9)
        check(superseded_count(db) == 0, "无关的两条不冲突（superseded=%d）" % superseded_count(db))
    finally:
        db.close()

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
