"""embed_demo —— ingest -> recall(词法+向量) -> forget -> dream 全流程。

直接运行：
    python3 examples/embed_demo.py

本 demo 覆盖 Phase 2 的关键能力：
1. FTS5 词法召回
2. 向量召回（存「嵌入式文件数据库」，搜「SQLite」也能命中）
3. RRF 融合（两路结果按名次合并）
4. 真 dream()：生成 L3 实体页 + DREAMS.md

注意：demo 里用了一个「玩具语义 embedder」（同义词扩展 + 哈希），只为演示接口。
生产环境请用 HttpEmbedder 接真模型，或通过 embedding_callable 传自己的函数。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain_memory as bm
from brain_memory.vec import HashEmbedder

SEED = [
    ("s1", "user", "我们决定使用SQLite来做本地数据库，不要用PostgreSQL"),
    ("s1", "user", "这个项目所有配置必须走环境变量，不能硬编码"),
    ("s1", "assistant", "好的"),
    # 这条**不含** SQLite 字样，靠向量才能召回
    ("s2", "user", "我们决定本地存储方案用嵌入式文件数据库，不引入独立服务进程"),
    ("s2", "user", "今天天气不错"),
    ("s2", "user", "记得明天要把 PRAGMA mmap_size 设成 0"),
    ("s2", "user", "```python\nprint(1)\n```"),
]

# 玩具语义表：只在 demo 里用
SYNONYM = {
    "sqlite": "本地 文件 嵌入式 数据库",
    "嵌入式": "sqlite 本地 数据库",
}


def demo_embed(texts):
    base = HashEmbedder(384)
    expanded = []
    for text in texts:
        low = text.lower()
        extra = " ".join(v for k, v in SYNONYM.items() if k in low)
        expanded.append(text + (" " + extra if extra else ""))
    return base.embed(expanded)


def hr(title):
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def main():
    tmp = tempfile.mkdtemp(prefix="bm_demo_")
    db_path = os.path.join(tmp, "app_memory.db")
    failures = []

    def check(cond, msg):
        print(("  [OK]   " if cond else "  [FAIL] ") + msg)
        if not cond:
            failures.append(msg)

    try:
        hr("1. 初始化")
        cfg = bm.Config.resolve({"embedding_callable": demo_embed})   # dict 走 resolve，别直接 Config(dict)
        db = bm.Database(db_path, config=cfg)
        print("  journal_mode  :", db.journal_mode)
        print("  lock          :", db.sync_mode)
        print("  fts_tokenizer :", db.fts_tokenizer)
        print("  vector_backend:", db.vector_backend)
        print("  embedder      :", db.vector_store.embedder.name, "dim=%d" % db.vector_store.dim)
        print("  Database is MemoryDB:", bm.Database is bm.MemoryDB)
        check(db.vector_backend in ("pure", "numpy", "sqlite-vec"), "向量后端可用")

        hr("2. ingest（门控：该收的收，该拒的拒）")
        for session, role, content in SEED:
            before = db.stats().counts["l2_active"]
            event_id = db.ingest(session, role, content)
            after = db.stats().counts["l2_active"]
            mark = "收 -> L2" if after > before else "拒/未晋升"
            print("  #%-3d %-10s %s" % (event_id, mark, content.replace("\n", " ")[:36]))

        st = db.stats()
        print()
        print("  L0 事件 %d | L1 晋升 %d | L2 活跃 %d"
              % (st.counts["l0_events"], st.counts["l1_promoted"], st.counts["l2_active"]))
        check(st.counts["l0_events"] == len(SEED), "L0 全部落盘（拒收也不丢原始事件）")
        check(st.counts["l2_active"] >= 3, "有价值的消息晋升到 L2")

        hr("3. 词法检索（FTS5 + LIKE）")
        hits = db.recall("本地数据库", top_k=3)
        for m in hits:
            print("  atom#%-3d score=%.4f  %s" % (m.atom_id, m.score, m.content[:42]))
        check(len(hits) >= 1, "字面命中")

        hr("4. 向量检索：换种说辞也能搜到")
        print("  查询 = 'SQLite'（库里有一条记录完全不含 SQLite 字样）")
        vec_hits = db.recall("SQLite", top_k=5)
        for m in vec_hits:
            print("  atom#%-3d score=%.4f  %s" % (m.atom_id, m.score, m.content[:42]))
        has_literal = any("SQLite" in m.content for m in vec_hits)
        has_vector = any("嵌入式" in m.content for m in vec_hits)
        check(has_literal, "含 SQLite 的记录被召回（词法路）")
        check(has_vector, "不含 SQLite 的记录也被召回（向量路）")
        check(len(vec_hits) >= 2, "两路结果融合后是并集")

        hr("5. RRF 融合细节")
        import sqlite3 as _sq
        with db._read_conn() as conn:
            fts_rows = conn.execute(
                "SELECT COUNT(*) FROM atoms_fts WHERE atoms_fts MATCH ?", ('"SQLite"',)
            ).fetchone()[0]
        print("  FTS5 单独命中数 :", fts_rows)
        print("  融合后返回数    :", len(vec_hits))
        check(len(vec_hits) > fts_rows, "融合结果多于单路（向量补了候选）")

        hr("6. token 预算截断")
        tight = db.recall("SQLite", top_k=3, budget_tokens=8)
        print("  budget_tokens=8 ->", [(m.atom_id, m.truncated, len(m.content)) for m in tight])
        check(len(tight) >= 1, "预算很小也至少返回 1 条")

        hr("7. forget(dry_run=True) —— 只算不写")
        snapshot = db.stats().counts
        print("  初始扫描 ->", len(db.forget(dry_run=True)), "条")
        target = db.recall("SQLite", top_k=1)[0]
        db.feedback(target.atom_id, "noise")
        scan = db.forget(dry_run=True)
        for item in scan:
            print("    atom#%-3d action=%-6s q=%.3f acc=%d  %s"
                  % (item["atom_id"], item["action"], item["quality"],
                     item["access_count"], item["content"][:30]))
        check(len(scan) >= 1, "dry_run 识别出该静默的原子")
        check(db.stats().counts["l2_silent"] == snapshot["l2_silent"], "dry_run 不改库")

        hr("8. forget(dry_run=False) —— 落状态，不物理删除")
        applied = db.forget(dry_run=False)
        st2 = db.stats()
        print("  处理 %d 条 | l2_silent=%d | l0_events=%d"
              % (len(applied), st2.counts["l2_silent"], st2.counts["l0_events"]))
        check(st2.counts["l2_silent"] >= 1, "静默状态已落库")
        check(st2.counts["l0_events"] == len(SEED), "raw_events 一条没少")

        hr("9. dream() —— 真巩固（L3 实体页 + DREAMS.md）")
        db.feedback(target.atom_id, "useful")   # 恢复，让它能进巩固
        text = db.dream()
        print(text)
        st3 = db.stats()
        check(st3.counts["l3_pages"] >= 1, "entity_pages 已生成（%d 页）" % st3.counts["l3_pages"])
        check(os.path.exists(os.path.join(tmp, "DREAMS.md")), "DREAMS.md 已写盘")
        with db._read_conn() as conn:
            pages = conn.execute("SELECT entity_id, LENGTH(markdown) FROM entity_pages").fetchall()
        print("  实体页：", [(r[0], r[1]) for r in pages])
        check(len(pages) >= 1, "实体页内容非空")

        hr("10. 收尾")
        final = db.stats()
        print("  counts :", final.counts)
        print("  quality: avg=%.4f silent=%d archived=%d"
              % (final.avg_quality, final.silent_count, final.archived_count))
        print("  backend: journal=%s vec=%s embedder=%s fts=%s"
              % (final.journal_mode, final.vector_backend,
                 db.vector_store.embedder.name, final.fts_tokenizer))
        db.close()
        db.close()
        print("  close x2 正常")

        hr("结果")
        if failures:
            print("失败 %d 项：" % len(failures))
            for f in failures:
                print("  -", f)
            return 1
        print("全部通过。数据库：", db_path)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
