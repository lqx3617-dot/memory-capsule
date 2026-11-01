#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写入吞吐 / recall 延迟基准（手动跑，不进 CI）。

用法::

    python scripts/bench.py                # 默认 500 / 2000 / 5000
    python scripts/bench.py --sizes 1000 20000
    python scripts/bench.py --no-graph     # 只看纯打分路径

为什么要有它：很多「性能」判断靠感觉。这个脚本把两个数字摆出来 ——
单条写入耗时、recall 中位延迟 —— 并且按 enable_graph 分开测，
因为图谱建边是写入路径上唯一的重 CPU（见下）。

2026-11-01 基线（GitHub Actions ubuntu-latest, py3.12）：

| 条数 | enable_graph | 写入 ms/条 | recall 中位 ms |
|---|---|---|---|
| 500 | True | 16.2 | 37.5 |
| 500 | False | 1.8 | 15.4 |
| 5000 | True | 21.9 | 180.9 |
| 5000 | False | 4.0 | 144.1 |

结论：写入的大头是 build_links（200 条候选 × SequenceMatcher）；
recall 的大头是向量暴力扫描（2000~5000 条时是主项）。
"""
import argparse
import os
import random
import statistics
import string
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain_memory as bm


def make_words(n=300, seed=42):
    rnd = random.Random(seed)
    return ["".join(rnd.choices(string.ascii_lowercase, k=6)) for _ in range(n)]


def gen(rnd, words, k=6):
    return " ".join(rnd.choices(words, k=k))


def bench_one(n, enable_graph, queries=30):
    tmp = tempfile.mkdtemp(prefix="bm_bench_")
    db = bm.MemoryDB(os.path.join(tmp, "b.db"), {"enable_graph": enable_graph})
    try:
        words = make_words()
        rnd = random.Random(7)
        texts = [gen(rnd, words) for _ in range(n)]
        t0 = time.perf_counter()
        for t in texts:
            db.remember("s", t)
        write_ms = (time.perf_counter() - t0) * 1000 / n
        qrnd = random.Random(11)
        lat = []
        for _ in range(queries):
            q = gen(qrnd, words)
            t1 = time.perf_counter()
            db.recall(q, top_k=10)
            lat.append((time.perf_counter() - t1) * 1000)
        return write_ms, statistics.median(lat)
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆胶囊写入 / recall 基准")
    ap.add_argument("--sizes", nargs="+", type=int, default=[500, 2000, 5000])
    ap.add_argument("--no-graph", action="store_true", help="只测 enable_graph=False")
    args = ap.parse_args()

    flags = (False,) if args.no_graph else (True, False)
    print("| 条数 | enable_graph | 写入 ms/条 | recall 中位 ms |")
    print("|---|---|---|---|")
    for n in args.sizes:
        for flag in flags:
            w, r = bench_one(n, flag)
            print("| %d | %s | %.3f | %.2f |" % (n, flag, w, r), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
