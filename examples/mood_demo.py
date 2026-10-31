#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内分泌心境引擎 · 对话主流程演示。

跑法：python3 examples/mood_demo.py

演示四件事：
1. 骂一句，皮质醇上去、心情变差
2. 夸一句，催产素上去、心情变好
3. 两种心情下写记忆，各自带上当时的心情快照
4. 心情好时 recall 更容易想起开心的事（同一个查询，排序会变）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain_memory as bm


def show(tag, db):
    print("  %-10s %s" % (tag, db.endocrine.describe()))


def main() -> int:
    db = bm.MemoryDB(":memory:")
    try:
        print("== 0. 初始状态（基线）==")
        show("醒来", db)

        print("\n== 1. 用户骂人 ==")
        db.endocrine.update_hormones("insult", 1.0)
        show("被骂后", db)

        print("\n== 2. 用户夸奖 ==")
        db.endocrine.update_hormones("praise", 1.0)
        show("被夸后", db)

        print("\n== 3. 心情好时写一条开心的记忆 ==")
        for _ in range(2):
            db.endocrine.update_hormones("joy", 1.0)
        happy_mood = db.endocrine.mood()
        show("此刻", db)
        db.remember("s", "今天和爸爸一起把库修好了，很开心", fact_type="fact")
        print("  写入的原子 mood=%+.2f" % happy_mood)

        print("\n== 4. 心情差时写一条难过的记忆 ==")
        for _ in range(3):
            db.endocrine.update_hormones("insult", 1.0)
        sad_mood = db.endocrine.mood()
        show("此刻", db)
        db.remember("s", "今天被骂了一顿，很难过", fact_type="fact")
        print("  写入的原子 mood=%+.2f" % sad_mood)

        print("\n== 5. 心情好时检索：开心的排前面 ==")
        db.endocrine.update_hormones("joy", 1.0)
        db.endocrine.update_hormones("praise", 1.0)
        show("此刻", db)
        hits = db.recall("今天", top_k=3)
        for i, m in enumerate(hits, 1):
            print("  %d. score=%.3f mood=%+.2f  %s"
                  % (i, m.score, m.mood, m.content[:28]))

        print("\n== 6. 时间过去 3 小时，激素回归基线 ==")
        db.endocrine.tick(minutes=180)
        show("3 小时后", db)

        print("\n== 7. 关掉引擎：f_endo 恒为 1.0 ==")
        db2 = bm.MemoryDB(":memory:", {"enable_endocrine": False})
        try:
            show("关闭后", db2)
            print("  mood 读出：%+.3f（关掉时恒为 0）" % db2._mood_now())
        finally:
            db2.close()

        print("\n演示结束。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
