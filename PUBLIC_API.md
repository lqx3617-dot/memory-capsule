# PUBLIC_API.md — 记忆胶囊（brain_memory）公开 API 契约

**版本** v0.1（待确认）　**稳定性承诺** Phase 1 冻结下列签名；任何破坏性变更走主版本号。

---

## 0. 总则

- 库只暴露 `brain_memory` 一个入口包；`__init__.py` 仅导出 `MemoryDB`、`Memory`、`Stats`、异常类、`__version__`。
- 核心方法 **5 个**：`ingest` / `recall` / `forget` / `dream` / `close`；另有 **1 个构造器** `MemoryDB(...)` 与 **3 个运维方法** `stats` / `maintenance` / `feedback`。
- **降级不抛异常**：WAL 失败、vec 失败、LLM 失败一律静默降级 + 日志 + 可观测字段。只有**显式要求**（`require_vector=True` 等）才抛。
- **`recall` 热路径零写**。**`forget` 默认 dry_run**。**永不物理删除 L0**。

---

## 1. 数据结构

### Memory（`@dataclass(frozen=True)`）

| 字段 | 类型 | 必有 | 说明 |
|---|---|---|---|
| content | str | ✅ | 原子正文 |
| source_event_id | int \| None | ✅ | 溯源到 `raw_events.id`（规则强制字段） |
| confidence | float | ✅ | 0–1 |
| quality | float | ✅ | 0–1，打分时刻的缓存值 |
| atom_id | int | ✅ | `atoms.id` |
| fact_type | str | | `fact/decision/task/preference/constraint/bugroot` |
| entity_id | int \| None | | 实体链接结果 |
| access_count | int | | 曝光次数 |
| created_at | str | | ISO8601 UTC |
| last_accessed_at | str \| None | | |
| score | float | | 本次检索得分（0–1，便于阈值判断） |
| layer | str | | `"L2"` / `"L1"` / `"L0"`（escalation 时用） |
| truncated | bool | | content 是否被 token 预算截断 |

> 契约只保证 `content / source_event_id / confidence / quality` 四项必存在；其余为附加字段，允许后续增列（不破坏兼容）。

### ForgetDecision（`forget` 返回 list[dict] 的元素）

`atom_id, content(截断 120 字), quality, access_count, age_days, action("silent"|"archive"|"unlink"), reason(人类可读)`

### Stats（`@dataclass(frozen=True)`）

`counts: dict`（`l0_events, l1_pending, l1_promoted, l1_rejected, l2_active, l2_silent, l2_archived, l3_pages, sessions`）、`avg_quality`、`avg_confidence`、`silent_count`、`archived_count`、`journal_mode`（`"wal"`/`"delete"`）、`vector_backend`（`"none"`/`"sqlite-vec"`/`"numpy"`）、`llm_mode`（`"off"`/`"on"`/`"degraded"`）、`fts_tokenizer`（`"trigram"`/`"unicode61"`）、`db_bytes`、`sqlite_version`、`pending_access_flush`

---

## 2. 构造函数

```text
MemoryDB(path: str | os.PathLike, config: dict | MemoryConfig | None = None) -> MemoryDB
```

| 项 | 内容 |
|---|---|
| path | SQLite 文件路径；父目录不存在则创建；`:memory:` 允许（仅测试，无 WAL） |
| config | `None`＝全默认；dict 按 §3 键覆盖；优先级 `config > 环境变量 BRAIN_MEMORY_* > 默认` |
| 副作用 | 建库建表、设 6 条 PRAGMA、探测 WAL/vec/LLM、起 flusher 线程、打一行启动日志 |
| 幂等 | 同一 path 重复 `MemoryDB(...)` 不报错；schema 已存在只做 `user_version` 校验 |
| 返回 | 实例（支持 `with MemoryDB(p) as mem:`） |
| 抛异常 | `ConfigError`（配置非法）、`DatabaseError`（打不开/权限/损坏）、`SchemaVersionError`（库版本高于支持） |
| 不抛 | WAL 失败、vec 失败、LLM 不可用 → 降级 + 日志 |
| 日志 | `brain_memory: open path=<p> journal_mode=<wal|delete> sync=NORMAL mmap=0 busy=5000 sqlite=<ver> vec=<backend> llm=<mode>` |

