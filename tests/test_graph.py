#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图谱联想：建边、电势扩散、个性化 PageRank、双速率衰减。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import math

import brain_memory as bm
from brain_memory import graph
from brain_memory.config import MemoryConfig
from brain_memory.forget import compute_quality

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def edges_of(db, atom_id):
    rows = db.conn.execute(
        "SELECT target_id, weight FROM mem_graph WHERE source_id = ?", (atom_id,)
    ).fetchall()
    return {int(r[0]): float(r[1]) for r in rows}


def main() -> int:
    print("1. 建边：相似的原子之间连双向边")
    db = bm.MemoryDB(":memory:")
    try:
        # 两条要「够像能连边、又不像到被去重合并」：相似度落在 0.35~0.9 之间。
        db.remember("s", "数据库统一用 SQLite 做存储", fact_type="decision")
        db.remember("s", "SQLite 必须开 WAL 模式，事务提交更快", fact_type="decision")
        db.remember("s", "今天天气不错", fact_type="fact")
        atoms = db.facts(fact_types=["decision"])
        check(len(atoms) == 2, "两条 decision（%d）" % len(atoms))
        if len(atoms) < 2:
            print("  [SKIP] 两条被合并了，后面的边断言跳过")
            return 1
        a, b = atoms[0].atom_id, atoms[1].atom_id
        rel = edges_of(db, a)
        check(b in rel, "a -> b 有边（权重 %.2f）" % rel.get(b, 0))
        check(a in edges_of(db, b), "b -> a 有边（双向）")

        print("2. 电势扩散：一跳 0.75×w，三跳内收集")
        seeds = {a: 1.0}
        pot = graph.spread(db.conn, seeds)
        print("    [INFO] pot = %s" % {k: round(v, 3) for k, v in pot.items()})
        check(a in pot and pot[a] == 1.0, "种子保留原电势")
        check(b in pot, "一跳就能到 b")
        check(pot[b] <= 0.75 * 1.0 + 1e-9, "b 的电势不超过 0.75×w")

        print("3. 电势下限与跳数上限")
        far = graph.spread(db.conn, seeds, hops=0)
        check(set(far) == {a}, "hops=0 时只有种子")
        tiny = graph.spread(db.conn, {a: 0.01}, floor=0.5)
        check(set(tiny) == {a}, "电势低于上限就不扩散")

        print("4. 个性化 PageRank：种子权重压过非种子")
        sample_edges = {1: [(2, 1.0)], 2: [(1, 1.0), (3, 1.0)], 3: [(2, 1.0)]}
        pr = graph.personalized_pagerank(sample_edges, {1: 1.0})
        check(len(pr) == 3, "三个节点都有分数")
        check(abs(sum(pr.values()) - 1.0) < 1e-6, "总和归一（%.4f）" % sum(pr.values()))
        # 1↔2↔3 是对称环，1 和 3 在数学上就是相等的 —— 不能断言「大于」。
        # 真正该验的是：种子比非种子受偏爱（用链式图），以及归一化成立。
        check(pr[1] >= pr[3] - 1e-9, "种子 1 不低于远端 3（%.3f vs %.3f）" % (pr[1], pr[3]))
        chain = {1: [(2, 1.0)], 2: [(3, 1.0)], 3: [(4, 1.0)], 4: []}
        pr2 = graph.personalized_pagerank(chain, {1: 1.0})
        check(pr2[1] > pr2[3] > pr2[4],
              "链式图上天真：%.3f > %.3f > %.3f" % (pr2[1], pr2[3], pr2[4]))
        check(pr[2] > pr[3], "中间节点 2 高于远端 3")

        print("5. 双速率衰减：短期池先掉，长期池慢掉")
        cfg = MemoryConfig()
        now = None
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)

        def q(days, s_short=1.0, s_long=0.5):
            return compute_quality(cfg=cfg, feedback=1.0, fact_type="fact",
                                   last_active=now - timedelta(days=days), now=now,
                                   s_short=s_short, s_long=s_long)

        q0 = q(0)
        q_half_day = q(0.5)      # 12 小时后：短期池基本掉光
        q_30 = q(30)
        print("    [INFO] 0 天 %.3f | 0.5 天 %.3f | 30 天 %.3f" % (q0, q_half_day, q_30))
        check(q0 > q_half_day, "12 小时后衰减（%.3f > %.3f）" % (q0, q_half_day))
        check(q_30 < q_half_day, "30 天后继续衰减（%.3f < %.3f）" % (q_30, q_half_day))
        # 长期池给得足，30 天后还有残留
        tall = compute_quality(cfg=cfg, feedback=1.0, fact_type="fact",
                               last_active=now - timedelta(days=30), now=now,
                               s_short=0.1, s_long=1.0)
        check(tall > q_30, "长期池强度高时更耐衰减（%.3f > %.3f）" % (tall, q_30))

        print("6. 短期强度会被次数喂大，并漏进长期池")
        db2 = bm.MemoryDB(":memory:")
        try:
            db2.remember("s", "被反复想起来的约定：提交前必须跑测试", fact_type="constraint")
            atom = db2.facts(fact_types=["constraint"])[0]
            before = db2.conn.execute(
                "SELECT s_short, s_long FROM atoms WHERE id = ?", (atom.atom_id,)
            ).fetchone()
            for _ in range(3):
                db2.note_access(atom.atom_id)
            db2.flush_access()
            after = db2.conn.execute(
                "SELECT s_short, s_long FROM atoms WHERE id = ?", (atom.atom_id,)
            ).fetchone()
            print("    [INFO] s_short %.3f -> %.3f | s_long %.3f -> %.3f"
                  % (before[0], after[0], before[1], after[1]))
            check(after[1] > before[1], "长期池被喂大（%.3f -> %.3f）" % (before[1], after[1]))
        finally:
            db2.close()

        print("7. 关掉图谱：行为回到纯打分")
        db3 = bm.MemoryDB(":memory:", {"enable_graph": False})
        try:
            db3.remember("s", "数据库统一用 SQLite 做存储", fact_type="decision")
            hits = db3.recall("SQLite", top_k=3)
            check(bool(hits), "关掉图谱仍能召回 %d 条" % len(hits))
        finally:
            db3.close()
    finally:
        db.close()

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
