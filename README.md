# 记忆胶囊

> 中文名：**记忆胶囊**　包名：`brain_memory`（`import brain_memory`，pip 名不变）

可嵌入的长期记忆库。**纯 stdlib + sqlite3**，无 HTTP 服务、无 Redis、无模型权重、无 root 依赖。
专为 Android PRoot（Termux / proot-distro，aarch64 用户态）设计，所有降级路径自动兜底。

`当前阶段：Phase 2 完成` —— L0→L3 全链路 + 混合检索（FTS5 + 向量 RRF）+ 真巩固（EntityPage / DREAMS.md）。

---

## 30 秒上手

```python
import brain_memory as bm

db = bm.MemoryDB("/data/data/<pkg>/files/memory.db")

db.ingest("chat-1", "user", "我们决定数据库统一用 WAL 模式，别的都不用")
db.ingest("chat-1", "user", "今天天气不错")          # 门控拒收，但 L0 仍落盘

for m in db.recall("数据库选型", top_k=5):
    print(m.score, m.content, m.source_event_id)

print(db.forget(dry_run=True))    # 只算不写
print(db.dream())                 # 巩固：生成 L3 实体页 + DREAMS.md
db.close()
```

装：

```bash
pip install -e .            # 基础（stdlib + 纯 Python 向量）
pip install -e ".[vec]"     # 可选：sqlite-vec 加速
pip install -e ".[numpy]"   # 可选：numpy 暴力余弦
```

---

## 目录

```
brain_memory/
├── __init__.py     公开导出（Database / Config 是别名）
├── __main__.py     python -m brain_memory
├── cli.py          命令行
├── config.py       MemoryConfig（62 个配置项）
├── db.py           MemoryDB：连接 / PRAGMA / 锁 / 池 / flusher / 业务 API
├── errors.py       异常层级
├── types.py        Memory / Stats / ForgetDecision + 枚举
├── locks.py        写优先读写锁 + 全局锁
├── gate.py         编码门控（规则 + 可选 LLM）
├── promote.py      晋升五项检查
├── recall.py       FTS5 + 向量 + RRF 融合
├── vec.py          Embedder 接口 / 三级向量后端
├── forget.py       遗忘打分
├── dream.py        巩固（L3 实体页 + DREAMS.md）
└── schema.sql      L0-L4 全部 DDL（v2）
```

---

## 五个核心 API

| API | 签名 | 返回 |
|---|---|---|
| 构造 | `MemoryDB(path, config=None)` | 实例（幂等） |
| 写入 | `ingest(session_id, role, content, *, meta=None, created_at=None, promote=True)` | `raw_events.id` |
| 检索 | `recall(query, top_k=5, budget_tokens=2000, *, min_quality=None, fact_types=None, session_id=None, layer="L2")` | `list[Memory]` |
| 遗忘 | `forget(dry_run=True, *, hard=False, confirm=False, limit=None, older_than_days=None)` | `list[dict]` |
| 巩固 | `dream(*, persist=True, max_atoms=None)` | `str`（Markdown） |
| 关闭 | `close(timeout=5.0)` | `None` |

运维：`stats()` / `maintenance(level)` / `feedback(atom_ids, signal)`。
列举：`facts(*, session_id=None, fact_types=None, min_quality=0.0, limit=50)` —— 按类型直接列，**不做语义检索**，用来整批注入用户偏好。
兼容别名：`Database` = `MemoryDB`，`Config` = `MemoryConfig`（同一对象，零开销）。**dict 配置请走 `Config.resolve(dict)`**。

---

## 向量检索（Phase 2）

### 三级后端，逐级降级

| backend | 条件 | 检索方式 | 说明 |
|---|---|---|---|
| `sqlite-vec` | 扩展加载 + vec0 建表成功 | vec0 KNN | 最快，需 `[vec]` extra |
| `numpy` | 装了 numpy | 矩阵点积 | 中等，适合 < 5 万条 |
| `pure` | 纯 stdlib | 逐条余弦 | **默认**，任何环境都能跑 |
| `none` | `enable_vector=False` 或全挂 | 退化纯 FTS5 | 静默，不抛 |

向量本身存在 `atoms.embedding`（小端 float32 BLOB），vec0 只是**可选加速索引** ——
所以换后端不用重建数据，也不会因为扩展挂了就丢向量。

### Embedding 三选一

