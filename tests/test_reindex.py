#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向量重建 + convention 类型。

两件事：
1. reindex_vectors() 是公开方法，换 embedding 模型后调它即可，用户不必碰 db._writer；
2. convention（项目惯例）以前不在 FACT_TYPES 白名单里，门控能产出、remember() 却会抹成 fact，
   导致 facts(fact_types=["convention"]) 查不到 remember 写入的惯例。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain_memory as bm


def main() -> int:
    db = bm.MemoryDB(":memory:")
    try:
        db.remember("s", "爸爸最喜欢的框架是 PySide6", fact_type="preference")
        db.remember("s", "这个项目所有配置必须走环境变量", fact_type="constraint")

        print("1. reindex_vectors()")
        n = db.reindex_vectors()
        print("[INFO] 重建 %d 条" % n)
        if n < 2:
            print("[FAIL] 应至少重建 2 条")
            return 1
        print("[OK]   公开方法可用，不用碰私有属性")

        print("2. convention 类型")
        db.remember("s", "这个项目按规矩所有 PR 都要过 CI", fact_type="convention")
        rows = db.facts(fact_types=["convention"])
        if not rows:
            print("[FAIL] convention 被抹成 fact 了（FACT_TYPES 白名单漏了它）")
            return 1
        print("[OK]   facts 能按 convention 查到：%r" % rows[0].content[:40])

        print("3. 重建后向量仍可检索")
        hits = db.recall("框架", top_k=3)
        if not hits:
            print("[FAIL] 重建后检索为空")
            return 1
        print("[OK]   recall 命中 %d 条" % len(hits))
        print("\n全部通过")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
