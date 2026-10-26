
import os
import re, sys, ast
bad = 0
fence = "```"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in [os.path.join(ROOT, p) for p in ("README.md", "EMBEDDING.md", "PUBLIC_API.md")]:
    text = open(path, encoding="utf-8").read()
    pattern = fence + "python\n(.*?)" + fence
    for m in re.finditer(pattern, text, re.S):
        code = m.group(1)
        if "..." in code or "xxx" in code or "sk-" in code or "<pkg>" in code:
            continue
        try:
            ast.parse(code)
        except SyntaxError as exc:
            line = text[:m.start()].count("\n") + 1
            print("  [WARN] %s:%d 代码块语法: %s" % (path, line, exc.msg))
            bad += 1
print("  文档代码块检查完成，问题 %d 处" % bad)
