#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发与生命周期：锁重入、嵌套事务、多进程共用库、并发 close。

这几类故障的共同点是「偶发」—— 出问题时日志里只有一句 LockTimeoutError，
看不出是别人占着锁，还是自己等自己。测试把它们变成确定的行为。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import subprocess
import tempfile
import threading

import brain_memory as bm
from brain_memory.errors import DatabaseError, LockOrderError
from brain_memory.locks import GlobalLock, WritePriorityRWLock

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    print("1. 嵌套写事务要立刻拒绝，而不是卡 5 秒锁超时")
    db = bm.MemoryDB(":memory:")
    try:
        try:
            with db._write_tx():
                with db._write_tx():
                    pass
            check(False, "嵌套写事务没被拒")
        except DatabaseError as exc:
            check("嵌套" in str(exc), "嵌套写事务被拒: %s" % exc)
    finally:
        db.close()

    print("2. 同一线程重复申请写锁要报锁序错误")
    for cls in (WritePriorityRWLock, GlobalLock):
        lock = cls()
        try:
            with lock.write():
                with lock.write():
                    pass
            check(False, "%s 写重入没被拒" % cls.__name__)
        except LockOrderError:
            check(True, "%s 写重入被拒（LockOrderError）" % cls.__name__)

    print("3. 两个进程并发 open 同一个新库并写入")
    tmp = tempfile.mkdtemp(prefix="bm_conc_")
    db_path = os.path.join(tmp, "multi.db")
    code = chr(10).join([
        "import sys",
        "sys.path.insert(0, %r)" % ROOT,
        "import brain_memory as bm",
        "db = bm.MemoryDB(%r)" % db_path,
        "for i in range(20):",
        "    db.remember('p', sys.argv[1] + ' 独有记忆 ' + str(i) + ' 号')",
        "db.close()",
    ])
    procs = []
    for tag in ("alpha", "beta"):
        procs.append((tag, subprocess.Popen(
            [sys.executable, "-c", code, tag],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)))
    crashed = []
    for tag, proc in procs:
        out, err = proc.communicate(timeout=120)
        if proc.returncode != 0:
            crashed.append("%s exit=%s %s" % (tag, proc.returncode, err.strip()[-200:]))
    check(not crashed, "两个进程都没崩" + ("" if not crashed else "：" + crashed[0]))

    db = bm.MemoryDB(db_path)
    try:
        n = db.stats().counts["l2_active"]
        print("    [INFO] 合并后 atoms = %d" % n)
        check(n >= 40, "两条进程各写 20 条，合并后 >= 40（实际 %d）" % n)
    finally:
        db.close()

    print("4. 多个线程同时 close 要幂等")
    db = bm.MemoryDB(":memory:")
    db.remember("s", "并发关闭测试用的一条记忆")
    errors = []

    def closer():
        try:
            db.close()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=closer) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=15)
    check(not errors, "4 个线程同时 close 没有异常" + ("" if not errors else "：" + str(errors[0])))
    check(db.is_closed, "最终处于已关闭状态")

    print("5. 关闭后再调用要抛 ClosedError，不能静默")
    try:
        db.ingest("s", "user", "关闭后的写入")
        check(False, "关闭后写入没被拒")
    except Exception as exc:
        check(type(exc).__name__ == "ClosedError", "关闭后写入抛 %s" % type(exc).__name__)

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
