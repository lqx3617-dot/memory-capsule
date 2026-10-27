#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-2/3/4 connectivity：被召回越多，质量衰减越慢。

原实现里 refs 统计的是 supersedes_id 引用数，而被引用者都是 superseded、
根本进不了 forget 扫描，所以 connectivity 恒为 1 —— 因子是死的。
"""
import sys
from datetime import datetime, timedelta, timezone

from brain_memory.config import MemoryConfig
from brain_memory.forget import compute_quality


def main() -> int:
    cfg = MemoryConfig()
    old = datetime.now(timezone.utc) - timedelta(days=120)
    base = compute_quality(cfg=cfg, feedback=1.0, fact_type="fact", last_active=old)
    hot = compute_quality(
        cfg=cfg, feedback=1.0, fact_type="fact", last_active=old, access_count=7
    )
    print("[INFO] 冷=%0.4f 热=%0.4f" % (base, hot))
    if not hot > base:
        print("[FAIL] connectivity 仍然是死的（access_count 不影响 quality）")
        return 1
    if hot > 1.0:
        print("[FAIL] quality 溢出 1.0")
        return 1
    print("[OK]   connectivity 生效且被 clamp 在 1.0 以内")
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
