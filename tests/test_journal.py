#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L4 审计完整性：journal 里出现的每个 action 都必须在 types 里声明过。

交叉核对时发现两处缺口：
1. forget.apply 写的是 d.action（silent/archive），而 JOURNAL_ACTIONS 里写的是 silence —— 声明了一个从未产生的值；
2. promote 遇到冲突只改 state，不写 journal —— conflict 声明了却从未产生。

这个测试跑完整流程，然后反过来检查 journal 表里出现的 action 是否都在白名单内。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain_memory as bm
from brain_memory.types import JOURNAL_ACTIONS

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def journal_actions(db):
    rows = db.conn.execute("SELECT DISTINCT action FROM journal").fetchall()
    return {str(r[0]) for r in rows}


def main() -> int:
    db = bm.MemoryDB(":memory:")
    try:
        print("1. 灌数据，把各条路径都走一遍")
        db.ingest("s", "user", "我们决定统一用 WAL 模式")
        db.ingest("s", "user", "今天天气不错")            # gate_reject
        db.ingest("s", "no_such_role", "随便说点啥")      # role_unknown
        db.remember("s", "这个库的配置必须走环境变量", fact_type="constraint")

        print("2. 制造冲突")
        db.remember("s", "数据库统一用 SQLite 做存储", fact_type="decision", confidence=0.9)
        db.remember("s", "数据库不要 SQLite 做存储了", fact_type="decision", confidence=0.9)
        rows = db.conn.execute("SELECT id, state, supersedes_id FROM atoms ORDER BY id").fetchall()
        superseded = [r for r in rows if r[1] == "superseded"]
        print("   [INFO] atom 数=%d，superseded 数=%d" % (len(rows), len(superseded)))

        print("3. 遗忘落盘（silent）")
        atom = db.facts(fact_types=["constraint"])[0]
        db.feedback(atom.atom_id, "noise")
        db.forget(dry_run=False)

        print("4. 巩固 + 维护")
        db.dream(persist=False)
        db.maintenance("light")

        print("5. journal 里出现的 action 必须都已声明")
        seen = journal_actions(db)
        print("   [INFO] 出现: " + " ".join(sorted(seen)))
        undeclared = sorted(seen - set(JOURNAL_ACTIONS))
        check(not undeclared, "未声明的 action: " + (", ".join(undeclared) if undeclared else "无"))

        print("6. 冲突与静默都要留痕")
        check("silent" in seen, "silent 有记录（原来只会写 silent 但声明的是 silence）")
        check("conflict" in seen or not superseded,
              "发生冲突时要写 journal（superseded=%d，conflict 记录=%s）" % (len(superseded), "conflict" in seen))

        print("7. 声明过的动作名与状态名对齐")
        check("silence" not in JOURNAL_ACTIONS, "JOURNAL_ACTIONS 里不该再有 silence")
        check("silent" in JOURNAL_ACTIONS, "JOURNAL_ACTIONS 里有 silent")
    finally:
        db.close()

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
