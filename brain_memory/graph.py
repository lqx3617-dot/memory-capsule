"""图谱联想：突触、电势扩散、个性化 PageRank。

三件事：

1. **建边**：新原子进来时，和「同实体」或「内容够像」的已有原子连双向边，
   权重取相似度。边是记忆之间的联想，不是分类。
2. **扩散**：从检索到的种子节点出发，按
   pot_next = pot_current × SPREAD_DECAY × synapse_weight
   一跳一跳往外传，最多 SPREAD_HOPS 跳。电势低于 SPREAD_FLOOR 就停。
   这样能捞到「语义不像、但被联想连起来」的节点。
3. **重排**：把种子电势当 personalization 向量，在收集到的子图上跑
   个性化 PageRank。直连的、被多条边指过来的节点会浮上来。

设计取舍：
- 纯 stdlib，不引 networkx；子图通常只有几十个节点，20 次迭代毫秒级。
- 扩散有电势下限和跳数上限 —— 图一旦变稠密，无边界的扩散会拖垮 recall。
- 只读，不写库（recall 承诺零写）。
"""
from __future__ import annotations

from typing import Any, Iterable

# 扩散参数
SPREAD_DECAY = 0.75      # 每跳的衰减系数
SPREAD_HOPS = 3          # 最多跳几次
SPREAD_FLOOR = 0.02      # 电势低于此值就不再往外传
SPREAD_MAX_NODES = 200   # 子图节点上限，防稠密图爆炸

# 建边参数
LINK_MIN_SIM = 0.35      # 内容相似度低于此值不连边
LINK_SAME_ENTITY = 0.5   # 同实体的默认权重
LINK_MAX_PER_ATOM = 8    # 每个原子最多连出几条边

# PageRank
PR_ALPHA = 0.85
PR_ITERS = 20


def load_edges(conn: Any, node_ids: Iterable[int]) -> dict[int, list[tuple[int, float]]]:
    """把给定节点集合的出边读出来（只读子图）。"""
    ids = [int(x) for x in node_ids]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT source_id, target_id, weight FROM mem_graph WHERE source_id IN ("
        + marks + ")",
        tuple(ids),
    ).fetchall()
    out: dict[int, list[tuple[int, float]]] = {}
    for row in rows:
        out.setdefault(int(row[0]), []).append((int(row[1]), float(row[2])))
    return out


def spread(conn: Any, seeds: dict[int, float], *,
           decay: float = SPREAD_DECAY, hops: int = SPREAD_HOPS,
           floor: float = SPREAD_FLOOR,
           max_nodes: int = SPREAD_MAX_NODES) -> dict[int, float]:
    """电势扩散：从种子出发沿突触往外传。

        pot_next = pot_current × decay × synapse_weight

    返回 {atom_id: 累积电势}，含种子本身（种子电势原样保留）。
    """
    pot: dict[int, float] = {int(k): float(v) for k, v in seeds.items() if v > 0}
    if not pot:
        return {}
    frontier = dict(pot)
    for _hop in range(max(0, int(hops))):
        if not frontier:
            break
        edges = load_edges(conn, frontier.keys())
        nxt: dict[int, float] = {}
        for node, current in frontier.items():
            for target, weight in edges.get(node, ()):
                if target in pot:          # 已有电势的不重复叠加，避免环上爆掉
                    continue
                value = current * decay * max(0.0, min(1.0, weight))
                if value < floor:
                    continue
                nxt[target] = max(nxt.get(target, 0.0), value)
        for node, value in nxt.items():
            pot[node] = value
            if len(pot) >= max_nodes:
                return pot
        frontier = nxt
    return pot


def personalized_pagerank(edges: dict[int, list[tuple[int, float]]],
                          personalization: dict[int, float],
                          *,
                          alpha: float = PR_ALPHA,
                          iters: int = PR_ITERS) -> dict[int, float]:
    """个性化 PageRank（在给定子图上跑）。

    personalization 里没有的节点给一个极小的底，保证图不塌。
    """
    nodes = set(personalization)
    for source, targets in edges.items():
        nodes.add(source)
        for target, _w in targets:
            nodes.add(target)
    if not nodes:
        return {}
    total = sum(personalization.values()) or 1.0
    pr = {n: (personalization.get(n, 0.0) / total if n in personalization else 0.0)
          for n in nodes}
    for n in nodes:
        if pr[n] <= 0.0:
            pr[n] = 1e-6
    s = sum(pr.values()) or 1.0
    pr = {n: v / s for n, v in pr.items()}

    out_weight: dict[int, float] = {}
    for source, targets in edges.items():
        w = sum(max(0.0, float(x[1])) for x in targets)
        out_weight[source] = w if w > 0 else 0.0

    for _ in range(max(1, int(iters))):
        nxt = {n: 0.0 for n in nodes}
        dangling = 0.0
        for source in nodes:
            w = out_weight.get(source, 0.0)
            if w <= 0:
                dangling += pr[source]
                continue
            share = alpha * pr[source] / w
            for target, weight in edges.get(source, ()):
                nxt[target] += share * max(0.0, float(weight))
        # 逐节点算 (1-alpha) 的保留项 —— 不能写成 (1-alpha) * pr（pr 是 dict）
        spread_back = alpha * dangling / len(nodes)   # 悬挂点质量均摊，防总分流失
        for n in nodes:
            nxt[n] += (1.0 - alpha) * pr[n] + spread_back
        total2 = sum(nxt.values()) or 1.0
        pr = {n: v / total2 for n, v in nxt.items()}
    return pr


def build_links(conn: Any, atom_id: int, content: str, entity_id: int | None,
                cfg: Any, *, now: str) -> int:
    """给新原子连边（在调用方事务内执行）。返回建立的边数（单向计数）。"""
    from .promote import similarity

    linked = 0
    rows = conn.execute(
        "SELECT id, content, entity_id FROM atoms "
        "WHERE state = 'active' AND id != ? ORDER BY id DESC LIMIT 200",
        (int(atom_id),),
    ).fetchall()
    scored: list[tuple[float, int]] = []
    for row in rows:
        other_id = int(row[0])
        sim = similarity(content, str(row[1]))
        same_entity = bool(entity_id) and row[2] is not None and int(row[2]) == int(entity_id)
        if same_entity:
            weight = max(LINK_SAME_ENTITY, sim)
        elif sim >= LINK_MIN_SIM:
            weight = sim
        else:
            continue
        scored.append((weight, other_id))
    scored.sort(reverse=True)
    for weight, other_id in scored[:LINK_MAX_PER_ATOM]:
        w = round(max(0.0, min(1.0, float(weight))), 4)
        for a, b in ((int(atom_id), other_id), (other_id, int(atom_id))):
            conn.execute(
                "INSERT INTO mem_graph(source_id, target_id, weight, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(source_id, target_id) DO UPDATE SET "
                "weight = MAX(weight, excluded.weight)",
                (a, b, w, now),
            )
        linked += 1
    return linked
