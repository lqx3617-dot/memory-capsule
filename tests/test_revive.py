#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-1 复活：被打成 silent 的原子，用户再说一遍必须回到 active 并可召回。

关键：只改 state 会失败 —— forget 会用 compute_quality 重算再打回 silent。
本测试同时盯 state 和 recall 两个面。
"""
import sys

import brain_memory as bm

SENTENCE = "我们决定数据库统一用 WAL 模式"


def main() -> int:
    db = bm.MemoryDB(":memory:")
    try:
        db.ingest("s", "user", SENTENCE)
        atoms = db.facts()
        if not atoms:
            print("[FAIL] 首轮没进 L2")
            return 1
        atom_id = atoms[0].atom_id

        db.feedback(atom_id, "noise")           # feedback=0 -> quality=0
        db.forget(dry_run=False)                # 落 silent
        state = [a for a in db.forget()]
        print("[INFO] forget 决策: %s" % [(d["atom_id"], d["action"]) for d in state])
        if db.recall("WAL 模式"):
            print("[FAIL] 应该已经 silent 到召回为空")
            return 1
        print("[OK]   已静默，recall 为空")

        db.ingest("s", "user", SENTENCE)        # 原样重述 -> 去重合并 -> 复活
        hits = db.recall("WAL 模式")
        if not hits:
            print("[FAIL] silent 没有复活")
            return 1
        print("[OK]   复活并召回: %r" % hits[0].content)

        again = db.forget()                     # 再扫一遍，不许立刻又被打回
        if any(d["atom_id"] == atom_id for d in again):
            print("[FAIL] 复活后立刻又被判定为可遗忘（quality 没抬起来）")
            return 1
        print("[OK]   复活后稳定，未被再次静默")
        print("\n全部通过")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
