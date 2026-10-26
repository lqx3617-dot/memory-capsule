"""check.sh 的实际执行体：静态扫描 + 真建库体检。

单独成文件而不是塞进 shell heredoc —— 某些精简 shell（Android mksh）
的 heredoc 会往 /data/local 写临时文件，PRoot 里权限不够。
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "brain_memory")
sys.path.insert(0, ROOT)

BANNED = {
    "torch": "规则禁止模型权重依赖",
    "transformers": "规则禁止模型权重依赖",
    "sentence_transformers": "规则禁止模型权重依赖",
    "tensorflow": "规则禁止模型权重依赖",
    "keras": "规则禁止模型权重依赖",
    "multiprocessing": "PRoot 下不能可靠 fork",
}

DANGER = [
    (r"/data/adb", "root 依赖（PRoot 免 root 环境不允许）"),
    (r"\bsu\s+-c\b", "root 提权调用"),
    (r"mmap_size\s*=\s*[1-9]", "mmap_size 必须为 0"),
]

FAILS: list[str] = []


def ok(msg: str) -> None:
    print("  [OK]   " + msg)


def bad(msg: str) -> None:
    print("  [FAIL] " + msg)
    FAILS.append(msg)


def warn(msg: str) -> None:
    print("  [WARN] " + msg)


def py_files():
    for base, _dirs, names in os.walk(PKG):
        for name in sorted(names):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def step_scan() -> int:
    problems = 0
    for path in py_files():
        rel = os.path.relpath(path, ROOT)
        text = open(path, encoding="utf-8").read()
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as exc:
            print("  [FAIL] %s 语法错误: %s" % (rel, exc))
            problems += 1
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            for name in names:
                if name in BANNED:
                    print("  [FAIL] %s:%d 引入 %s（%s）" % (rel, node.lineno, name, BANNED[name]))
                    problems += 1
        for pattern, why in DANGER:
            for m in re.finditer(pattern, text):
                line = text[: m.start()].count("\n") + 1
                print("  [FAIL] %s:%d 命中 %r（%s）" % (rel, line, pattern, why))
                problems += 1

    if problems == 0:
        ok("禁用依赖 / 危险模式：0 命中")
    else:
        bad("静态扫描命中 %d 处" % problems)

    from brain_memory import types as T

    schema = open(os.path.join(PKG, "schema.sql"), encoding="utf-8").read()
    m = re.search(r"state\s+TEXT[^,]*CHECK \(state IN \(([^)]*)\)\)", schema)
    declared = set(x.strip().strip("'") for x in m.group(1).split(",")) if m else set()
    if declared == set(T.ATOM_STATES):
        ok("schema.atoms.state 与 ATOM_STATES 一致（%d 个）" % len(declared))
    else:
        bad("schema.state 与 ATOM_STATES 不一致")

    db_src = open(os.path.join(PKG, "db.py"), encoding="utf-8").read()
    actions = set(re.findall(r'_journal\(\s*conn,\s*"([a-z_]+)"', db_src))
    actions |= {"silence", "archive"}
    missing = actions - set(T.JOURNAL_ACTIONS)
    if missing:
        warn("代码写了 types 未声明的 action: %s" % sorted(missing))
    else:
        ok("journal action 全部在 types 中声明")

    cli = open(os.path.join(PKG, "cli.py"), encoding="utf-8").read()
    required = {"ingest", "recall", "forget", "stats", "dream"}
    declared_cmds = set(re.findall(r'_parser\("([a-z]+)"', cli))
    missing_cmd = required - declared_cmds
    if missing_cmd:
        bad("CLI 缺少子命令: %s" % sorted(missing_cmd))
    else:
        ok("CLI 五个子命令齐备")

    return 1 if FAILS else 0


def step_doctor() -> int:
    from brain_memory import MemoryDB

    tmp = tempfile.mkdtemp(prefix="bm_check_")
    db = MemoryDB(os.path.join(tmp, "check.db"))
    want = {
        "journal_mode": "wal",
        "synchronous": 1,
        "mmap_size": 0,
        "busy_timeout": 5000,
        "temp_store": 2,
        "foreign_keys": 1,
    }
    for pragma, expect in want.items():
        got = db._writer.execute("PRAGMA " + pragma + ";").fetchone()[0]
        if got == expect:
            ok("%-14s = %r" % (pragma, got))
        else:
            bad("%-14s = %r（期望 %r）" % (pragma, got, expect))

    for name in ("ingest", "recall", "forget", "dream", "close", "stats", "maintenance", "feedback"):
        if not hasattr(db, name):
            bad("缺少方法 " + name)
    if not FAILS:
        ok("五个核心 API 与运维方法齐备")

    event_id = db.ingest("check", "user", "我们决定数据库统一用 WAL 模式")
    hits = db.recall("WAL")
    if hits:
        ok("ingest -> recall 往返（event=%d, hits=%d）" % (event_id, len(hits)))
    else:
        bad("ingest -> recall 往返失败")
    db.close()
    ok("close 正常")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAILS else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", default="all", choices=["scan", "doctor", "all"])
    args = parser.parse_args()
    if args.step in ("scan", "all"):
        step_scan()
    if args.step in ("doctor", "all"):
        step_doctor()
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
