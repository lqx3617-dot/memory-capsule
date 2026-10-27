"""编码门控 Perception Gate（L0 -> L1）。

分层判定，顺序固定：
1) 硬拒：长度、纯标点、纯链接、代码块、日志行、JSON 块、寒暄、复读、瞎聊
2) 规则计分：偏好 / 决策 / 约束 / 惯例 / Bug 根因 / 任务，命中越狠分越高
3) 边界区间才调 LLM（可选），LLM 失败立即回退规则并熔断 60s

设计原则：规则永远有答案，LLM 只是加分项；门控拒收不抛异常，L0 照样落盘。
"""
from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import MemoryConfig

# ---------------------------------------------------------------- 正则与词表

_WS_RE = re.compile(r"\s+")
_PUNCT_ONLY_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)
_URL_ONLY_RE = re.compile(r"^(https?://|www\.)\S+$", re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```")
_LOG_LINE_RE = re.compile(
    r"^\s*(\[?\d{4}-\d{2}-\d{2}|\[\d{2}:\d{2}:\d{2}|[IDEW]/[A-Za-z]|Traceback |\$\s|>>>\s)"
)

# 寒暄 / 闲聊：整句就是这些内容 -> 拒
GREETING_TOKENS = {
    "你好", "您好", "在吗", "在不在", "在么", "早上好", "晚上好", "早安", "晚安",
    "哈喽", "hello", "hi", "嗨", "谢谢", "多谢", "谢了", "好的", "好", "嗯", "哦",
    "收到", "ok", "666", "哈哈", "哈哈哈", "哈哈哈哈", "笑死", "牛", "牛逼", "厉害",
    "辛苦了", "麻烦了", "拜拜", "再见", "睡了", "吃了", "吃了吗", "干嘛呢",
}

CHITCHAT_MARKERS = (
    "今天天气", "天气不错", "天气真好", "好无聊", "随便聊聊", "聊点啥",
    "吃了吗", "吃饭了没", "你吃饭了", "在干嘛", "干嘛呢", "睡了吗",
)

# 计分词表：每命中一个 +1，按类别汇总
SIGNAL_WORDS: dict[str, tuple[str, ...]] = {
    "preference": (
        "我习惯", "我更喜欢", "我偏好", "我喜欢用", "我倾向", "我一般用",
        "以后都用", "我比较喜欢", "我更愿意", "我通常", "我用惯了",
        "我不喜欢", "我讨厌", "我受不了", "我更希望", "我比较想",
    ),
    "decision": (
        "决定", "统一用", "统一改成", "换成", "改用", "方案是", "架构上",
        "选型", "就定", "最终定", "拍板", "不用了改", "重构为",
    ),
    "constraint": (
        "必须", "不能", "禁止", "只能", "不允许", "一定要", "要求是",
        "别用", "不要用", "不准", "务必", "强制", "硬性要求",
        # 用户拒绝类表达，含"别给我发语音"这种最常见的偏好
        "别给我", "别发", "不要给我", "别打", "不听", "不看", "不想收到",
        "只发文字", "文字就行", "文字就好", "不用回", "别回",
    ),
    "convention": (
        "这个项目", "我们这个库", "按规矩", "约定是", "惯例", "沿用",
        "我们的规范", "团队规定", "代码风格是",
    ),
    "bugroot": (
        "根因", "根本原因", "问题出在", "原因是", "导致", "踩坑", "坑在",
        "修复方式", "报错是因为", "崩在",
    ),
    "task": (
        "todo", "待办", "下一步", "记得", "别忘了", "明天要", "要做的是",
        "待会儿", "稍后处理", "留个记录",
    ),
}

# 正则型信号：命中算 2 分
SIGNAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("decision", r"用.{1,12}(?:不用|而不是|代替).{1,12}"),
    ("constraint", r"(?:必须|只能|一定)(?:要|得)?.{0,12}(?:不能|不许|不准)"),
    ("bugroot", r"(?:因为|由于).{1,20}(?:导致|所以|才)"),
    ("preference", r"我(?:更|比较)?(?:喜欢|习惯|倾向).{1,20}"),
    ("task", r"(?:记得|别忘了|待会儿).{1,20}"),
)

# 有这些词就不算瞎聊（防止把「今天天气不错，但必须改配置」误杀）
_SIGNAL_HINTS = (
    "必须", "不能", "决定", "改成", "统一", "根因", "原因", "记得", "约定",
    "要求", "禁止", "喜欢", "习惯", "报错", "配置", "参数", "接口", "数据库",
)


# ---------------------------------------------------------------- 结果结构


@dataclass(frozen=True, slots=True)
class GateResult:
    accepted: bool
    fact_type: str
    confidence: float
    content: str
    reason: str
    method: str = "rule"          # rule / llm / llm_fallback
    entity_hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "fact_type": self.fact_type,
            "confidence": self.confidence,
            "content": self.content,
            "reason": self.reason,
            "method": self.method,
            "entity_hint": self.entity_hint,
        }


# ---------------------------------------------------------------- 工具函数


def normalize_text(text: str) -> str:
    """轻量归一：压空白、去首尾引号。原文语义不动。"""
    out = _WS_RE.sub(" ", str(text)).strip()
    out = out.strip("\"'“”‘’")
    return out


def _strip_punct(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE).lower()


def _is_json_blob(text: str) -> bool:
    head = text.lstrip()[:1]
    if head not in ("{", "["):
        return False
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return False
    return True


def _is_repetition(text: str) -> bool:
    core = _strip_punct(text)
    if len(core) < 6:
        return False
    return max(core.count(ch) for ch in set(core)) / len(core) > 0.6


def _has_signal_hint(text: str) -> bool:
    low = text.lower()
    return any(hint in low for hint in _SIGNAL_HINTS)


# ---------------------------------------------------------------- 规则主体



# 疑问句不是事实：问「我喜欢玩什么游戏」不代表「喜欢玩什么游戏」是偏好。
# 实测这类问句收进 L2 后，下一轮注入 system prompt，
# 模型会把问题当已知事实复述，反而更乱。
# 陈述/祈使优先：句子里一旦出现这些词，就不当问句处理。
# 原实现扫「句尾 8 字」并含单字「么」，导致
# 「代码风格必须这么写」「不要问我为什么」「没什么好商量的」全被硬拒。
_ASSERTIVE = ("必须", "不能", "不要", "别", "不用", "禁止", "只能", "统一",
              "决定", "记得", "务必", "一定要", "不管", "无论", "宁愿")
# 明确疑问构式：几乎不用于陈述，优先于 _ASSERTIVE 判定
_Q_PATTERNS = ("哪个", "哪些", "哪种", "是不是", "要不要", "行不行",
               "好不好", "可不可以", "有没有")
# 以疑问词开头的句子，即使后半句带「不用/别」也仍然是问句
_WH_FIRST = ("为什么", "怎么", "哪", "什么", "多少", "是否")
_TAIL_QUESTION = ("吗", "呢", "么")
_END_QUESTION = ("什么", "怎么", "哪儿", "哪里", "几点", "多少",
                 "为什么", "是不是", "有没有", "能不能")


def _is_question(text: str) -> bool:
    """判断是不是一句问话。判定顺序：问号 > 疑问构式 > 疑问词开头 > 断言词 > 句尾特征。"""
    t = (text or "").strip()
    if not t:
        return False
    if t[-1] in "?？":
        return True
    if any(p in t for p in _Q_PATTERNS):
        return True
    if t.startswith(_WH_FIRST):
        return True
    if any(w in t for w in _ASSERTIVE):
        return False
    if t[-3:].endswith(_TAIL_QUESTION):
        return True
    return any(t.endswith(w) for w in _END_QUESTION)


def _hard_reject(text: str, cfg: MemoryConfig) -> str | None:
    core = text.strip()
    if len(core) < cfg.gate_min_chars:
        return "too_short"
    if _PUNCT_ONLY_RE.match(core):
        return "punctuation_only"
    if _URL_ONLY_RE.match(core):
        return "url_only"
    if _CODE_FENCE_RE.search(core):
        return "code_block"
    if _is_json_blob(core):
        return "json_blob"
    if _LOG_LINE_RE.match(core) and len(core) < 500:
        return "log_line"
    if _is_repetition(core):
        return "repetition"
    if _is_question(core):
        return "question"

    stripped = _strip_punct(core)
    if stripped in GREETING_TOKENS:
        return "greeting"

    if not _has_signal_hint(core):
        for marker in CHITCHAT_MARKERS:
            if marker in core:
                return "chitchat"
    return None


def _score(text: str) -> tuple[float, str, list[str]]:
    """返回 (原始分, 命中最强的 fact_type, 命中明细)。"""
    low = text.lower()
    hits: dict[str, int] = {}
    detail: list[str] = []

    for fact_type, words in SIGNAL_WORDS.items():
        for word in words:
            if word in low:
                hits[fact_type] = hits.get(fact_type, 0) + 1
                detail.append(fact_type + ":" + word)

    for fact_type, pattern in SIGNAL_PATTERNS:
        if re.search(pattern, text):
            hits[fact_type] = hits.get(fact_type, 0) + 2
            detail.append(fact_type + ":/" + pattern + "/")

    if not hits:
        return 0.0, "fact", detail

    total = float(sum(hits.values()))
    diversity = min(3, len(hits)) - 1
    raw = total + 0.5 * max(0, diversity)

    priority = ("preference", "decision", "constraint", "convention", "bugroot", "task")
    best = max(hits.items(), key=lambda kv: (kv[1], -priority.index(kv[0])))[0]
    return raw, best, detail


def _confidence(raw: float) -> float:
    # 1 个词约 0.55，2 个约 0.73，3 个约 0.85，5 个以上接近 0.95
    return round(min(0.96, 0.38 + 0.19 * math.log1p(raw) + 0.06 * min(raw, 4)), 4)


# ---------------------------------------------------------------- LLM 门控


class _LlmClient:
    """极简 OpenAI 兼容客户端，只用 stdlib。失败一律返回 None。"""

    def __init__(self, cfg: MemoryConfig) -> None:
        self.cfg = cfg
        self._cooldown_until = 0.0

    @property
    def available(self) -> bool:
        return (
            self.cfg.enable_llm_gate
            and bool(self.cfg.llm_endpoint)
            and bool(self.cfg.llm_model)
            and time.monotonic() >= self._cooldown_until
        )

    def tripped(self) -> None:
        self._cooldown_until = time.monotonic() + max(0.0, self.cfg.llm_cooldown_s)

    def evaluate(self, text: str) -> GateResult | None:
        if not self.available:
            return None
        url = self.cfg.llm_endpoint.rstrip("/") + "/chat/completions"
        body = {
            "model": self.cfg.llm_model,
            "temperature": 0,
            "max_tokens": 200,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是记忆门控。判断这句话是否值得写入长期记忆。"
                        "值得：架构决策、用户偏好、项目惯例、Bug 根因、关键约束、待办。"
                        "不值得：寒暄、闲聊、临时调试输出、常识、重复信息。"
                        "只输出 JSON：{\"accept\": true 或 false, \"fact_type\": "
                        "\"fact|decision|task|preference|constraint|bugroot\", "
                        "\"confidence\": 0 到 1, \"content\": \"规范化后的一句话\"}"
                    ),
                },
                {"role": "user", "content": text[:2000]},
            ],
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + (self.cfg.llm_api_key or ""),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.llm_timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except Exception:
            self.tripped()
            return None

        accept = bool(parsed.get("accept"))
        fact_type = str(parsed.get("fact_type") or "fact")
        try:
            conf = float(parsed.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        normalized = normalize_text(str(parsed.get("content") or text))
        return GateResult(
            accepted=accept,
            fact_type=fact_type if fact_type in SIGNAL_WORDS else "fact",
            confidence=conf,
            content=normalized,
            reason="llm",
            method="llm",
        )


# ---------------------------------------------------------------- 对外入口


class RuleGate:
    """门控器。无状态（除 LLM 熔断时间），可被多线程共用。"""

    def __init__(self, cfg: MemoryConfig) -> None:
        self.cfg = cfg
        self._llm = _LlmClient(cfg)

    def evaluate(self, content: str, role: str = "user", db: Any = None) -> GateResult:
        text = normalize_text(content)

        reason = _hard_reject(text, self.cfg)
        if reason is not None:
            return GateResult(
                accepted=False,
                fact_type="fact",
                confidence=0.0,
                content=text,
                reason=reason,
                method="rule",
            )

        # 只记用户说的话。assistant/system/tool 的内容一律不进 L1 ——
        # 否则机器人复述「别给我发语音」会被当成用户的偏好存下来（真踩过）。
        if role != "user":
            return GateResult(
                accepted=False,
                fact_type="fact",
                confidence=0.0,
                content=text,
                reason="non_user_role",
                method="rule",
            )

        raw, fact_type, detail = _score(text)
        conf = _confidence(raw)

        # 强收：规则已经很有把握，省掉 LLM 调用
        if conf >= 0.78:
            return GateResult(
                accepted=True,
                fact_type=fact_type,
                confidence=conf,
                content=text,
                reason="rule:" + ",".join(detail[:3]),
                method="rule",
            )

        # 边界区间：试探 LLM，失败即回退规则（不抛）
        if 0.42 <= conf < 0.78:
            llm = self._llm.evaluate(text)
            if llm is not None:
                return llm
            method = "rule"
            if db is not None and self.cfg.llm_endpoint:
                try:
                    db.mark_llm_degraded("gate llm 调用失败，回退规则")
                except Exception:
                    pass
                method = "llm_fallback"
            return GateResult(
                accepted=conf >= self.cfg.gate_threshold,
                fact_type=fact_type,
                confidence=conf,
                content=text,
                reason="fallback:" + (",".join(detail[:3]) or "low_signal"),
                method=method,
            )

        # 低分：直接按阈值判
        return GateResult(
            accepted=conf >= self.cfg.gate_threshold,
            fact_type=fact_type,
            confidence=conf,
            content=text,
            reason="rule:" + (",".join(detail[:3]) or "no_signal"),
            method="rule",
        )


def evaluate(cfg: MemoryConfig, content: str, role: str = "user", db: Any = None) -> GateResult:
    """一次性调用的便捷入口。"""
    return RuleGate(cfg).evaluate(content, role=role, db=db)
