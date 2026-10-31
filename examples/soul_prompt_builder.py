#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""System Prompt 组装：称呼规则置顶，长期偏好紧跟，检索结果放最后。

为什么称呼要放最顶：模型对 prompt 头部的指令遵从度最高，而「用名字称呼、
不要用您」这种规则一旦没被听见，整段回复的语气就错了 —— 它比任何一条记忆都靠前。

用法：

    items = db.recall_with_budget(user_text, total_budget=2500)
    prompt = build_system_prompt(persona="你是爸爸的助手", user_name="李跃铭",
                                 memory_items=items)
"""
from __future__ import annotations

from typing import Any


DEFAULT_ADDRESS_RULES = (
    "用户叫{name}，必须用这个名字称呼，严禁使用「您」。",
    "自称时用「我」，不要用「本助手」「小助手」这类第三人称。",
)


def build_system_prompt(
    *,
    user_name: str,
    persona: str = "",
    memory_items: list[Any] | None = None,
    address_rules: tuple[str, ...] | None = None,
    extra: str = "",
) -> str:
    """拼 system prompt。顺序：称呼规则 → persona → PIN 记忆 → 检索记忆 → 附加。"""
    items = list(memory_items or [])
    rules = address_rules if address_rules is not None else DEFAULT_ADDRESS_RULES

    lines: list[str] = []
    for rule in rules:
        lines.append(rule.format(name=user_name))
    if persona:
        lines.append("")
        lines.append(persona)

    pinned = [m for m in items if getattr(m, "layer", "") == "PIN"]
    recalled = [m for m in items if getattr(m, "layer", "") != "PIN"]

    if pinned:
        lines.append("")
        lines.append("## 必须遵守的长期设定")
        for m in pinned:
            lines.append("- " + str(getattr(m, "content", m)))
    if recalled:
        lines.append("")
        lines.append("## 可能相关的历史记忆")
        for m in recalled:
            lines.append("- " + str(getattr(m, "content", m)))
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines)


def main() -> int:
    """自测：不需要数据库，纯字符串组装。"""
    class M:
        def __init__(self, content, layer):
            self.content = content
            self.layer = layer

    text = build_system_prompt(
        user_name="李跃铭",
        persona="你是爸爸的助手。",
        memory_items=[M("不喜欢被叫您", "PIN"), M("昨天在聊检索", "L2")],
    )
    fails = []
    first = text.split("\n")[0]
    if "李跃铭" not in first:
        fails.append("第一行没有名字")
    if "严禁使用「您」" not in first:
        fails.append("第一行没有称呼禁令")
    if text.index("必须遵守的长期设定") > text.index("可能相关的历史记忆"):
        fails.append("PIN 段没排在检索段前面")
    print(text)
    print()
    if fails:
        print("失败: " + "；".join(fails))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
