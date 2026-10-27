#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-1 门控：陈述/祈使不许被误判成疑问句。

注意断言分三层，别混：
  A. 带明确约束信号的句子 —— 必须被**收下**；
  B. 不带信号但明显是陈述/祈使的句子 —— 允许因低分被拒，但 reason 绝不能是 "question"；
  C. 真疑问句 —— 必须被拒，且 reason 是 "question"。
第一版测试把 B 也断言成 accepted，那是错的：门控的置信度阈值与疑问判定是两件事。
"""
import sys

from brain_memory.config import MemoryConfig
from brain_memory.gate import RuleGate

MUST_ACCEPT = [
    "代码风格必须这么写，别改",
    "我们决定数据库统一用 WAL 模式",
    "这个项目所有配置必须走环境变量，不能硬编码",
]
NOT_QUESTION = [
    "不要问我为什么",
    "没什么好商量的",
    "不管多少钱都要买这个方案",
]
MUST_REJECT_QUESTION = [
    "数据库选哪个好呢",
    "我喜欢什么",
    "这个接口怎么调用？",
    "为什么不用 Redis",
]


def main() -> int:
    gate = RuleGate(MemoryConfig())
    bad = 0

    for text in MUST_ACCEPT:
        r = gate.evaluate(text)
        mark = "[OK]  " if r.accepted else "[FAIL]"
        print("%s 必须收下  %r -> accepted=%s reason=%s" % (mark, text, r.accepted, r.reason))
        bad += 0 if r.accepted else 1

    for text in NOT_QUESTION:
        r = gate.evaluate(text)
        ok = r.reason != "question"
        mark = "[OK]  " if ok else "[FAIL]"
        print("%s 不许判疑问 %r -> accepted=%s reason=%s" % (mark, text, r.accepted, r.reason))
        bad += 0 if ok else 1

    for text in MUST_REJECT_QUESTION:
        r = gate.evaluate(text)
        ok = (not r.accepted) and r.reason == "question"
        mark = "[OK]  " if ok else "[FAIL]"
        print("%s 必须拒收  %r -> accepted=%s reason=%s" % (mark, text, r.accepted, r.reason))
        bad += 0 if ok else 1

    print("\n%s" % ("全部通过" if bad == 0 else "失败 %d 项" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
