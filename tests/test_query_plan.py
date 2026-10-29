#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询计划：关键查询不许退化成全表扫。

静态看代码判断不了 SQLite 会走哪条路 —— 统计信息、数据量、索引顺序都会影响。
这个测试灌够数据、跑 ANALYZE，然后直接问 EXPLAIN QUERY PLAN。

断言的尺子故意放松（只要求「不是裸 SCAN」），因为计划会随 SQLite 版本变；
但完整计划会打印出来，退化时肉眼能看见。
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


def plan(conn, sql, params=()):
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return [str(r[3]) for r in rows]


def show(title, lines):
    print("    " + title + ": " + " | ".join(lines))

def main() -> int:
    db = bm.MemoryDB(":memory:")
    try:
        print("1. 灌数据并 ANALYZE")
        # 不能用 remember()：400 条相似文本会被去重并成一条。
        # 直接写表 + FTS，只为撑出像样的查询计划。
        with db._write_tx() as conn:
            for i in range(400):
                text = "查询计划样本 %d 号 %s" % (i, "abcde"[i % 5] * (i % 4 + 1))
                cur = conn.execute(
                    "INSERT INTO atoms(fact_type, content, confidence, quality, access_count, "
                    "created_at, state, feedback) "
                    "VALUES ('fact', ?, 0.6, 0.6, 0, '2026-10-29T00:00:00Z', 'active', 1.0)",
                    (text,),
                )
                conn.execute("INSERT INTO atoms_fts(rowid, content) VALUES (?, ?)",
                             (int(cur.lastrowid), text))
        db.conn.execute("ANALYZE")
        print("    atoms 行数 = %d" % db.stats().counts["l2_active"])

        print("2. forget 扫描（state IN + ORDER BY id）")
        p = plan(db.conn,
                 "SELECT id, content, quality, access_count, feedback, fact_type, "
                 "created_at, last_accessed_at, state FROM atoms "
                 "WHERE state IN ('active','silent','archived') ORDER BY id")
        show("计划", p)
        # state IN ('active','silent','archived') 覆盖库里绝大多数行，
        # 全表扫本来就是合理选择，不该硬断言走索引。真正要守住的是「不要额外排序」。
        check(not any("TEMP B-TREE" in s for s in p),
              "不需要额外排序（idx_atoms_state_id 生效）")

        print("3. supersedes 引用计数")
        p = plan(db.conn,
                 "SELECT supersedes_id, COUNT(*) FROM atoms "
                 "WHERE supersedes_id IS NOT NULL GROUP BY supersedes_id")
        show("计划", p)
        check(any("idx_atoms_supersedes" in s for s in p),
              "命中 idx_atoms_supersedes")

        print("4. dream 取高质量原子")
        p = plan(db.conn,
                 "SELECT id, content FROM atoms WHERE state = 'active' "
                 "ORDER BY quality DESC LIMIT 10")
        show("计划", p)
        check(any("idx_atoms_state_quality" in s for s in p),
              "命中 idx_atoms_state_quality（反向扫，无需排序）")

        print("5. promote 去重：同 fact_type 最近 50 条")
        p = plan(db.conn,
                 "SELECT id, content FROM atoms WHERE fact_type = ? "
                 "AND state IN ('active','silent') ORDER BY id DESC LIMIT 50",
                 ("fact",))
        show("计划", p)
        check(any("idx_atoms_fact_type" in s or "idx_atoms_state" in s for s in p),
              "命中 fact_type 或 state 索引")

        print("6. stats 的平均质量（覆盖索引，不必回表）")
        p = plan(db.conn,
                 "SELECT AVG(quality), AVG(confidence) FROM atoms WHERE state = 'active'")
        show("计划", p)
        # state_quality 是覆盖索引、state_id 不是；SQLite 认为差不多就会任选其一，
        # 所以只要求「走 state 前缀索引」，不锁死具体是哪个。
        check(any("idx_atoms_state" in s for s in p),
              "命中 state 前缀索引")
    finally:
        db.close()

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
