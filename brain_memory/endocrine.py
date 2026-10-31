"""内分泌心境引擎（Endocrine System）。

四个激素量，各自有一个基线，偏离基线后按指数回归：

    h(t+dt) = h_base + (h(t) - h_base) * exp(-LAM_H * dt)

| 量 | 基线 | 含义 |
|---|---|---|
| h_sero | 0.6 | 血清素：稳定感、耐心 |
| h_dopa | 0.4 | 多巴胺：兴奋、想说话 |
| h_cort | 0.2 | 皮质醇：压力、防御 |
| h_oxy  | 0.5 | 催产素：亲近、信任 |

dt 的单位是**分钟**，LAM_H = 0.02 —— 半衰期约 35 分钟：
吵完架一小时，皮质醇从 0.5 回落到 0.29；两小时到 0.15。

它只做两件事：
1. 让「此刻的心情」有一个可读、可衰减的状态，而不是每轮重新猜；
2. 给 recall 提供一个 f_endo 系数 —— 心情好时更容易想起开心的事。

不做的事：不改模型权重、不影响门控判定、不写进 prompt（除非调用方自己要）。
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .types import utc_now

if TYPE_CHECKING:
    from .db import MemoryDB

# 四个量的基线与取值范围
HORMONES: dict[str, float] = {
    "h_sero": 0.6,
    "h_dopa": 0.4,
    "h_cort": 0.2,
    "h_oxy": 0.5,
}

LAM_H = 0.02                 # 每分钟的回归系数（半衰期 ln2/0.02 ≈ 34.7 分钟）
ENDO_GAIN = 0.35             # f_endo 的最大偏移量
MINUTES_CLAMP = 60 * 24 * 30  # 一次最多补算 30 天，避免库放很久后循环爆炸

# 事件 → 各激素的增量（正值抬高、负值压低）
EVENT_DELTAS: dict[str, dict[str, float]] = {
    "insult":   {"h_cort": +0.30, "h_sero": -0.20, "h_oxy": -0.10},   # 被骂
    "praise":   {"h_oxy": +0.30, "h_sero": +0.10, "h_cort": -0.10},   # 被夸
    "joy":      {"h_dopa": +0.30, "h_sero": +0.10},                    # 开心的事
    "intimate": {"h_oxy": +0.25, "h_dopa": +0.10},                     # 亲近
    "stress":   {"h_cort": +0.25, "h_dopa": -0.10},                    # 压力
    "calm":     {"h_cort": -0.20, "h_sero": +0.05},                    # 安抚
    "ignored":  {"h_sero": -0.10, "h_oxy": -0.10},                     # 被晾着
}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def decay_to_base(current: float, base: float, minutes: float,
                  lam: float = LAM_H) -> float:
    """把 current 按指数回归到 base，经过 minutes 分钟。"""
    if minutes <= 0:
        return _clamp01(current)
    minutes = min(float(minutes), MINUTES_CLAMP)
    return _clamp01(base + (float(current) - base) * math.exp(-lam * minutes))


def valence(values: dict[str, float]) -> float:
    """把四个激素压成一个 -1..1 的心情值（正 = 好心情）。

    用的是「愉悦三件套均值 - 压力」，不追求生理精确，只要单调：
    多巴胺/催产素/血清素高 → 变正；皮质醇高 → 变负。
    """
    good = (values.get("h_dopa", 0.0) + values.get("h_oxy", 0.0)
            + values.get("h_sero", 0.0)) / 3.0
    bad = values.get("h_cort", 0.0)
    return max(-1.0, min(1.0, good - bad))


def f_endo(mood_now: float, mood_atom: float, gain: float = ENDO_GAIN) -> float:
    """情绪系数：乘在检索得分上。

    同向放大、反向缩小：
      心情好(+0.8) 想起开心记忆(+1.0)  → 1 + 0.35×0.8 ≈ 1.28
      心情好(+0.8) 想起难过记忆(-1.0)  → 1 - 0.35×0.8 ≈ 0.72
      心情中性(0)                      → 1.00（完全不影响）
    """
    return 1.0 + float(gain) * float(mood_now) * float(mood_atom)


# ---------------------------------------------------------------- 事件识别
#
# 只做「够用」的判断：关键词命中 + 少量强度修正。不用模型，不引入依赖。
# 原则：宁可漏判，不要误判 —— 把「今天天气不错」判成夸奖，激素就开始乱跳。

EVENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "insult": ("傻", "蠢", "滚", "闭嘴", "废物", "垃圾", "去死", "讨厌你",
               "烦死", "恶心", "智障", "有病", "没用", "差劲"),
    "praise": ("厉害", "牛", "太棒", "真好", "爱你", "喜欢你", "佩服",
               "靠谱", "优秀", "漂亮", "感谢", "辛苦了"),
    "joy": ("开心", "高兴", "太好了", "哈哈", "笑死", "爽", "舒服",
            "快乐", "搞定", "成了"),
    "stress": ("累", "压力", "焦虑", "加班", "熬夜", "崩溃", "顶不住",
               "受不了", "烦"),
    "calm": ("别急", "慢慢来", "没事", "放松", "休息", "不急"),
    "ignored": ("没人理", "不理我", "被无视", "冷场"),
}

# 判断顺序：越强的情绪越优先，一条消息只取一个事件
_EVENT_PRIORITY = ("insult", "praise", "stress", "joy", "calm", "ignored")

# 否定前缀：「不厉害」「没意思」不该算夸奖
_NEGATIONS = ("不", "没", "别", "无", "哪有")


def _negated(text: str, index: int) -> bool:
    head = text[max(0, index - 3):index]
    return any(head.endswith(neg) for neg in _NEGATIONS)


def _intensity_of(text: str) -> float:
    """强度：基础 0.6，感叹号/叠字/长句各加一点，封在 0.3~1.5。"""
    score = 0.6
    marks = text.count("!") + text.count("！")
    if marks:
        score += min(0.3, marks * 0.08)
    if re.search(r"(。。|！！！|哈哈|啊啊)", text):
        score += 0.1
    if len(text) >= 50:
        score += 0.1
    return max(0.3, min(1.5, round(score, 2)))


def detect_event(text: str) -> tuple[str, float] | None:
    """从一句话里认出情绪事件，返回 (event_type, intensity)；认不出返回 None。

    用法：

        hit = detect_event("你真是个废物")
        if hit:
            db.endocrine.update_hormones(*hit)

    db.note_event(text) 是「识别 + 更新」的合并版，通常直接用那个。
    """
    body = (text or "").strip()
    if not body:
        return None
    hits: dict[str, int] = {}
    for event_type, words in EVENT_KEYWORDS.items():
        for word in words:
            idx = body.find(word)
            if idx < 0:
                continue
            if _negated(body, idx):
                continue
            hits[event_type] = hits.get(event_type, 0) + 1
    if not hits:
        return None
    for event_type in _EVENT_PRIORITY:
        if event_type in hits:
            # 命中越多越强
            bonus = min(0.2, 0.1 * (hits[event_type] - 1))
            return event_type, min(1.5, _intensity_of(body) + bonus)
    return None


class EndocrineSystem:
    """system_state 表的读写 + 衰减 + 事件扰动。

    所有方法都不抛异常（读失败按基线返回），因为「心情读不出来」不该让对话挂掉。
    """

    def __init__(self, db: "MemoryDB") -> None:
        self.db = db
        self._ensured = False

    # ---- 存储 ----

    def ensure(self) -> None:
        """建表 + 补齐四个量（幂等，老库也能升级）。"""
        if self._ensured:
            return
        with self.db._write_tx() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS system_state ("
                "key TEXT PRIMARY KEY, value REAL NOT NULL, updated_at TEXT NOT NULL)"
            )
            now = utc_now()
            for name, base in HORMONES.items():
                conn.execute(
                    "INSERT INTO system_state(key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO NOTHING",
                    (name, base, now),
                )
        self._ensured = True

    def _read(self) -> tuple[dict[str, float], str]:
        self.ensure()
        out = dict(HORMONES)
        stamp = utc_now()
        try:
            with self.db._read_conn() as conn:
                rows = conn.execute("SELECT key, value, updated_at FROM system_state").fetchall()
            for row in rows:
                key = str(row[0])
                if key in out:
                    out[key] = _clamp01(float(row[1]))
                stamp = str(row[2]) if row[2] else stamp
        except Exception:
            pass
        return out, stamp

    def _write(self, values: dict[str, float]) -> None:
        self.ensure()
        now = utc_now()
        with self.db._write_tx() as conn:
            for name in HORMONES:
                conn.execute(
                    "UPDATE system_state SET value = ?, updated_at = ? WHERE key = ?",
                    (_clamp01(values.get(name, HORMONES[name])), now, name),
                )

    # ---- 衰减 ----

    @staticmethod
    def _minutes_since(stamp: str) -> float:
        try:
            text = stamp.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
            return max(0.0, delta)
        except Exception:
            return 0.0

    def tick(self, minutes: float | None = None) -> dict[str, float]:
        """把激素按经过的时间回归到基线，写回并返回当前值。

        minutes=None 时按 system_state 的 updated_at 自动算 —— 所以每次读之前调
        一次 tick()，就等价于「随时间自然平复」。
        """
        values, stamp = self._read()
        dt = self._minutes_since(stamp) if minutes is None else float(minutes)
        if dt <= 0:
            return values
        decayed = {name: decay_to_base(values[name], HORMONES[name], dt)
                   for name in HORMONES}
        try:
            self._write(decayed)
        except Exception:
            pass
        return decayed

    # ---- 事件扰动 ----

    def update_hormones(self, event_type: str, intensity: float = 1.0) -> dict[str, float]:
        """按事件抬高/压低激素，返回扰动后的状态。

        event_type 取 EVENT_DELTAS 的键：insult / praise / joy / intimate /
        stress / calm / ignored。未知事件按「无扰动」处理，不抛异常。
        """
        try:
            amount = max(0.0, float(intensity))
        except (TypeError, ValueError):
            amount = 1.0
        values = self.tick()          # 先按自然衰减算到现在，再叠加事件
        deltas = EVENT_DELTAS.get(str(event_type), {})
        for name, delta in deltas.items():
            if name in values:
                values[name] = _clamp01(values[name] + delta * amount)
        try:
            self._write(values)
            with self.db._write_tx() as conn:
                self.db._journal(
                    conn, "hormone_shift", "system_state", None,
                    {"event": str(event_type), "intensity": amount,
                     "after": {k: round(v, 4) for k, v in values.items()}},
                )
        except Exception:
            pass
        return values

    # ---- 读 ----

    def current(self) -> dict[str, float]:
        """当前四个值（不衰减、不写库）。"""
        return self._read()[0]

    def mood(self, persist: bool = False) -> float:
        """当前心情值 -1..1。

        默认 **纯读**：只做衰减计算，不写库 —— recall 里要调它，
        而 recall 承诺零写。要落盘就传 persist=True（或直接调 tick()）。
        """
        values, stamp = self._read()
        dt = self._minutes_since(stamp)
        if dt > 0:
            values = {k: decay_to_base(values[k], HORMONES[k], dt) for k in HORMONES}
            if persist:
                try:
                    self._write(values)
                except Exception:
                    pass
        return valence(values)

    def describe(self) -> str:
        """一句话描述，方便塞进日志或调试输出。"""
        v = self.current()
        m = valence(v)
        if m >= 0.35:
            word = "心情不错"
        elif m >= 0.1:
            word = "还算平稳"
        elif m >= -0.1:
            word = "没什么起伏"
        elif m >= -0.35:
            word = "有点低落"
        else:
            word = "心情很差"
        return "%s（valence=%+.2f）%s" % (
            word, m,
            " ".join("%s=%.2f" % (k, v[k]) for k in HORMONES),
        )
