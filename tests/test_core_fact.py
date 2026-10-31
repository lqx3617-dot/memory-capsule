#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心事实 is_core：冲突时新事实被拒，旧的不下线。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain_memory as bm

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def journal_actions(db):
    rows = db.conn.execute(
        "SELECT DISTINCT action FROM journal").fetchall()
    return {str(r[0]) for r in rows}


def state_of(db, atom_id):
    row = db.conn.execute(
        "SELECT state FROM atoms WHERE id = ?", (atom_id,)).fetchone()
    return str(row[0]) if row else ""


def main() -> int:
    db = bm.MemoryDB(":memory:")
    try:
        print("1. 建一条事实并标成核心")
        # 用「极性相反」的句子 —— 冲突检测只认两种：否定极性相反、数字改写。
        # 「名字是 A」vs「名字是 B」极性相同，不会被判冲突（那是另一个话题）。
        db.remember("s", "这个项目的配置必须走环境变量", fact_type="constraint", confidence=0.9)
        atom = db.facts(fact_types=["constraint"])[0]
        check(atom.is_core is False, "新原子默认不是核心")
        n = db.mark_core(atom.atom_id)
        check(n == 1, "mark_core 改了 1 行")
        again = db.facts(fact_types=["constraint"])[0]
        check(again.is_core is True, "标记后 is_core=True")

        print("2. 来一条冲突的新事实：应当被拒，旧的不下线")
        db.remember("s", "这个项目的配置不需要走环境变量", fact_type="constraint",
                    confidence=0.95)
        check(state_of(db, atom.atom_id) == "active", "核心事实仍是 active")
        active = db.facts(fact_types=["constraint"])
        check(len(active) == 1, "库里仍然只有这一条（新事实没进来，实际 %d）" % len(active))
        check("core_protect" in journal_actions(db), "journal 记了 core_protect")

        print("3. 取消核心标记后，覆盖恢复正常")
        db.mark_core(atom.atom_id, core=False)
        db.remember("s", "这个项目的配置不需要走环境变量", fact_type="constraint",
                    confidence=0.95)
        check(state_of(db, atom.atom_id) == "superseded", "旧原子被下线")
        active = db.facts(fact_types=["constraint"])
        check(len(active) == 1 and "不需要" in active[0].content,
              "新的那条在库里：%r" % active[0].content)
    finally:
        db.close()

    print("4. 非核心事实的冲突行为不变")
    db = bm.MemoryDB(":memory:")
    try:
        db.remember("s", "十点下班", fact_type="task", confidence=0.9)
        db.remember("s", "九点下班", fact_type="task", confidence=0.9)
        rows = db.conn.execute(
            "SELECT state FROM atoms ORDER BY id").fetchall()
        check([r[0] for r in rows] == ["superseded", "active"],
              "非核心：旧的被正常下线（%s）" % [r[0] for r in rows])
    finally:
        db.close()

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
