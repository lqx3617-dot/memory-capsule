#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""称呼归一：不许把不同标识符的前缀截断成同一个「名字」。

原正则 {1,6} 只取 6 个字符：「叫 getUserInfo」「叫 getUserList」都得到 getUse，
check_duplicate 直接把它们当同一件事合并，丢一条事实。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain_memory.promote import _call_name

SAME = [("他叫老李", "喊他老李"), ("昵称是豆豆", "外号叫豆豆")]
DIFF = [
    ("这个函数叫 getUserInfo", "那个接口叫 getUserList"),
    ("这个库叫 brain_memory", "那个包叫 brain_agent"),
    ("项目叫 memory-capsule", "项目叫 memory-agent"),
]


def main() -> int:
    bad = 0
    for a, b in SAME:
        na, nb = _call_name(a), _call_name(b)
        ok = bool(na) and na == nb
        print(("  [OK]   " if ok else "  [FAIL] ") + "同名 %r -> %r / %r" % (a, na, nb))
        bad += 0 if ok else 1
    for a, b in DIFF:
        na, nb = _call_name(a), _call_name(b)
        ok = na != nb
        print(("  [OK]   " if ok else "  [FAIL] ") + "不同 %r -> %r / %r" % (a, na, nb))
        bad += 0 if ok else 1
    for text in ("这个按钮叫 /api/v2", "他叫李"):
        name = _call_name(text)
        ok = name == ""
        print(("  [OK]   " if ok else "  [FAIL] ") + "拒绝 %r -> %r" % (text, name))
        bad += 0 if ok else 1
    print("\n%s" % ("全部通过" if bad == 0 else "失败 %d 项" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())