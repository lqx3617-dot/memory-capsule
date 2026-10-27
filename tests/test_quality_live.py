#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""质量单一真源：recall / facts 的过滤必须用现算 quality，不是写入时的快照。

原实现里 atoms.quality = 写入时的 confidence，之后永不衰减，
所以 feedback=noise 的原子（实算 quality=0）依然能穿过 min_quality 栅栏。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain_memory as bm


def main() -> int:
    db = bm.MemoryDB(":memory:")
    try:
        db.remember("s", "数据库统一用 WAL 模式", fact_type="decision", confidence=0.9)
        atom = db.facts()[0]
        print("[INFO] 首次 facts: quality=%0.3f atom#%d" % (atom.quality, atom.atom_id))
        if atom.quality < 0.5:
            print("[FAIL] 现算 quality 应接近 1.0（feedback=1 / decay=1）")
            return 1

        db.feedback(atom.atom_id, "noise")          # feedback=0 -> 实算 quality=0
        quiet = db.recall("WAL 模式", min_quality=0.3)
        print("[INFO] feedback=noise 后 recall(min_quality=0.3) 命中 %d 条" % len(quiet))
        if quiet:
            print("[FAIL] 实算 quality=0 仍被返回 —— 过滤用的是陈旧快照")
            return 1
        print("[OK]   低质量原子被挡住")

        db.feedback(atom.atom_id, "used")           # feedback=1 -> 实算 quality ~1
        loud = db.recall("WAL 模式", min_quality=0.3)
        if not loud:
            print("[FAIL] feedback=used 后应重新可召回")
            return 1
        print("[OK]   反馈回升后重新可召回: %r" % loud[0].content)

        if not db.facts(min_quality=0.3):
            print("[FAIL] facts(min_quality=0.3) 应返回该原子")
            return 1
        print("[OK]   facts 走同一口径")
        print("\n全部通过")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())