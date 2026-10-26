"""escalation 专项：L2 置信度不足时向下探 L1/L0，默认关闭。"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain_memory as bm

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def section(t):
    print()
    print("-" * 60)
    print(t)
    print("-" * 60)


def build(path):
    db = bm.MemoryDB(path)
    db.ingest("s1", "user", "我们决定数据库统一用 WAL 模式，别的都不用")
    db.ingest("s1", "user", "今天天气不错")                      # 拒收，只有 L0
    db.ingest(
        "s1", "user", "必须把 upload_timeout 配置项调到 30 秒", promote=False
    )                                                            # 只到 L1
    return db


def main():
    tmp = tempfile.mkdtemp(prefix="bm_esc_")

    section("1. 默认关闭：不查 L1/L0")
    db = build(os.path.join(tmp, "a.db"))
    hits = db.recall("天气")
    print("  recall('天气') ->", len(hits), "条")
    check(len(hits) == 0, "开关关闭时不返回 L0 内容")

    section("2. 打开开关：下探到 L0")
    db2 = build(os.path.join(tmp, "b.db"))
    db2.close()
    db2 = bm.MemoryDB(os.path.join(tmp, "b.db"), {"enable_escalation": True})
    hits = db2.recall("天气")
    for m in hits:
        print("  layer=%s record=%s score=%.3f  %s" % (m.layer, m.record_id, m.score, m.content))
    check(len(hits) >= 1, "下探后能召回 L0 原始事件")
    check(all(m.layer == "L0" for m in hits), "层标记为 L0")
    check(all(m.record_id is not None for m in hits), "record_id 指向 raw_events.id")

    section("3. 下探到 L1（候选事实）")
    hits = db2.recall("upload_timeout 配置项")
    print("  hits =", [(m.layer, m.record_id, m.content[:24]) for m in hits])
    check(len(hits) >= 1, "能召回 L1 候选")
    check(any(m.layer == "L1" for m in hits), "层标记为 L1")

    section("4. L2 有高置信结果时不触发下探")
    hits = db2.recall("WAL 模式")
    print("  hits =", [(m.layer, round(m.score, 3)) for m in hits])
    check(len(hits) >= 1 and hits[0].layer == "L2", "优先返回 L2")
    check(all(m.layer == "L2" for m in hits), "没有混入 L1/L0")

    section("5. 只对 L2 记访问计数")
    before = db2.pending_access()
    db2.recall("天气")          # 只命中 L0
    after = db2.pending_access()
    print("  pending_access: %d -> %d" % (before, after))
    check(after == before, "L0/L1 命中不计访问数（它们不是 atom）")

    section("6. L0 扫描上限生效")
    db3 = bm.MemoryDB(
        os.path.join(tmp, "c.db"),
        {"enable_escalation": True, "escalate_l0_limit": 1},
    )
    db3.ingest("s1", "user", "第一条里有 关键词")
    db3.ingest("s1", "user", "第二条里也有 关键词")
    hits = db3.recall("关键词")
    print("  escalate_l0_limit=1 ->", [(m.layer, m.record_id) for m in hits])
    check(len(hits) <= 1, "上限为 1 时最多返回 1 条 L0")

    section("7. 预算耗尽就不下探")
    tight = db2.recall("天气", budget_tokens=1)
    print("  budget_tokens=1 ->", [(m.layer, m.content[:16]) for m in tight])
    check(len(tight) <= 1, "预算极小不会塞一堆 L0")

    section("8. 关闭时性能不受影响（无 L0 查询）")
    import time
    big = bm.MemoryDB(os.path.join(tmp, "d.db"))
    for i in range(200):
        big.ingest("s", "user", "第%d条普通记录，用来撑大 L0 表" % i)
    t0 = time.monotonic()
    for _ in range(20):
        big.recall("完全不存在的词")
    cost_off = (time.monotonic() - t0) * 1000 / 20
    big.close()
    big2 = bm.MemoryDB(os.path.join(tmp, "d.db"), {"enable_escalation": True})
    t0 = time.monotonic()
    for _ in range(20):
        big2.recall("完全不存在的词")
    cost_on = (time.monotonic() - t0) * 1000 / 20
    print("  单次 recall: 关=%.1fms 开=%.1fms" % (cost_off, cost_on))
    check(cost_off < 50, "关闭时单次 recall < 50ms")

    for d in (db, db2, db3, big2):
        try:
            d.close()
        except Exception:
            pass
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 60)
    if FAILS:
        print("失败 %d 项:" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("escalation 全部通过")
    return 0


sys.exit(main())
