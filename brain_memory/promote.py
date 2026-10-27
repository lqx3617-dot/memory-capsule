"""晋升 L1 -> L2：价值、证据、实体链接、去重、冲突 五项检查。

冲突不物理删除：旧原子 state 置 'superseded'，新原子 supersedes_id 指向旧原子。
去重命中不新建：合并到已有原子（取更高 confidence），候选标 merged。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from .config import MemoryConfig
from .types import utc_now

if TYPE_CHECKING:
    from .db import MemoryDB

_WS_RE = re.compile(r"\s+")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]{2,}")
_QUOTED_RE = re.compile(r"[《“\"'\x60]([^》”\"'\x60]{2,16})[》”\"'\x60]")
_NOUN_RE = re.compile(r"([\u4e00-\u9fff]{2,10})(?:项目|模块|库|服务|接口|组件|系统)")
_NEGATIONS = ("不要", "不用", "禁止", "不能", "别用", "不允许", "不该", "无需", "不需要", "取消")

# 指示代词 / 泛指词当实体没意义，直接丢
_STOP_ENTITY = {
    "这个", "那个", "这些", "那些", "一个", "每个", "所有", "我们", "你们",
    "他们", "现在", "以后", "之前", "自己", "东西", "地方", "情况", "问题",
}

# 实体名里出现这些字，基本可以断定是把半句话当名词了
_BAD_ENTITY_CHARS = set("的了是在和与或把被让给从对为要用能不要把没很更最都也就还才你我他它这那")
_MIN_ATOM_CHARS = 2



# 称呼类事实的归一（2026-10-27）。
# 实测库里同一个信息存了 4 遍：「叫老李」「喊他老李」「对方叫老李」「昵称/称呼是老李」。
# 它们的字符串相似度只有 0.31~0.57，卡在 0.9 的阈值下永远合并不了。
# 但语义上是同一件事 —— 只要「称呼动词 + 同一个人名」就对上了。
# 2026-10-27：{1,6} 会截断长标识符 —— 「叫 getUserInfo」「叫 getUserList」
# 都只取到 "getUse"，被判成同一个人。改成收整词（含 _ . - 等标识符字符）。
_CALL_RE = re.compile(
    r"(?:叫|喊|称呼|昵称|名字是|姓名|外号)[\s（(]?([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_.\-]{0,31})"
)
# 按长度降序：先剥「名字是」再剥「是」，否则会把名字首字当虚词吃掉。
# 「外号叫豆豆」这种双动词也会提取出「叫豆豆」，所以要连着剥。
_CALL_STOP = ("名字是", "称呼", "昵称", "外号", "姓名", "是他", "是她",
              "是", "他", "她", "叫", "喊", "了", "成", "过", "着")
_CALL_MIN = 2
# 数字冲突检测：「十点下班」vs「九点下班」相似度 0.857，会被纯字符串判成重复，
# 但它们是两条互相矛盾的事实，合并了等于把用户的话改掉。
_NUM_RE = re.compile(r"[0-9]+|[一二三四五六七八九十百千]+(?=[点号月岁次个分时天周年])")


def _call_name(text: str) -> str:
    """提取称呼名。宁可返回空，也不返回一个会误合并的短串。"""
    m = _CALL_RE.search(text or "")
    if not m:
        return ""
    name = m.group(1)
    # 去掉动词后可能跟的虚词：「喊他老李」->「老李」「称呼是老李」->「老李」
    changed = True
    while changed:                       # 反复剥，直到剥不动（「叫」「是」可能连着出现）
        changed = False
        for w in _CALL_STOP:
            if name.startswith(w) and len(name) > len(w):
                name = name[len(w):]
                changed = True
                break
    name = name.strip(" .,-_:")
    if len(name) < _CALL_MIN:          # 单字名一律不参与「同名即重复」判定
        return ""
    if any(ch in name for ch in "/\\:"):   # 路径 / 协议头不是称呼
        return ""
    return name

def _numbers(text: str) -> set:
    return set(_NUM_RE.findall(text or ""))


def normalize(text: str) -> str:
    return _WS_RE.sub(" ", str(text)).strip()


def similarity(a: str, b: str) -> float:
    """纯 stdlib 相似度，短文本足够快（<1ms）。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def polarity(text: str) -> int:
    low = text.lower()
    return -1 if any(neg in low for neg in _NEGATIONS) else 1


