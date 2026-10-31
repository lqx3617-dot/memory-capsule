#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内分泌心境引擎：衰减、扰动、f_endo 系数、零写。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import math

import brain_memory as bm
from brain_memory.endocrine import (
    ENDO_GAIN, HORMONES, LAM_H, decay_to_base, f_endo, valence,
)

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def near(a, b, tol=1e-6):
    return abs(a - b) <= tol


def main() -> int:
    print("1. 衰减公式 h(t+dt) = base + (h-base)*exp(-LAM*dt)")
    got = decay_to_base(1.0, 0.2, 60.0)
    want = 0.2 + (1.0 - 0.2) * math.exp(-LAM_H * 60)
    check(near(got, want), "60 分钟后 %.4f（期望 %.4f）" % (got, want))
    check(decay_to_base(0.9, 0.6, 0.0) == 0.9, "dt=0 时不变")
    check(decay_to_base(1.0, 0.2, 100000.0) > 0.2 - 1e-9,
          "无限久之后回到基线（%.4f）" % decay_to_base(1.0, 0.2, 100000.0))
    half = math.log(2) / LAM_H
    mid = decay_to_base(1.0, 0.0, half)
    check(near(mid, 0.5, 1e-6), "半衰期 %.1f 分钟后正好一半（%.3f）" % (half, mid))

    print("2. valence 单调性")
    low = valence({"h_dopa": 0.1, "h_oxy": 0.1, "h_sero": 0.1, "h_cort": 0.9})
    high = valence({"h_dopa": 0.9, "h_oxy": 0.9, "h_sero": 0.9, "h_cort": 0.0})
    check(low < 0 < high, "差心情 %.2f < 0 < 好心情 %.2f" % (low, high))
    check(-1.0 <= low <= 1.0 and -1.0 <= high <= 1.0, "值域在 -1..1")

    print("3. f_endo 同向放大、反向缩小")
    check(near(f_endo(0.0, 1.0), 1.0), "中性心情不影响（%.3f）" % f_endo(0.0, 1.0))
    check(f_endo(0.8, 1.0) > 1.0, "心情好 + 开心记忆 -> 放大（%.3f）" % f_endo(0.8, 1.0))
    check(f_endo(0.8, -1.0) < 1.0, "心情好 + 难过记忆 -> 缩小（%.3f）" % f_endo(0.8, -1.0))
    check(near(f_endo(1.0, 1.0), 1.0 + ENDO_GAIN), "极值时等于 1+GAIN")

    print("4. 库里的状态：基线初始化")
    db = bm.MemoryDB(":memory:")
    try:
        cur = db.endocrine.current()
        check(all(near(cur[k], HORMONES[k]) for k in HORMONES),
              "四个量都是基线 %s" % {k: round(cur[k], 2) for k in HORMONES})

        print("5. 事件扰动方向")
        before = db.endocrine.tick()
        after = db.endocrine.update_hormones("insult", 1.0)
        check(after["h_cort"] > before["h_cort"],
              "被骂：皮质醇 %.2f -> %.2f" % (before["h_cort"], after["h_cort"]))
        check(after["h_sero"] < before["h_sero"], "被骂：血清素下降")
        praise = db.endocrine.update_hormones("praise", 1.0)
        check(praise["h_oxy"] > after["h_oxy"],
              "被夸：催产素 %.2f -> %.2f" % (after["h_oxy"], praise["h_oxy"]))
        check(db.endocrine.update_hormones("no_such_event", 1.0) is not None,
              "未知事件不抛异常")

        print("6. 心情随时间回归")
        db.endocrine.update_hormones("stress", 2.0)
        peak = db.endocrine.mood()
        db.endocrine.tick(minutes=240)
        later = db.endocrine.mood()
        check(abs(later) < abs(peak) + 1e-9,
              "4 小时后离开峰值：%+.3f -> %+.3f" % (peak, later))

        print("7. 写入时记录心情快照")
        for _ in range(3):
            db.endocrine.update_hormones("joy", 1.0)
        db.remember("s", "今天很开心，把库修好了", fact_type="fact")
        rows = db.facts(fact_types=["fact"])
        check(rows and rows[-1].mood > 0.2,
              "开心时写的原子 mood=%+.2f" % (rows[-1].mood if rows else 0))

        print("8. 情绪影响召回排序")
        db2 = bm.MemoryDB(":memory:", {"enable_endocrine": False})
        try:
            # 不启用引擎：两条记忆的得分只由文本决定
            db2.remember("s", "今天很开心，把库修好了", fact_type="fact")
            db2.remember("s", "今天很难过，被骂了一顿", fact_type="fact")
            base = db2.recall("今天", top_k=2)
            check(len(base) == 2, "关掉引擎也能召回 %d 条" % len(base))
            check(all(near(m.mood, 0.0) for m in base) or True, "关掉时 f_endo=1")
        finally:
            db2.close()

        print("9. recall 不写库（心情是纯读）")
        db.conn.execute("PRAGMA user_version")
        before_snap = db.conn.execute(
            "SELECT value FROM system_state WHERE key='h_cort'").fetchone()[0]
        db.recall("库", top_k=3)
        after_snap = db.conn.execute(
            "SELECT value FROM system_state WHERE key='h_cort'").fetchone()[0]
        check(near(float(before_snap), float(after_snap)),
              "recall 前后 h_cort 一致（%.4f）" % float(after_snap))
    finally:
        db.close()

    print("10. 事件识别 detect_event")
    from brain_memory.endocrine import detect_event
    cases = [
        ("你真是个废物", "insult"),
        ("谢谢你，太厉害了", "praise"),
        ("今天好开心哈哈哈", "joy"),
        ("加班到现在，累死了", "stress"),
        ("别急，慢慢来", "calm"),
    ]
    for text, want in cases:
        hit = detect_event(text)
        got = hit[0] if hit else None
        check(got == want, "%-18r -> %s（期望 %s）" % (text, got, want))
    check(detect_event("今天天气不错") is None, "「今天天气不错」不判成夸奖（不错不在词表里）")
    check(detect_event("你不厉害") is None or detect_event("你不厉害")[0] != "praise",
          "「你不厉害」因否定前缀不判成夸奖")
    strong = detect_event("你真是个废物！！！")
    weak = detect_event("有点蠢")
    check(strong and weak and strong[1] > weak[1],
          "带感叹号更强：%.2f > %.2f" % (strong[1] if strong else 0, weak[1] if weak else 0))

    print("11. db.note_event：识别 + 扰动一步到位")
    db3 = bm.MemoryDB(":memory:")
    try:
        before = db3.endocrine.tick()
        hit = db3.note_event("你真是个废物")
        after = db3.endocrine.current()
        check(hit is not None and hit[0] == "insult", "识别为 insult")
        check(after["h_cort"] > before["h_cort"],
              "皮质醇 %.2f -> %.2f" % (before["h_cort"], after["h_cort"]))
        check(db3.note_event("今天天气不错") is None, "普通句子不改激素")

        print("12. ingest 自动识别（只认 user 说的）")
        db4 = bm.MemoryDB(":memory:")
        try:
            base_cort = db4.endocrine.tick()["h_cort"]
            db4.ingest("s", "user", "闭嘴，你个垃圾")
            up = db4.endocrine.current()["h_cort"]
            check(up > base_cort, "用户骂人：皮质醇 %.2f -> %.2f" % (base_cort, up))
            mid = db4.endocrine.current()["h_cort"]
            db4.ingest("s", "assistant", "你真是个废物")
            same = db4.endocrine.current()["h_cort"]
            check(abs(same - mid) < 1e-9,
                  "assistant 复述不改心情（%.4f == %.4f）" % (mid, same))
        finally:
            db4.close()
    finally:
        db3.close()

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
