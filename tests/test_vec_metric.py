#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vec0 距离换算：必须与余弦同一量纲，否则 vec_min_similarity 形同虚设。

原实现 1/(1+distance)：最不像的一对（d=2）还有 0.33，阈值 0.10 什么都挡不住。
归一化向量下 L2^2 = 2 - 2cos，等价换算应为 1 - d*d/2。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain_memory.vec import distance_to_similarity


def main() -> int:
    cases = [
        (0.0, 1.0, "完全相同"),
        (2 ** 0.5, 0.0, "正交（cos=0）"),
        (2.0, -1.0, "完全相反（cos=-1）"),
        (1.0, 0.5, "cos=0.5"),
    ]
    bad = 0
    for dist, want, label in cases:
        got = distance_to_similarity(dist)
        ok = abs(got - want) < 1e-9
        print(("  [OK]   " if ok else "  [FAIL] ") + "%s d=%0.3f -> %0.4f（期望 %0.4f）" % (label, dist, got, want))
        bad += 0 if ok else 1

    worst = distance_to_similarity(2.0)
    if worst >= 0.10:
        print("  [FAIL] 最不像的一对仍然高于 vec_min_similarity=0.10")
        bad += 1
    else:
        print("  [OK]   最不像的一对（%0.3f）会被阈值挡住" % worst)

    print("\n%s" % ("全部通过" if bad == 0 else "失败 %d 项" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())