#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时间压缩 dt_eff：真实年龄 → 等效天数，再代入衰减。

| 真实年龄 | dt_eff |
|---|---|
| < 7 天 | 原样 |
| 7~30 天 | 7 + (age-7) × 0.5 |
| > 30 天 | 18.5 + (age-30) × 0.15 |
| 任何情况 | 不超过 age_compress_cap_d（默认 90） |
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone

from brain_memory.config import MemoryConfig
from brain_memory.forget import compress_age_days, compute_quality

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def near(a, b, tol=1e-6):
    return abs(a - b) <= tol


def main() -> int:
    cfg = MemoryConfig()
    print("1. 四个区间的分段结果")
    cases = [
        (0.0, 0.0, "刚写入"),
        (6.9, 6.9, "7 天内原样"),
        (7.0, 7.0, "第一拐点"),
        (15.0, 7.0 + 8.0 * 0.5, "7~30 天：对折"),
        (30.0, 18.5, "第二拐点"),
        (90.0, 18.5 + 60.0 * 0.15, "30 天以上：15%"),
        (365.0, 18.5 + 335.0 * 0.15, "一年"),
    ]
    for age, want, label in cases:
        got = compress_age_days(age, cfg)
        check(near(got, want, 1e-6),
              "%-18s age=%-6.1f -> %.2f（期望 %.2f）" % (label, age, got, want))

    print("2. 上限封顶")
    huge = compress_age_days(100000.0, cfg)
    check(near(huge, cfg.age_compress_cap_d),
          "极老记忆封顶到 %.0f（实际 %.2f）" % (cfg.age_compress_cap_d, huge))

    print("3. 压缩让老记忆衰减更慢（双速率下的对比）")
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=180)
    q = compute_quality(cfg=cfg, feedback=1.0, fact_type="fact",
                        last_active=old, now=now)
    # 双速率下：短期池早掉光，长期池 s_long=0.5 按半衰期掉。
    # 若不做时间压缩，长期池 180 天只剩 0.5^(180/90)=0.25 -> 0.6*0.5*0.25=0.075。
    raw = cfg.w_long * 0.5 * (0.5 ** (180.0 / cfg.halflife_semantic_d))
    check(q > raw, "180 天：压缩后 %.3f > 纯半衰期 %.3f" % (q, raw))
    check(q > 0.15, "180 天仍有 %.3f，不会被当成「忘了」" % q)

    print("4. 6 天：短期池已掉光，长期池主导")
    fresh = now - timedelta(days=6)
    q6 = compute_quality(cfg=cfg, feedback=1.0, fact_type="fact",
                         last_active=fresh, now=now)
    q0 = compute_quality(cfg=cfg, feedback=1.0, fact_type="fact",
                         last_active=now, now=now)
    check(q6 < q0, "6 天比刚写入低（%.3f < %.3f）" % (q6, q0))
    check(q6 > 0.2, "6 天仍有 %.3f，长期池撑着" % q6)
    # 短期池的时间常数决定了「12 小时后基本掉光」
    q_12h = compute_quality(cfg=cfg, feedback=1.0, fact_type="fact",
                            last_active=now - timedelta(hours=12), now=now)
    check(q_12h < q0 * 0.9, "12 小时后短期池衰减明显（%.3f < %.3f）" % (q_12h, q0))

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
