# -*- coding: utf-8 -*-
"""文档里的 Python 代码块语法检查。

不硬编码文件名 —— 文档会挪位置（2026-10-27 从根目录移进 docs/），
写死路径的话 CI 会直接 FileNotFoundError。改成扫根目录 + docs/ 下所有 .md。
"""

import ast
import os
import re
import sys

bad = 0
fence = "```"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

paths = []
for d in (ROOT, os.path.join(ROOT, "docs")):
    if not os.path.isdir(d):
        continue
    for name in sorted(os.listdir(d)):
        if name.lower().endswith(".md"):
            paths.append(os.path.join(d, name))

if not paths:
    print("  [WARN] 没找到任何 .md 文档")
    sys.exit(0)

pattern = fence + "python\\n(.*?)" + fence

for path in paths:
    try:
        text = open(path, encoding="utf-8").read()
    except Exception as exc:
        print("  [WARN] 读不了 %s: %s" % (path, exc))
        continue
    for m in re.finditer(pattern, text, re.S):
        code = m.group(1)
        # 占位符代码不检查：示例里常有 ... / xxx / sk- / <pkg>
        if "..." in code or "xxx" in code or "sk-" in code or "<pkg>" in code:
            continue
        try:
            ast.parse(code)
        except SyntaxError as exc:
            line = text[:m.start()].count("\n") + 1
            print("  [WARN] %s:%d 代码块语法: %s" % (path, line, exc.msg))
            bad += 1

print("  文档代码块检查完成（扫了 %d 个文档），问题 %d 处" % (len(paths), bad))
