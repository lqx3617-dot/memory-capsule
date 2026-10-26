"""T4-T7 专项测试：门控 / 晋升 / 检索降级 / 遗忘 / 并发。"""
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain_memory as bm
from brain_memory import gate as gate_mod

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


def main():
    tmp = tempfile.mkdtemp(prefix="bm_t4_")
    path = os.path.join(tmp, "t.db")

    section("1. 门控：该收的收")
    g = gate_mod.RuleGate(bm.MemoryConfig())
    accept_cases = [
        ("我们决定数据库统一用 WAL 模式，别的都不用", "decision"),
        # constraint 命中 2 个（必须/不能）> convention 命中 1 个（这个项目），按分高取 constraint
        ("这个项目所有配置必须走环境变量，不能硬编码", "constraint"),
        ("这个项目按规矩所有 PR 都要过 CI，我们的规范是先跑 check.sh", "convention"),
        ("我习惯把日志写到 files/logs 下面", "preference"),
        ("根因是 protobuf 被降到 3.10 导致导入失败", "bugroot"),
        ("记得明天要把 mmap_size 设成 0", "task"),
    ]
    for text, want in accept_cases:
        r = g.evaluate(text)
        check(r.accepted, "收 -> " + text[:26] + "  (conf=%.3f type=%s)" % (r.confidence, r.fact_type))
        check(r.fact_type == want, "   fact_type == %s（实际 %s）" % (want, r.fact_type))

    section("2. 门控：该拒的拒")
    reject_cases = [
        ("今天天气不错", "chitchat"),
        ("好的", "too_short"),
        ("哈", "too_short"),
        ("哈哈哈哈哈哈", "repetition"),
        ("https://example.com/a/b", "url_only"),
        ('{"a": 1, "b": 2}', "json_blob"),
        ("2026-10-26 08:00:01 INFO starting service", "log_line"),
        ("。。。。。。。", "punctuation_only"),
    ]
    for text, want in reject_cases:
        r = g.evaluate(text)
        check(not r.accepted, "拒 -> " + text[:26] + "  (reason=%s)" % r.reason)
        check(r.reason == want, "   reason == %s（实际 %s）" % (want, r.reason))

    section("3. 边界：含信号词的闲聊不该被误杀")
    r = g.evaluate("今天天气不错，但是配置必须走环境变量")
    check(r.accepted, "「天气+必须」应被收（实际 accepted=%s reason=%s）" % (r.accepted, r.reason))

    section("4. 向量后端：sqlite-vec 不可用时自动选纯 Python 后端")
    db = bm.MemoryDB(path, {"enable_vector": True})
    check(db.vector_backend in ("pure", "numpy", "sqlite-vec"),
          "选到可用后端（实际 %s）" % db.vector_backend)
    check(db.journal_mode == "wal", "journal_mode 仍为 wal")
    ev = db.ingest("s1", "user", "我们决定数据库统一用 WAL 模式，别的都不用")
    hits = db.recall("WAL 模式")
    check(len(hits) >= 1, "vec 不可用时 recall 仍返回结果（%d 条）" % len(hits))
    check(all(h.source_event_id is not None for h in hits), "结果带 source_event_id")

    section("5. 去重：完全相同的第二条 -> merged")
    db.ingest("s1", "user", "我们决定数据库统一用 WAL 模式，别的都不用")
    st = db.stats()
    check(st.counts["l1_merged"] >= 1, "l1_merged >= 1（实际 %d）" % st.counts["l1_merged"])
    check(st.counts["l2_active"] == 1, "重复内容没有新增原子")

    section("6. 冲突：同类型、极性相反 -> superseded")
    db.ingest("s2", "user", "数据库统一用 WAL 模式")
    before = db.stats().counts["l2_active"]
    db.ingest("s2", "user", "数据库不用 WAL 模式了，改用 DELETE 模式")
    st2 = db.stats()
    print("  active: %d -> %d, superseded=%d" % (before, st2.counts["l2_active"], st2.counts["l2_superseded"]))
    check(st2.counts["l2_superseded"] >= 1, "旧原子被标记 superseded（不删除）")
    hits_new = db.recall("DELETE 模式")
    check(any("DELETE" in h.content for h in hits_new), "新原子可检索")
    hits_old = db.recall("WAL 模式")
    check(hits_old, "档案里仍有旧原子记录（未物理删除）")

    section("7. 遗忘：silent / archive / keep 三态")
    db.ingest("s3", "user", "记得把索引重建一遍")
    a_keep = db.recall("索引重建")[0]
    db.feedback(a_keep.atom_id, "useful")
    scan = db.forget(dry_run=True)
    check(not any(d["atom_id"] == a_keep.atom_id for d in scan), "useful 的原子不在遗忘列表")

    db.feedback(a_keep.atom_id, "noise")
    scan2 = db.forget(dry_run=True)
    check(any(d["atom_id"] == a_keep.atom_id and d["action"] == "silent" for d in scan2), "noise + 低曝光 -> silent")

    for _ in range(5):
        db.recall("索引重建")
    db.flush_access()
    db.feedback(a_keep.atom_id, "noise")
    scan3 = db.forget(dry_run=True)
    hit = [d for d in scan3 if d["atom_id"] == a_keep.atom_id]
    check(hit and hit[0]["action"] == "archive", "noise + 高曝光 -> archive（实际 %s）" % (hit[0]["action"] if hit else "无"))

    section("8. 危险操作护栏")
    try:
        db.forget(dry_run=False, hard=True, confirm=False)
        check(False, "hard 缺 confirm 应抛 ValueError")
    except ValueError:
        check(True, "hard 缺 confirm 抛 ValueError")
    try:
        db.feedback(1, "bogus")
        check(False, "非法 signal 应抛 ValueError")
    except ValueError:
        check(True, "非法 signal 抛 ValueError")

    section("9. dream() 桩")
    text = db.dream()
    check(isinstance(text, str) and text.startswith("# DREAMS"), "dream 返回 DREAMS Markdown")
    check("未改动任何模型权重" in text, "dream 声明不做权重改动")

    section("10. 并发：2 线程 x 50 次 ingest + recall")
    errs = []

    def worker(tag):
        try:
            for i in range(50):
                if i % 5 == 0:
                    db.ingest("c%d" % tag, "user", "记得处理事务"
                              "编号%d的第%d项，必须写日志" % (tag, i))
                db.recall("事务")
        except Exception as exc:
            errs.append(exc)

    t0 = time.monotonic()
    ths = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    cost = (time.monotonic() - t0) * 1000
    check(not errs, "并发无异常（耗时 %.0f ms）" % cost)
    if errs:
        print("   首个异常:", repr(errs[0]))

    section("11. 关库后行为")
    db.close()
    for fn, name in (
        (lambda: db.stats(), "stats"),
        (lambda: db.recall("x"), "recall"),
        (lambda: db.ingest("s", "user", "必须做点什么"), "ingest"),
        (lambda: db.forget(), "forget"),
        (lambda: db.dream(), "dream"),
    ):
        try:
            fn()
            check(False, name + " 关闭后应抛 ClosedError")
        except bm.ClosedError:
            check(True, name + " 关闭后抛 ClosedError")

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 60)
    if FAILS:
        print("失败 %d 项:" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("T4-T7 全部通过")
    return 0


try:
    code = main()
finally:
    shutil.rmtree(tempfile.gettempdir(), ignore_errors=False) if False else None
sys.exit(code)
