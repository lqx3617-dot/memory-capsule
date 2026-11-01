#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""性能探针：写入吞吐 / recall 延迟随库规模的变化（一次性，跑完删）。"""
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


def bench_one(n, enable_graph):
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
        for _ in range(30):
            q = gen(qrnd, words)
            t1 = time.perf_counter()
            db.recall(q, top_k=10)
            lat.append((time.perf_counter() - t1) * 1000)
        return write_ms, statistics.median(lat)
    finally:
        db.close()


def main() -> int:
    sizes = [500, 2000, 5000]
    print("| 条数 | enable_graph | 写入 ms/条 | recall 中位 ms |")
    print("|---|---|---|---|")
    rows = []
    for n in sizes:
        for flag in (True, False):
            w, r = bench_one(n, flag)
            line = "| %d | %s | %.3f | %.2f |" % (n, flag, w, r)
            print(line, flush=True)
            rows.append(line)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("### 性能探针\n\n| 条数 | enable_graph | 写入 ms/条 | recall 中位 ms |\n|---|---|---|---|\n")
            fh.write("\n".join(r.split("|", 1)[1] for r in rows).join(""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
