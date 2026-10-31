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

    print("3. 压缩确实让老记忆衰减更慢")
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=180)
    q = compute_quality(cfg=cfg, feedback=1.0, fact_type="fact",
                        last_active=old, now=now)
    raw_decay = 0.5 ** (180.0 / cfg.halflife_semantic_d)
    check(q > raw_decay,
          "180 天：压缩后 quality=%.3f > 未压缩 %.3f" % (q, raw_decay))
    check(q > 0.5, "180 天仍有 %.3f，不会被当成「忘了」" % q)

    print("4. 7 天内不受影响")
    fresh = now - timedelta(days=6)
    q6 = compute_quality(cfg=cfg, feedback=1.0, fact_type="fact",
                         last_active=fresh, now=now)
    raw6 = 0.5 ** (6.0 / cfg.halflife_semantic_d)
    check(near(q6, raw6, 1e-6), "6 天：压缩前后一致（%.4f）" % q6)

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
