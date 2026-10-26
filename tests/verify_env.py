import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from brain_memory import MemoryDB, MemoryConfig

db = MemoryDB("test_memory.db", config=MemoryConfig())
print("MemoryDB repr:", db)
print()

print("--- 你原本想查的三项（改成正确 API 后）---")
for pragma in ("journal_mode", "mmap_size", "synchronous"):
    value = db._writer.execute("PRAGMA " + pragma + ";").fetchone()[0]
    print("%-16s = %r" % (pragma, value))
print()

print("--- 规则要求的六条 PRAGMA 全量核对 ---")
expect = {
    "journal_mode": "wal",
    "synchronous": 1,          # NORMAL == 1
    "mmap_size": 0,
    "busy_timeout": 5000,
    "temp_store": 2,           # MEMORY == 2
    "foreign_keys": 1,
}
bad = []
for pragma, want in expect.items():
    got = db._writer.execute("PRAGMA " + pragma + ";").fetchone()[0]
    flag = "OK " if got == want else "!! "
    if got != want:
        bad.append((pragma, want, got))
    print("%s%-16s = %-8r 期望 %r" % (flag, pragma, got, want))
print()

print("--- 附加项 ---")
print("--     user_version   =", db._writer.execute("PRAGMA user_version;").fetchone()[0])
print("--     cache_size     =", db._writer.execute("PRAGMA cache_size;").fetchone()[0])
print("--     sqlite_version =", sqlite3.sqlite_version)
print("--     fts_tokenizer  =", db.fts_tokenizer)

print()
print("--- 表清单 ---")
rows = db._writer.execute(
    "SELECT name FROM sqlite_master WHERE type IN ('table','index') ORDER BY name"
).fetchall()
tables = [r[0] for r in rows]
print("对象数:", len(tables))
print("表:", [t for t in tables if not t.startswith("idx_") and not t.startswith("sqlite_")])
print("索引数:", len([t for t in tables if t.startswith("idx_")]))
print("journal 记录:", db._writer.execute("SELECT action, detail_json FROM journal").fetchall())

print()
print("--- stats() ---")
for k, v in db.stats().as_dict().items():
    print("  %-20s %s" % (k, v))

db.close()
print()
print("结论:", "全部 PRAGMA 生效" if not bad else ("不符: %r" % (bad,)))
sys.exit(0 if not bad else 1)
