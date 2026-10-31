#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预算式召回：先保住长期偏好，余量再做语义检索。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain_memory as bm

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    db = bm.MemoryDB(":memory:", {"chars_per_token": 2.0})
    try:
        print("1. 灌偏好 + 约束 + 普通事实")
        db.remember("s", "爸爸不喜欢被叫您，要叫名字", fact_type="preference")
        db.remember("s", "这个项目所有配置必须走环境变量", fact_type="constraint")
        db.remember("s", "数据库统一用 WAL 模式", fact_type="decision")
        db.remember("s", "昨天在讨论检索排序的权重", fact_type="fact")

        print("2. 默认 2500 预算：PIN 在前，检索在后")
        items = db.recall_with_budget("检索排序", total_budget=2500, facts_budget=800)
        layers = [m.layer for m in items]
        print("    [INFO] layers = %s" % layers)
        check(len(items) >= 2, "至少拿到若干条（%d）" % len(items))
        check(layers[0] == "PIN", "第一条是 PIN")
        check("PIN" in layers, "PIN 段存在")
        # PIN 段必须连续在最前面
        first_non_pin = next((i for i, x in enumerate(layers) if x != "PIN"), len(layers))
        check(all(x == "PIN" for x in layers[:first_non_pin]),
              "PIN 段是连续的前缀")

        print("3. PIN 段里必须有偏好和约束")
        pinned_text = " ".join(m.content for m in items if m.layer == "PIN")
        check("您" in pinned_text, "偏好进了 PIN：%r" % pinned_text[:40])
        check("环境变量" in pinned_text, "约束进了 PIN")

        print("4. 去重：facts 里的不会在检索段重复出现")
        ids = [m.atom_id for m in items if m.atom_id]
        check(len(ids) == len(set(ids)), "没有重复 atom（%d 条）" % len(ids))

        print("5. 预算极小：只给 PIN，不炸")
        tiny = db.recall_with_budget("检索排序", total_budget=60, facts_budget=60)
        print("    [INFO] 极小预算返回 %d 条" % len(tiny))
        check(all(m.layer == "PIN" for m in tiny), "小预算下只出 PIN")
        total_tokens = sum(db._est_tokens(m.content) for m in tiny)
        check(total_tokens <= 60, "没超预算（估 %d token）" % total_tokens)

        print("6. facts_budget=0：退化成普通 recall")
        none_pin = db.recall_with_budget("检索排序", total_budget=2000, facts_budget=0)
        check(all(m.layer != "PIN" for m in none_pin),
              "facts_budget=0 时没有 PIN（%d 条）" % len(none_pin))

        print("7. 空 query：只回 PIN")
        only_pin = db.recall_with_budget("", total_budget=2000, facts_budget=800)
        check(all(m.layer == "PIN" for m in only_pin), "空 query 只回 PIN")
    finally:
        db.close()

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