```python
# 1) 默认：纯 Python 哈希向量（零依赖零网络，跨进程稳定）
db = bm.MemoryDB(path)

# 2) 自备函数（任意模型，同步调用即可）
def my_embed(texts): return [[...], ...]
db = bm.MemoryDB(path, {"embedding_callable": my_embed})

# 3) OpenAI 兼容 /embeddings 端点
db = bm.MemoryDB(path, {
    "embedding_backend": "http",
    "embedding_endpoint": "http://127.0.0.1:8788/v1",
    "embedding_api_key": "sk-...",
    "embedding_model": "text-embedding-3-small",
})
```

能力边界要说清楚：哈希向量是**词形重叠**的高维投影，对「换序、跨标点、部分字面重叠」比
FTS5 宽容；「SQLite ↔ 本地数据库」这种真同义要靠真模型（`embedding_callable` 或 http）。

### RRF 融合

```
FTS5 召回 top-N  ─┐
                  ├─> RRF(score = Σ 1/(k + rank))  ─> quality 过滤 ─> token 预算截断
向量召回 top-N  ─┘
```

只看名次不看分数量纲，两路各自的第一名权重相同，所以不会因为 bm25 是负数、
余弦是 0~1 而被单边压制。`k` 默认 60，配置项 `rrf_k`。

实测（本机，4 条原子）：FTS5 单独命中 1 条 → 融合后返回 3 条，其中「嵌入式文件数据库」
被向量路从「SQLite」查询下召回。

### 两道安全阀

| 配置 | 默认 | 作用 |
|---|---|---|
| `vec_min_similarity` | 0.10 | 相似度低于此值的向量结果直接丢，避免噪声灌进 RRF |
| `vec_fail_limit` | 3 | embedding 连续失败 3 次 → 整个后端停用，退回 FTS5 |

第二道很关键：接了远程 embedding API 后，如果网络断了，没有熔断就会**每条消息都等一次超时**。
熔断后 `stats().vector_backend` 变成 `none`，业务无感。

---

## CLI

```bash
python -m brain_memory --db mem.db ingest "记得明天要改配置"
python -m brain_memory --db mem.db recall "数据库选型" --top-k 3 --json
python -m brain_memory --db mem.db forget                  # dry_run
python -m brain_memory --db mem.db forget --apply          # 落 silent/archive
python -m brain_memory --db mem.db stats
python -m brain_memory --db mem.db dream
python -m brain_memory --db mem.db maintain --level full
python -m brain_memory --db mem.db --config journal_mode_target=DELETE stats
```

`ingest` 既收位置参数也收 `--content/-c`；`-c -` 从 stdin 读；`--session` 默认 `cli`。

---

## 低置信度下探（escalation）

**默认关**。开了才在「L2 没结果 或 最高分 < `escalate_threshold`」时往下找：

```
L2 atoms（FTS5 + 向量 + RRF）
   ↓ 无结果 / 分数 < 0.35
L1 candidates（LIKE，候选事实）
   ↓ 仍然不够
L0 raw_events（LIKE，只看最近 N 条）
```

| 配置 | 默认 | 说明 |
|---|---|---|
| `enable_escalation` | `False` | 总开关，App 需要时再开 |
| `escalate_threshold` | 0.35 | L2 最高分低于此值才下探 |
| `escalate_max_results` | 3 | 下探最多补几条 |
| `escalate_l0_limit` | 50 | L0 只看最近多少条（**绝不全表扫**） |

返回结果用 `layer` 标层级（L2/L1/L0），`record_id` 指当前层主键
（L2=atom_id、L1=candidate.id、L0=raw_events.id）。
L0/L1 命中**不计访问计数** —— 它们不是 atom，没有 quality 可言。

实测开销：关 = 0.3ms，开 = 0.8ms（100 条库、无命中场景）。

---

## dream()：真巩固

```
1. 扫 quality >= dream_min_quality(0.6) 的活跃原子（quality 现场重算，不用缓存）
2. 按 entity 分组；没有 entity 的按 fact_type 兜底分组
3. 组内按内容相似度聚类（阈值 dream_cluster_similarity = 0.55）
4. 每个簇生成 Markdown 摘要 -> 写 entity_pages（dirty=0）
5. 汇总写 DREAMS.md（DB 同目录，可用 dreams_path 覆盖）
6. 返回本次巩固了哪些实体、哪些记忆
```

输出长这样：

