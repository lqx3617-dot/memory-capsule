#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双速率强度：合并命中的按次递推 + dream 打分读库内强度。

两条都是评审抓出来的真 bug，且都只在「有召回历史」之后才显形：

1. `note_access()` 把同一原子的多次命中合并成一个 `times` 计数，
   而 flush 只更新了一次 —— 「3 秒内命中 10 次」和「命中 1 次」对长期池的贡献一样。
2. `dream()` 的 SELECT 没带 `s_short` / `s_long`，打分永远用默认值 1.0 / 0.5 ——
   召回得再多，巩固时也看不出来。

这里把两件事都钉成确定行为：先给数值，再给等价性（合并 N 次 == 分 N 次 flush）。
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


def strengths(db, atom_id):
    row = db.conn.execute(
        "SELECT s_short, s_long FROM atoms WHERE id = ?", (atom_id,)
    ).fetchone()
    return float(row[0]), float(row[1])


def one_atom(db, content, fact_type="fact"):
    db.remember("s", content, fact_type=fact_type)
    return db.facts(fact_types=[fact_type])[0].atom_id


def main() -> int:
    print("1. 一次 flush 里合并的 3 次命中，要按 3 次累加 s_short")
    db = bm.MemoryDB(":memory:")
    try:
        aid = one_atom(db, "召回计数要按次数累加短期强度")
        # 刚写入的 s_short 就是 1.0（在饱和点上），直接加看不出「按次」和「只加一次」的
        # 区别 —— 先压到 0.2，3 次 ×0.15 = 0.65，正好落在不饱和区间里。
        db.conn.execute(
            "UPDATE atoms SET s_short = 0.2, s_long = 0.5 WHERE id = ?", (aid,)
        )
        db.conn.commit()
        s0, l0 = strengths(db, aid)
        gain = float(db.config.stm_gain)
        to_ltm = float(db.config.stm_to_ltm)
        db.note_access(aid, times=3)
        db.flush_access()
        s1, l1 = strengths(db, aid)
        expect_s = min(1.0, s0 + 3 * gain)
        expect_l = min(1.0, l0 + to_ltm * (s0 + (s0 + gain) + (s0 + 2 * gain)))
        check(abs(s1 - expect_s) < 1e-9,
              "s_short = %.4f（期望 %.4f）" % (s1, expect_s))
        check(abs(l1 - expect_l) < 1e-9,
              "s_long = %.6f（期望 %.6f）" % (l1, expect_l))
        check(s1 > s0 and l1 > l0,
              "两个池都涨了（%.4f>%.4f, %.4f>%.4f）" % (s1, s0, l1, l0))
    finally:
        db.close()

    print("2. 合并 3 次 ≈ 分 3 次 flush（逐次递推等价）")
    db_a = bm.MemoryDB(":memory:")
    db_b = bm.MemoryDB(":memory:")
    try:
        a1 = one_atom(db_a, "同一句记忆用于比较合并与分开刷盘")
        a2 = one_atom(db_b, "同一句记忆用于比较合并与分开刷盘")
        # 同样先压到不饱和区间，否则两边都顶在 1.0，比了等于没比。
        for target, atom_id in ((db_a, a1), (db_b, a2)):
            target.conn.execute(
                "UPDATE atoms SET s_short = 0.4, s_long = 0.3 WHERE id = ?", (atom_id,)
            )
            target.conn.commit()
        db_a.note_access(a1, times=3)
        db_a.flush_access()
        s_batch, l_batch = strengths(db_a, a1)
        for _ in range(3):
            db_b.note_access(a2, times=1)
            db_b.flush_access()
        s_sep, l_sep = strengths(db_b, a2)
        check(abs(s_batch - s_sep) < 1e-9,
              "s_short 一致（%.6f vs %.6f）" % (s_batch, s_sep))
        check(abs(l_batch - l_sep) < 1e-6,
              "s_long 一致（%.6f vs %.6f）" % (l_batch, l_sep))
    finally:
        db_a.close()
        db_b.close()

    print("3. dream 打分要读库里的 s_short / s_long")
    db = bm.MemoryDB(":memory:")
    try:
        text = "巩固时必须读库里真实强度的一条记忆"
        aid = one_atom(db, text)
        db.conn.execute(
            "UPDATE atoms SET s_short = 0.0, s_long = 0.0 WHERE id = ?", (aid,)
        )
        db.conn.commit()
        out = db.dream(persist=False)
        check(text not in out, "强度归零的原子被 dream 排除")
        db.conn.execute(
            "UPDATE atoms SET s_short = 1.0, s_long = 0.5 WHERE id = ?", (aid,)
        )
        db.conn.commit()
        out2 = db.dream(persist=False)
        check(text in out2, "强度正常的原子进了 dream")
    finally:
        db.close()

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
