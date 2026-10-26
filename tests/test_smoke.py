import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain_memory as bm

FAILS = []


def step(name, fn):
    try:
        fn()
        print("[OK]  " + name)
    except Exception:
        FAILS.append(name)
        print("[FAIL] " + name)
        traceback.print_exc()


def main():
    tmp = tempfile.mkdtemp(prefix="bm_smoke_")
    dbpath = os.path.join(tmp, "nested", "mem.db")

    print("=== 1. 初始化 ===")
    db = bm.MemoryDB(dbpath)
    print("journal_mode =", db.journal_mode)
    print("sync_mode    =", db.sync_mode)
    print("fts_tokenizer=", db.fts_tokenizer)
    print("vector       =", db.vector_backend)
    print("db file      =", os.path.exists(dbpath))

    print("=== 2. 重复初始化（幂等） ===")
    db2 = bm.MemoryDB(dbpath)
    print("second journal_mode =", db2.journal_mode)
    db2.close()

    print("=== 3. stats ===")
    print(db.stats().as_dict())

    print("=== 4. 写事务 + 读连接 ===")
    with db._write_tx() as conn:
        cur = conn.execute(
            "INSERT INTO raw_events(session_id, role, content, created_at) VALUES (?,?,?,?)",
            ("s1", "user", "以后数据库统一用 WAL 模式", bm.utc_now()),
        )
        eid = cur.lastrowid
    print("event_id =", eid)
    with db._read_conn() as conn:
        row = conn.execute("SELECT id, content FROM raw_events").fetchone()
        print("read back:", tuple(row))

    print("=== 5. atoms + 访问计数 flusher ===")
    with db._write_tx() as conn:
        cur = conn.execute(
            "INSERT INTO atoms(fact_type, content, confidence, created_at, source_event_id) "
            "VALUES (?,?,?,?,?)",
            ("constraint", "数据库统一用 WAL 模式", 0.9, bm.utc_now(), eid),
        )
        aid = cur.lastrowid
        conn.execute("INSERT INTO atoms_fts(rowid, content) VALUES (?, ?)", (aid, "数据库统一用 WAL 模式"))
    db.note_access(aid)
    db.note_access(aid)
    print("pending before flush =", db.pending_access())
    print("flushed rows =", db.flush_access())
    with db._read_conn() as conn:
        row = conn.execute("SELECT access_count, last_accessed_at FROM atoms WHERE id=?", (aid,)).fetchone()
        print("access_count =", row[0], "last =", row[1])

    print("=== 6. FTS 检索 ===")
    with db._read_conn() as conn:
        rows = conn.execute("SELECT rowid, content FROM atoms_fts WHERE atoms_fts MATCH ?", ('"WAL"',)).fetchall()
        print("fts hits =", [(r[0], r[1]) for r in rows])
    if not rows:
        raise AssertionError("FTS 未召回")

    print("=== 7. 并发：2 线程各 50 写 + 50 读 ===")
    errs = []

    def worker(tag):
        try:
            for i in range(50):
                with db._write_tx() as conn:
                    conn.execute(
                        "INSERT INTO raw_events(session_id, role, content, created_at) VALUES (?,?,?,?)",
                        ("s2", "user", "并发写入 %d-%d" % (tag, i), bm.utc_now()),
                    )
                with db._read_conn() as conn:
                    conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()
        except Exception as exc:
            errs.append(exc)

    t1 = threading.Thread(target=worker, args=(1,))
    t2 = threading.Thread(target=worker, args=(2,))
    started = time.monotonic()
    t1.start(); t2.start(); t1.join(); t2.join()
    print("并发耗时 %.1f ms, 错误数 %d" % ((time.monotonic() - started) * 1000, len(errs)))
    if errs:
        raise errs[0]
    s = db.stats()
    print("l0_events =", s.counts["l0_events"])
    if s.counts["l0_events"] != 101:
        raise AssertionError("事件数不对: %d" % s.counts["l0_events"])

    print("=== 8. maintenance ===")
    print(db.maintenance("normal"))

    print("=== 9. close 幂等 + 关闭后调用 ===")
    db.close()
    db.close()
    try:
        db.stats()
    except bm.ClosedError:
        print("ClosedError 正确抛出")
    else:
        raise AssertionError("close 后调用 stats 未抛 ClosedError")

    print("=== 10. DELETE 模式（模拟 WAL 不可用） ===")
    dpath = os.path.join(tmp, "delete_mode.db")
    dbd = bm.MemoryDB(dpath, {"journal_mode_target": "DELETE"})
    print("journal_mode =", dbd.journal_mode, "sync_mode =", dbd.sync_mode)
    assert dbd.journal_mode == "delete", dbd.journal_mode
    assert dbd.sync_mode == "global", dbd.sync_mode
    with dbd._write_tx() as conn:
        conn.execute(
            "INSERT INTO raw_events(session_id, role, content, created_at) VALUES (?,?,?,?)",
            ("d1", "user", "delete 模式写入", bm.utc_now()),
        )
    print("delete 模式 l0_events =", dbd.stats().counts["l0_events"])
    dbd.close()

    print("=== 11. :memory: ===")
    dbm = bm.MemoryDB(":memory:")
    print("journal_mode =", dbm.journal_mode, "sync_mode =", dbm.sync_mode)
    with dbm._write_tx() as conn:
        conn.execute(
            "INSERT INTO raw_events(session_id, role, content, created_at) VALUES (?,?,?,?)",
            ("m1", "user", "内存库", bm.utc_now()),
        )
    print("l0_events =", dbm.stats().counts["l0_events"])
    dbm.close()

    print("=== 12. 配置校验 ===")
    for bad in ({"read_pool_size": 9}, {"journal_mode_target": "xxx"}, {"no_such_key": 1}, {"silent_quality": 0.9}):
        try:
            inst = bm.MemoryDB(":memory:", bad)
        except bm.ConfigError as exc:
            print("ConfigError OK:", str(exc)[:60])
        else:
            inst.close()
            raise AssertionError("非法配置未被拒绝: %r" % (bad,))

    print("=== 13. 锁行为 ===")
    from brain_memory.locks import WritePriorityRWLock
    lk = WritePriorityRWLock()
    try:
        with lk.read():
            with lk.write(timeout=0.2):
                pass
    except bm.LockOrderError:
        print("锁序自锁检测 OK")
    else:
        raise AssertionError("持读锁申请写锁未被拒绝")

    holder_ready = threading.Event()
    holder_release = threading.Event()

    def holder():
        with lk.write(timeout=1.0):
            holder_ready.set()
            holder_release.wait(2.0)

    th = threading.Thread(target=holder)
    th.start()
    holder_ready.wait(2.0)
    try:
        with lk.write(timeout=0.2):
            pass
    except bm.LockTimeoutError:
        print("锁超时 OK")
    finally:
        holder_release.set()
        th.join()

    print("=== 14. 临时目录 ===")
    print(tmp)
    print()
    shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print("失败项:", FAILS)
        sys.exit(1)
    print("全部通过")


main()
