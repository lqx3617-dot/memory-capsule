#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针：只用来验证 check.sh 的 [FLAKY] 判定 —— 首跑故意失败，重跑通过。

⚠ 这不是测试，属于一次性验证工具。验证完必须删掉，
否则每次 CI 都会挂一个「偶发」，等于自己给自己造噪声。
"""
import os
import sys
import tempfile

MARK = os.path.join(tempfile.gettempdir(), "bm_flaky_probe_mark")


def main() -> int:
    if not os.path.exists(MARK):
        try:
            with open(MARK, "w", encoding="utf-8") as fh:
                fh.write("1")
        except OSError:
            pass
        print("  [FAIL] 探针：首跑故意失败（同一 job 内下一次运行会通过）")
        return 1
    print("  [OK]   探针：重跑通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