```markdown
# DREAMS

- 扫描到高质量原子：3 条（quality >= 0.60）
- 生成实体页：3 个
- 涉及实体：PRAGMA, constraint, decision

## 巩固详情

## constraint（1 条）

- [q=1.00, atom#2] 这个项目所有配置必须走环境变量，不能硬编码
```

**不做的事**：不改模型权重、不决定删除、不在 recall 热路径执行。

---

## 降级矩阵

| 降级 | 判定 | 行为 | 可观测 |
|---|---|---|---|
| WAL → DELETE | `PRAGMA journal_mode=WAL` 读回 ≠ wal | 并发转全局互斥 | `stats().journal_mode` |
| sqlite-vec → numpy → pure → none | 逐级 try/except | 静默降级，最短路径仍是 FTS5 | `stats().vector_backend` |
| LLM 门控 → 规则 | 未配置 / 超时 / 非 2xx / JSON 失败 | 规则门控 + 60s 熔断 | `stats().llm_mode` |
| trigram → unicode61 | 建 FTS 表抛 no such tokenizer | 查询侧 bigram 展开 | `stats().fts_tokenizer` |
| 向量 embedding 连续失败 | `vec_fail_limit`（默认 3） | 停用向量后端，退回 FTS5 | `stats().vector_backend = "none"` |
| escalation 查询异常 | L1/L0 查询抛错 | 只返回 L2 结果 | 无 |

**降级不抛异常**。只有 `require_wal=True` / `require_vector=True` 才抛。

---

## 不变量

1. `raw_events` 只增不改不删
2. `atoms` 除 `state/quality/access_count/last_accessed_at/feedback/embedding` 外不可变
3. 冲突只产生 `supersedes_id` 边，旧原子标 `superseded`，不删
4. `forget` 默认 `dry_run=True`
5. 降级不抛异常
6. `atoms` / `atoms_fts` / 向量 同事务写入
7. `journal` 只增不改不删

---

## 检查

```bash
sh check.sh          # 静态扫描 + 语法 + 真建库体检
sh check.sh --full   # 再加 _smoke / _test_t4 / _test_phase2 / embed_demo
```

```
== 1/4 静态扫描 ==
  [OK]   禁用依赖 / 危险模式：0 命中
  [OK]   schema.atoms.state 与 ATOM_STATES 一致（4 个）
  [OK]   journal action 全部在 types 中声明
  [OK]   CLI 五个子命令齐备
== 2/4 语法编译 ==
  [OK]   compileall 通过
== 3/4 真建库体检 ==
  [OK]   journal_mode = 'wal' / synchronous = 1 / mmap_size = 0
  [OK]   busy_timeout = 5000 / temp_store = 2 / foreign_keys = 1
  [OK]   ingest -> recall 往返（event=1, hits=1）
== 4/4 全量测试 ==
  [OK]   _smoke.py / _test_t4.py / _test_phase2.py / examples/embed_demo.py
== 汇总 ==
  全部通过
```

---

## 文档

| 文件 | 内容 |
|---|---|
| `AGENT_RULES.md` | 项目硬约束（原始规则） |
| `PLAN.md` | 实施计划、schema、并发模型、验收映射 |
| `PUBLIC_API.md` | API 契约、异常、降级总表、不变量 |
| `EMBEDDING.md` | App 集成最佳实践 |

---

## 性能（本机 Android，非 PRoot）

| 操作 | 实测 |
|---|---|
| ingest 单条（含门控 + 晋升 + 向量） | < 3 ms |
| recall（FTS5 + 向量 + RRF，4 条库） | < 10 ms |
| recall（纯 FTS5，100 条库） | < 5 ms |
| 2 线程 × 50 次 ingest + recall | 115 ms |
| recall + escalation（无命中） | 0.8 ms |
| dream（4 条原子） | < 15 ms |
| 建库（首次，含 FTS + 向量探测） | ~15 ms |

PRoot 下按 1.5–3 倍 I/O 放大估算，仍远低于 PLAN 的 ingest <50ms / recall <100ms 预算。
纯 Python 向量后端在 5000 条以内可接受（~200ms 全扫），超过建议装 sqlite-vec 或 numpy。

---

## Phase 2 已知边界

- `sqlite-vec` 路径已实现但**本机无法实测**（Python 3.14 暂无对应 wheel），逻辑按官方 API 写，
  降级链路已实测。装得上的环境请跑一次 `sh check.sh --full` 复验。
- escalation（L2→L1→L0 下探）仍只留判定钩子。
- 哈希向量不是语义模型，真同义召回需配 `embedding_callable` 或 http 端点。
