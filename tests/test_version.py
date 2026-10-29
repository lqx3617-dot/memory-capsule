#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本号一致性：pyproject.toml 与 brain_memory.__version__ 不许漂移。

漂移过一次（pyproject 1.2.2，__version__ 还是 0.1.0），用户读到的版本是错的。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain_memory as bm

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def declared_version() -> str:
    with open(os.path.join(HERE, "pyproject.toml"), "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("version =") or line.startswith("version="):
                return line.split("=", 1)[1].strip().strip(chr(34)).strip(chr(39))
    return ""


def main() -> int:
    declared = declared_version()
    actual = bm.__version__
    print("[INFO] pyproject=%s  __version__=%s" % (declared, actual))
    if not declared:
        print("[FAIL] 读不到 pyproject 的 version")
        return 1
    if declared != actual:
        print("[FAIL] 版本漂移：pyproject 与 __version__ 不一致")
        return 1
    print("[OK]   版本一致")
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
