"""Phase 2 专项：向量后端 / 同义召回 / RRF 融合 / 迁移 / 真 dream。"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain_memory as bm
from brain_memory.db import SCHEMA_VERSION
from brain_memory.vec import HashEmbedder, pack_vector, unpack_vector, cosine

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def section(t):
    print()
    print("-" * 62)
    print(t)
    print("-" * 62)


# 演示用「语义」embedder：把产品名扩展成同义描述再哈希。
# 生产请接 HttpEmbedder 或自备真模型，这里只为验证接口与熔合链路。
SYNONYM = {
    "sqlite": "本地 文件 嵌入式 数据库",
    "嵌入式": "sqlite 本地 数据库",
    "postgresql": "服务端 远程 数据库",
}


def demo_embed(texts):
    base = HashEmbedder(384)
    expanded = []
    for text in texts:
        low = text.lower()
        extra = " ".join(v for k, v in SYNONYM.items() if k in low)
        expanded.append(text + (" " + extra if extra else ""))
    return base.embed(expanded)


def main():
    tmp = tempfile.mkdtemp(prefix="bm_p2_")

    section("1. 向量编解码")
    vec = [0.6, 0.8, 0.0]
    blob = pack_vector(vec)
    back = unpack_vector(blob)
    check(len(blob) == 12, "float32 打包长度正确（3 维 = 12 字节）")
    check(all(abs(a - b) < 1e-6 for a, b in zip(vec, back)), "打包/解包往返一致")
    check(abs(cosine([1, 0], [1, 0]) - 1.0) < 1e-9, "同向向量余弦 = 1")
    check(abs(cosine([1, 0], [0, 1])) < 1e-9, "正交向量余弦 = 0")

    section("2. 默认后端（无 sqlite-vec / 无 numpy）")
    db = bm.MemoryDB(os.path.join(tmp, "a.db"))
    print("  backend =", db.vector_backend, "| embedder =", db.vector_store.embedder.name)
    check(db.vector_backend in ("pure", "numpy", "sqlite-vec"), "自动选到可用后端")
    check(db.vector_store.dim == 384, "维度 384")

    section("3. embedding 是否随 atoms 落库")
    db.ingest("s1", "user", "我们决定使用SQLite来做本地数据库，不要用PostgreSQL")
    with db._read_conn() as conn:
        row = conn.execute("SELECT id, embedding FROM atoms LIMIT 1").fetchone()
    check(row is not None and row[1], "atoms.embedding 已写入（%d 字节）" % (len(row[1]) if row and row[1] else 0))

    section("4. 词法检索（FTS5 + LIKE）")
    hits = db.recall("本地数据库")
    check(len(hits) >= 1 and "SQLite" in hits[0].content, "FTS 能按字面召回")

    section("5. 同义召回：存「本地数据库」搜「SQLite」")
    db2 = bm.MemoryDB(
        os.path.join(tmp, "b.db"),
        {"embedding_callable": demo_embed, "vec_dim": 384},
    )
    print("  backend =", db2.vector_backend, "| embedder =", db2.vector_store.embedder.name)
    check(db2.vector_store.embedder.name == "callable", "自定义 callable 生效")

    # 这条消息**不含** SQLite 字样，FTS 查不到
    db2.ingest("s1", "user", "我们决定本地存储方案用嵌入式文件数据库，不引入独立服务进程")
    lexical = db2.recall("SQLite")
    with db2._read_conn() as conn:
        fts_only = conn.execute(
            "SELECT COUNT(*) FROM atoms_fts WHERE atoms_fts MATCH ?", ('"SQLite"',)
        ).fetchone()[0]
    print("  FTS 命中数 =", fts_only, "| recall 返回 =", len(lexical))
    check(len(lexical) >= 1, "向量兜底把「嵌入式文件数据库」召回到「SQLite」查询下")
    check(all("SQLite" not in m.content for m in lexical), "召回内容里确实没有 SQLite 字样（纯靠向量）")

    section("6. RRF 融合：两路都召回时按名次融合")
    db3 = bm.MemoryDB(
        os.path.join(tmp, "c.db"),
        {"embedding_callable": demo_embed, "vec_dim": 384},
    )
    db3.ingest("s1", "user", "数据库统一用 WAL 模式，事务提交更快")
    db3.ingest("s1", "user", "本地数据库选型定为 SQLite，不要用服务端数据库")
    db3.ingest("s1", "user", "记得周末把索引重建一遍")
    fused = db3.recall("SQLite 数据库", top_k=3)
    for m in fused:
        print("    atom#%d score=%.4f  %s" % (m.atom_id, m.score, m.content[:38]))
    check(len(fused) >= 2, "融合后返回多条")
    check("SQLite" in fused[0].content, "含 SQLite 的那条排第一")
    check(any("WAL" in m.content for m in fused), "向量路把 WAL 那条也带进来了")

    section("7. 关掉向量：后端 none，recall 仍可用")
    db4 = bm.MemoryDB(os.path.join(tmp, "d.db"), {"enable_vector": False})
    check(db4.vector_backend == "none", "enable_vector=False -> none")
    db4.ingest("s1", "user", "这个项目所有配置必须走环境变量")
    check(len(db4.recall("环境变量")) >= 1, "无向量时 recall 正常")

    section("8. schema 迁移（v1 库 → 当前版本）")
    v1path = os.path.join(tmp, "v1.db")
    schema = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain_memory", "schema.sql"), encoding="utf-8").read()
    conn = sqlite3.connect(v1path)
    conn.executescript(schema)
    # 用当前 schema 建好，再「拆」回 v1：删掉后来加的列、把版本退回 1。
    # 不靠字符串替换 schema 文本 —— 那种写法 schema.sql 一改格式就静默失效，
    # 上一版就因为 user_version 从 2 变 3 而假绿了整个迁移测试。
    for col in ("embedding", "is_core"):
        try:
            conn.execute("ALTER TABLE atoms DROP COLUMN " + col)
        except sqlite3.Error:
            pass
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    db5 = bm.MemoryDB(v1path)
    with db5._read_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(atoms)").fetchall()]
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    check("embedding" in cols, "迁移后 atoms 有 embedding 列")
    check("is_core" in cols, "迁移后 atoms 有 is_core 列")
    check(ver == SCHEMA_VERSION, "user_version 升到 %d（实际 %s）" % (SCHEMA_VERSION, ver))
    db5.close()

    section("9. 真 dream()：L3 实体页 + DREAMS.md")
    db6 = bm.MemoryDB(os.path.join(tmp, "e.db"))
    db6.ingest("s1", "user", "我们决定使用 SQLite 作为本地数据库")
    db6.ingest("s1", "user", "SQLite 必须用 WAL 模式，事务提交更快")
    db6.ingest("s1", "user", "SQLite 不能并发写入，必须统一走单写连接")
    db6.ingest("s1", "user", "这个项目所有配置必须走环境变量")
    before_pages = db6.stats().counts["l3_pages"]
    text = db6.dream()
    st = db6.stats()
    print(text.split("## 巩固详情")[0].strip())
    check(text.startswith("# DREAMS"), "返回 DREAMS Markdown")
    check("巩固详情" in text, "包含巩固详情章节")
    check(st.counts["l3_pages"] >= 1, "entity_pages 写入成功（%d 页）" % st.counts["l3_pages"])
    check(st.counts["l3_pages"] > before_pages, "页数相比之前有增长")
    dreams_file = os.path.join(tmp, "DREAMS.md")
    check(os.path.exists(dreams_file), "DREAMS.md 已生成到 DB 同目录")
    if os.path.exists(dreams_file):
        body = open(dreams_file, encoding="utf-8").read()
        check("SQLite" in body, "DREAMS.md 里能看到实体名")
        check("atom#" in body, "DREAMS.md 里能看到具体记忆")

    section("10. dream 不删除、不改权重")
    check(db6.stats().counts["l2_active"] >= 3, "原子一个没少")
    check("未改动任何模型权重" in text, "明确声明不改权重")

    db.close()
    db2.close()
    db3.close()
    db4.close()
    db6.close()
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 62)
    if FAILS:
        print("失败 %d 项:" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("Phase 2 全部通过")
    return 0


sys.exit(main())