def extract_entity_hint(text: str) -> str | None:
    """从文本里挖一个实体名：引号内容 > 英文标识符 > 「X项目/模块/库」。"""
    for candidate in (
        (_QUOTED_RE.search(text), 1),
        (_IDENT_RE.search(text), 0),
        (_NOUN_RE.search(text), 1),
    ):
        match, group = candidate
        if match is None:
            continue
        name = match.group(group).strip()
        if not name or name.lower() in _STOP_ENTITY:
            continue
        if len(name) > 12:
            continue
        if any(ch in _BAD_ENTITY_CHARS for ch in name):
            continue
        return name
    return None


def ensure_entity(conn: Any, name: str, type_: str = "concept") -> int:
    row = conn.execute(
        "SELECT id FROM entities WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row is not None:
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO entities(name, type, created_at) VALUES (?, ?, ?)",
        (name, type_ if type_ in ("user", "project", "concept") else "concept", utc_now()),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------- 五项检查


def check_value(content: str, confidence: float, cfg: MemoryConfig) -> tuple[bool, str]:
    if not content:
        return False, "empty"
    if len(content) < _MIN_ATOM_CHARS:
        return False, "too_short"
    if len(content) > cfg.max_atom_chars:
        return False, "too_long"
    if confidence < cfg.promote_min_confidence:
        return False, "low_confidence"
    return True, ""


def check_evidence(conn: Any, raw_event_id: int | None) -> tuple[bool, str]:
    if not raw_event_id:
        return False, "no_event"
    row = conn.execute("SELECT 1 FROM raw_events WHERE id = ?", (raw_event_id,)).fetchone()
    return (row is not None), ("" if row is not None else "event_missing")


def check_duplicate(
    conn: Any, content: str, fact_type: str, cfg: MemoryConfig
) -> tuple[int | None, float]:
    rows = conn.execute(
        "SELECT id, content FROM atoms WHERE fact_type = ? AND state IN ('active','silent') "
        "ORDER BY id DESC LIMIT 50",
        (fact_type,),
    ).fetchall()
    best_id: int | None = None
    best_sim = 0.0
    _cname = _call_name(content)
    _cnums = _numbers(content)
    for row in rows:
        _other = str(row[1])
        # 规则一：称呼同一个人 → 直接视为重复（不管字符串像不像）
        _oname = _call_name(_other)
        if _cname and _oname and _cname == _oname and len(_cname) >= _CALL_MIN:
            return int(row[0]), 1.0
        sim = similarity(content, _other)
        # 规则二：两条都带数字且完全不相交 → 不可能重复
        # （「十点下班」和「九点下班」相似度 0.857，必须挡住）
        _onums = _numbers(_other)
        if _cnums and _onums and not (_cnums & _onums):
            continue
        if sim > best_sim:
            best_sim, best_id = sim, int(row[0])
    if best_id is not None and best_sim >= cfg.dedup_similarity:
        return best_id, best_sim
    return None, best_sim


def check_conflict(
    conn: Any, content: str, fact_type: str, cfg: MemoryConfig
) -> tuple[int | None, float]:
    """同 fact_type、否定极性相反、正文相似度 >= 0.55 视为冲突。"""
    mine = polarity(content)
    mine_nums = _numbers(content)
    rows = conn.execute(
        "SELECT id, content FROM atoms WHERE fact_type = ? AND state = 'active' "
        "ORDER BY id DESC LIMIT 50",
        (fact_type,),
    ).fetchall()
    best_id: int | None = None
    best_sim = 0.0
    for row in rows:
        other = str(row[1])
        other_nums = _numbers(other)
        if polarity(other) == mine:
            # 极性相同、句干相似、数字完全不相交 -> 是被改写过的旧事实（原实现的漏杀侧）
            if mine_nums and other_nums and not (mine_nums & other_nums):
                stem = similarity(_NUM_RE.sub("#", content), _NUM_RE.sub("#", other))
                if stem >= 0.6:
                    return int(row[0]), stem
            continue
        sim = similarity(content, other)
        if sim >= 0.55 and sim > best_sim:
            best_sim, best_id = sim, int(row[0])
    return best_id, best_sim


# ---------------------------------------------------------------- 晋升主流程


def promote_candidate(conn: Any, cfg: MemoryConfig, cand: dict[str, Any]) -> tuple[str, int | None]:
    """在调用方事务内执行。返回 (status, atom_id)。"""
    content = normalize(cand.get("content", ""))
    fact_type = str(cand.get("fact_type") or "fact")
    confidence = float(cand.get("confidence") or 0.0)
    raw_event_id = cand.get("raw_event_id")
    hint = cand.get("entity_hint") or extract_entity_hint(content)

    ok, _why = check_value(content, confidence, cfg)
    if not ok:
        return "rejected", None
    ok, _why = check_evidence(conn, raw_event_id)
    if not ok:
        return "rejected", None

    entity_id = ensure_entity(conn, hint) if hint else None

    dup_id, _dup_sim = check_duplicate(conn, content, fact_type, cfg)
    if dup_id is not None:
        now = utc_now()
        conn.execute(
            "UPDATE atoms SET confidence = MAX(confidence, ?), quality = MAX(quality, ?) "
            "WHERE id = ?",
            (confidence, confidence, dup_id),
        )
        row = conn.execute("SELECT state FROM atoms WHERE id = ?", (dup_id,)).fetchone()
        if row is not None and str(row[0]) == "silent":
            # 用户又把这句话说了一遍 = 这条记忆还活着。
            # 只改 state 没用：forget 会用 compute_quality 现场重算再打回 silent，
            # 所以同时把 feedback 抬到 useful、last_accessed_at 推到当下（decay 归 1）。
            conn.execute(
                "UPDATE atoms SET state = 'active', state_changed_at = ?, "
                "feedback = MAX(feedback, ?), last_accessed_at = ? WHERE id = ?",
                (now, cfg.feedback_useful, now, dup_id),
            )
            conn.execute(
                "INSERT INTO journal(action, target_table, target_id, detail_json, created_at) "
                "VALUES ('revive', 'atoms', ?, ?, ?)",
                (dup_id, '{"via":"dedup_merge"}', now),
            )
        return "merged", dup_id

    conflict_id, _conflict_sim = check_conflict(conn, content, fact_type, cfg)

    trimmed = content[: cfg.max_atom_chars]
    cur = conn.execute(
        "INSERT INTO atoms(entity_id, fact_type, content, confidence, quality, "
        "access_count, created_at, supersedes_id, state, source_event_id, "
        "source_candidate_id, feedback) "
        "VALUES (?,?,?,?,?,0,?,?,'active',?,?,1.0)",
        (
            entity_id,
            fact_type,
            trimmed,
            confidence,
            confidence,
            utc_now(),
            conflict_id,
            raw_event_id,
            cand.get("id"),
        ),
    )
    atom_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO atoms_fts(rowid, content) VALUES (?, ?)", (atom_id, trimmed)
    )

    if conflict_id is not None:
        conn.execute(
            "UPDATE atoms SET state = 'superseded', state_changed_at = ? WHERE id = ?",
            (utc_now(), conflict_id),
        )

    return "promoted", atom_id