---

## 3. 五个核心 API

### 3.1 `ingest`

```text
mem.ingest(session_id: str, role: str, content: str, *,
           meta: dict | None = None,
           created_at: str | None = None,
           promote: bool = True) -> int
```

| 项 | 内容 |
|---|---|
| session_id | 非空字符串，空串抛 `ValueError` |
| role | `user`/`assistant`/`system`/`tool`；其它值归一为 `user` 并记 journal（不抛） |
| content | 原样写入 L0；`None` 抛 `ValueError`；空串允许进 L0，但门控必拒 |
| meta | 任意可 JSON 序列化 dict；不可序列化抛 `ValueError` |
| created_at | 缺省 = 当前 UTC |
| promote | `True`＝同调用内尝试 L1→L2 晋升；`False`＝只落 L0 + L1(pending) |
| 返回 | `raw_events.id`（int，自增） |
| 副作用 | L0 必写；门控通过则写 candidates(pending)；promote 通过则写 atoms + atoms_fts + journal，并回填 `source_event_id` |
| 抛异常 | `ValueError`、`ClosedError`、`LockTimeoutError`、`DatabaseError` |
| **不抛** | 门控拒收（照常返回 event_id）、LLM 失败（走规则）、去重命中（记 merge） |
| 性能 | < 50ms/条 |
| 线程安全 | ✅（写锁串行） |

### 3.2 `recall`

```text
mem.recall(query: str, top_k: int = 5, budget_tokens: int = 2000, *,
           min_quality: float | None = None,
           fact_types: list[str] | None = None,
           session_id: str | None = None,
           layer: str = "L2") -> list[Memory]
```

| 项 | 内容 |
|---|---|
| query | 空串/纯空白/纯停用词 → 返回 `[]`（不抛） |
| top_k | `<=0` → `[]`；上限 `100`（超出按 100 处理） |
| budget_tokens | `<=0` 抛 `ValueError`；预算按 `ceil(len(content)/1.7)` 估算；首条即超预算时截断 content 并置 `truncated=True`，保证至少 1 条 |
| min_quality | 缺省＝配置 `recall_min_quality(0.0)`，只过滤不报错 |
| fact_types | 白名单过滤 |
| session_id | 限定来源会话（走 `raw_events` join） |
| layer | Phase 1 只接受 `"L2"`；传 `"L1"/"L0"` 且 `enable_escalation=False` → 记 warning 并按 L2 处理，**不抛** |
| 返回 | 按 `score` 降序的 `list[Memory]`，已剔除 `state != 'active'` 的原子 |
| 打分 | `score = 0.55*norm_bm25 + 0.35*quality + 0.10*recency`（权重可配） |
| 副作用 | **零数据库写**；只往内存 `_access_delta` 累加（flusher 异步刷盘） |
| 抛异常 | `ValueError`、`ClosedError`、`LockTimeoutError`、`DatabaseError` |
| 降级 | vec 不可用 → 纯 FTS5，结果照常返回；FTS 表空 → 返回 `[]` |
| 性能 | < 100ms（atoms < 10k） |
| escalation 骨架 | `max(score) < escalate_threshold(0.35)` 且配置开启时，Phase 2 会扩展到 L1/L0；Phase 1 只记 warning 日志 |

### 3.3 `forget`

```text
mem.forget(dry_run: bool = True, *,
           hard: bool = False,
           confirm: bool = False,
           limit: int | None = None,
           older_than_days: float | None = None) -> list[dict]
```

