#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""索引存在性：schema.sql 定义了什么，建出来的库里就得有什么。

为什么不是「查询计划测试」：试过用 EXPLAIN QUERY PLAN 断言关键查询走某条索引，
但 SQLite 的选择依赖数据分布 —— 400 条全是 active 时它合理地选全表扫，
断言会红，但代码没毛病。那种测试只能产出噪音。

这里只守一件确定的事：索引本身在不在。走不走由优化器按数据决定。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import re
import brain_memory as bm

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 每类索引负责哪条查询（写在这里，改查询时能对照）
EXPECTED = {
    "idx_atoms_state_quality": "dream 取高质量原子、stats 求平均（覆盖索引）",
    "idx_atoms_state_id": "forget 扫描：state 过滤 + ORDER BY id 不再额外排序",
    "idx_atoms_fact_type": "promote 去重 / 冲突检测：同 fact_type 最近 N 条",
    "idx_atoms_supersedes": "forget 的引用计数 GROUP BY supersedes_id",
    "idx_atoms_entity": "dream 按实体分组（Python 侧分组，索引当前未被查询命中）",
    "idx_atoms_created": "forget 的 older_than_days 过滤",
    "idx_candidates_status": "promote_pending 取 pending",
    "idx_raw_events_session": "按会话回溯",
}

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    with open(os.path.join(HERE, "brain_memory", "schema.sql"), "r", encoding="utf-8") as fh:
        ddl = fh.read()
    declared = set(re.findall(r"CREATE INDEX IF NOT EXISTS (\w+)", ddl))

    db = bm.MemoryDB(":memory:")
    try:
        actual = {
            str(r[0])
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
            ).fetchall()
        }
        print("1. schema.sql 声明 %d 个索引，建库后实有 %d 个" % (len(declared), len(actual)))
        missing = sorted(declared - actual)
        check(not missing, "声明的都在（缺: " + (", ".join(missing) if missing else "无") + "）")

        print("2. _ensure_indexes 补的索引也在（老库升级路径）")
        check("idx_atoms_state_id" in actual, "idx_atoms_state_id 存在")

        print("3. 关键索引与查询的对应关系")
        for name, why in EXPECTED.items():
            check(name in actual, "%s —— %s" % (name, why))
    finally:
        db.close()

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