def promote_pending(db: "MemoryDB", limit: int = 20) -> dict[str, int]:
    """批量把 pending 候选推到 L2。返回各状态计数。"""
    cfg = db.config
    result = {"promoted": 0, "merged": 0, "rejected": 0, "pending": 0}
    with db._write_tx() as conn:
        rows = conn.execute(
            "SELECT id, raw_event_id, fact_type, content, confidence, entity_hint "
            "FROM candidates WHERE status = 'pending' ORDER BY id LIMIT ?",
            (int(limit),),
        ).fetchall()
        for row in rows:
            cand = dict(row)
            status, atom_id = promote_candidate(conn, cfg, cand)
            if status == "rejected":
                conn.execute(
                    "UPDATE candidates SET status = 'rejected', resolved_at = ? WHERE id = ?",
                    (utc_now(), cand["id"]),
                )
            elif status == "merged":
                conn.execute(
                    "UPDATE candidates SET status = 'merged', resolved_at = ? WHERE id = ?",
                    (utc_now(), cand["id"]),
                )
                db._journal(conn, "merge", "atoms", atom_id, {"candidate_id": cand["id"]})
            else:
                conn.execute(
                    "UPDATE candidates SET status = 'promoted', resolved_at = ? WHERE id = ?",
                    (utc_now(), cand["id"]),
                )
                db._journal(conn, "promote", "atoms", atom_id, {"candidate_id": cand["id"]})
                store = getattr(db, "vector_store", None)
                if store is not None and atom_id is not None:
                    try:
                        store.index_atom(conn, atom_id, cand["content"])
                    except Exception:
                        pass
            result[status] = result.get(status, 0) + 1
    return result