| 项 | 内容 |
|---|---|
| dry_run | 默认 `True`：只计算并返回列表，**库内一行不改** |
| hard | 仅 `dry_run=False and hard=True and confirm=True` 才物理删 `atoms` 行（`raw_events` 永不动） |
| confirm | `hard=True` 而 `confirm=False` → 抛 `ValueError`（防误删） |
| limit | 单次最多处理条数，`None`＝全部（App 空闲时调用） |
| older_than_days | 仅看创建时间早于 N 天的原子 |
| 返回 | `list[dict]`（`ForgetDecision` 形态），`action` ∈ `silent/archive/unlink` |
| 副作用（非 dry_run） | `UPDATE atoms SET state=?, state_changed_at=?`（silent/archive）；`hard` 时 `DELETE`；均写 journal |
| 抛异常 | `ValueError`、`ClosedError`、`LockTimeoutError`、`DatabaseError` |
| 不抛 | 无候选时返回 `[]` |

### 3.4 `dream`

```text
mem.dream(*, persist: bool = True, max_atoms: int | None = None) -> str
```

| 项 | 内容 |
|---|---|
| 语义 | 执行一次巩固（L2→L3 摘要、episodic 语义化、写 `DREAMS.md`） |
| Phase 1 | **桩实现**：不写 L3，写 journal(`dream`)，返回固定说明文本（含 atoms 数量与「L3 巩固在 Phase 2 启用」），**不抛异常** |
| Phase 2 | 返回生成的摘要 Markdown 全文；`persist=True` 时同时落 `entity_pages` 与 `DREAMS.md` |
| 抛异常 | `ClosedError`、`LockTimeoutError`、`DatabaseError`（Phase 2 才可能） |
| 约束 | 不在 `recall` 热路径被调用；不做任何权重改动 |

### 3.5 `close`

```text
mem.close(timeout: float = 5.0) -> None
```

| 项 | 内容 |
|---|---|
| 时序 | 置 `_closed` → 等在飞操作（≤timeout）→ flush 访问计数 → `wal_checkpoint(TRUNCATE)`（仅 WAL）→ 关 3 个连接 |
| 幂等 | 重复调用无副作用、不抛 |
| 之后调用 | 任何 API 抛 `ClosedError` |
| 兜底 | 未显式 close 时由 `__del__`/`atexit` 尽力 flush，但不保证 checkpoint |

---

## 4. 运维方法

### 4.1 `stats() -> Stats`
只读、不加写锁；返回 §1 的 `Stats`。缺表（未初始化完成）时对应计数为 0。

### 4.2 `maintenance(level: str = "normal") -> dict`
`level` ∈ `"light"`（`PRAGMA optimize`）/`"normal"`（+ `wal_checkpoint(TRUNCATE)`）/`"full"`（+ `VACUUM`）。
返回 `{"journal_mode", "checkpointed_pages", "optimized", "vacuumed", "duration_ms"}`。
**禁止在热路径调用**；DELETE 模式下 `VACUUM` 可能数秒，建议充电空闲。

### 4.3 `feedback(atom_ids: int | list[int], signal: str) -> int`
`signal` ∈ `used(1.0)` / `useful(0.8)` / `noise(0.0)`。写入 `atoms.confidence_adj`? 不 —— 写入内存映射 + 异步刷 `quality` 输入列。
返回受影响行数；非法 signal 抛 `ValueError`。**（决策 b，待确认后生效）**

---

## 5. 异常体系

```
BrainMemoryError(Exception)
├── ConfigError            # 配置类型/范围非法
├── DatabaseError          # 打开失败、磁盘满、库损坏（包 sqlite3.Error）
├── SchemaVersionError     # user_version 高于本库支持
├── ClosedError            # close 之后调用
├── LockTimeoutError       # 锁等待超过 busy_timeout
└── ExtensionError         # 仅 require_vector=True 场景
    └── EmbeddingDimensionError
```

- `VectorUnavailable` / `LLMUnavailable` **是内部信号，不对外抛**。
- `ValueError` 用于参数非法（Python 惯例，不包进 `BrainMemoryError`）。
- `KeyboardInterrupt`/`SystemExit` 从不吞。

---

## 6. 降级总表

| 触发 | 检测点 | 行为 | 可观测 | 是否抛 |
|---|---|---|---|---|
| WAL 建不起来 | `PRAGMA journal_mode=WAL` 读回 ≠ wal | 转 DELETE，并发降为互斥 | `stats().journal_mode="delete"`、启动日志、journal(`degrade_wal`) | 否（`require_wal=True` 时抛） |
| sqlite-vec 不可用 | import / enable_load_extension / load_extension / vec0 建表 任一失败 | 纯 FTS5（可选 numpy 兜底 <5000 atoms） | `stats().vector_backend="none"` | 否（`require_vector=True` 时抛 `ExtensionError`） |
| trigram 分词不可用 | 建 FTS 表抛 `no such tokenizer` | unicode61 + 查询侧 bigram 展开 | `stats().fts_tokenizer` | 否 |
| LLM 不可用 | 未配置/超时(8s)/非2xx/解析失败 | 规则门控；60s 熔断 | `stats().llm_mode="degraded"` | 否（`raise_on_llm_error=True` 时抛） |
| 读连接池耗尽 | `queue.Empty`（5s） | 抛 `LockTimeoutError` | 日志 | 是 |

---

## 7. 并发契约（对外承诺）

1. **同一实例可被任意多线程共用**（`check_same_thread=False` 全部生效）。
2. **写串行、读并行**（WAL）；**全互斥**（DELETE）。
3. **不死锁**：所有锁带 5s 超时，超时抛 `LockTimeoutError`，绝不无限等待。
4. **不丢数据**：多步写同事务；访问计数允许丢增量，不允许丢事件与原子。
5. **`recall` 不写盘**：命中计数由 flusher 每 5s / 32 条批量提交。
6. **`close` 后可重复调用**；其他调用抛 `ClosedError`。
7. 不支持：跨进程共享同一实例、跨进程 fork 后复用连接。

---

## 8. 不变量（Invariants）

| 编号 | 不变量 |
|---|---|
| I1 | `raw_events` 只增不改不删 |
| I2 | `atoms` 除 `state/quality/access_count/last_accessed_at` 外不可变 |
| I3 | 冲突只产生 `supersedes_id` 边，旧原子仍在库 |
| I4 | `forget` 默认 `dry_run=True` |
| I5 | 降级不抛异常 |
| I6 | `atoms` 与 `atoms_fts` 永远同事务写入 |
| I7 | `journal` 只增不改不删 |

---

## 9. CLI（附带品，非主交付）

```
python -m brain_memory --db PATH ingest --session S --role user --content "…"
python -m brain_memory --db PATH recall "查询词" --top-k 5
python -m brain_memory --db PATH forget            # 默认 dry_run，打印表格
python -m brain_memory --db PATH forget --apply    # 真写状态（仍不物理删）
python -m brain_memory --db PATH forget --hard --confirm   # 物理删 atoms（仍保留 L0）
python -m brain_memory --db PATH stats --json
python -m brain_memory --db PATH dream
python -m brain_memory --db PATH maintain --level full
```

---

## 10. 待确认项（与 PLAN.md §12 对应）

| 编号 | 影响契约 |
|---|---|
| a | 是否把 API 计数表述为「1 构造 + 5 方法 + 3 运维」 |
| b | 是否采纳 `feedback()` 作为公开方法 |
| c | `hard` 语义：只删 atoms 行 vs 完全不删只标 `unlinked` |
| d | `atoms.state` 两列是否加（不加则 forget 无处落状态） |
| e | Phase 1 不建 vec0 表 |
| f | 中文 FTS 分词：接受双路回退，或强制要求 SQLite ≥ 3.34 |
