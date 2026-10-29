# PLAN.md — 记忆胶囊（brain_memory）长期记忆库

**版本** v0.1（待确认）　**目标环境** Android PRoot / Termux / aarch64 / Python 3.10+ / 无 root / 无 systemd
**规则来源** 仓库根 `README.md` 与 [`docs/AGENT_RULES.md`](AGENT_RULES.md)

> **本文是 v1.0 时期的实施计划，作为历史记录保留。** Phase 1 / Phase 2 都已落地，
> 文中「Phase 1 桩」「Phase 2 才启用」这类表述描述的是当时的计划状态，不是当前行为。
> 当前行为以 [`README.md`](../README.md) 与 [`PUBLIC_API.md`](PUBLIC_API.md) 为准。

---

## 0. 一句话

交付一个 `import brain_memory` 即用的纯 Python 库：stdlib + sqlite3 实现 **L0→L2 三级记忆管道 + FTS5 混合召回 + 温和遗忘**，零外部服务、零模型权重、三条降级路径（WAL→DELETE / vec→FTS5 / LLM→规则）全部自动兜底且**降级不抛异常**。

---

## 1. 环境事实 → 工程决策

| # | PRoot 事实 | 带来的风险 | 本库的决策 |
|---|---|---|---|
| 1 | ptrace 拦截每次 syscall，I/O 开销放大 1.5–3× | 每条 SQL 都慢 | 单事务批量提交、writer 复用同一连接、语句预编译缓存、避免逐条 autocommit |
| 2 | `/dev/shm` 可能不可用或受限 | WAL 的 shared-memory 索引可能建不起来 | 只用**文件式 WAL**（`-wal`/`-shm` 与 DB 同目录），不开 `locking_mode=EXCLUSIVE`，不开 shared-cache；WAL 失败即回退 DELETE |
| 3 | `mmap` 行为不稳定 | 崩溃 / SIGBUS | 强制 `PRAGMA mmap_size=0`，全部走 pread/pwrite |
| 4 | 无 root、无 systemd | 不能常驻守护 | 库内不起任何常驻服务；`maintenance()` 由 App 在充电/空闲时主动调用 |
| 5 | 不能可靠 fork | 多进程写同一 DB 会锁死 | 只用线程：**1 写连接 + 2 读连接 + 1 flusher 线程**，全在单进程内 |
| 6 | 扩展加载可能被禁（`enable_load_extension` 不可用） | sqlite-vec 加载失败 | try/except 包住全过程，失败静默降级纯 FTS5（见 §4） |
| 7 | 不允许引 torch / transformers / 模型权重 | 体积与架构限制 | 向量只走 sqlite-vec 或 numpy 余弦；LLM 只做可选 HTTP 调用；巩固不做任何权重改动 |

---

## 2. 分层与阶段划分

| 层 | 表 | 语义 | Phase 1 | Phase 2 |
|---|---|---|---|---|
| L0 | `raw_events` | append-only 原始事件，不可变，永不删 | ✅ 全量 | — |
| L1 | `candidates` | 门控抽取的候选事实，`status=pending` | ✅ | — |
| L2 | `atoms` | 晋升后的结构化事实 + FTS5 索引 | ✅ | 加 vec0 向量列 |
| L3 | `entity_pages` | 按实体聚合的 Markdown 摘要页 | ❌ 只建表不写 | ✅ 由 dream() 生成 |
| L4 | `journal` | append-only 审计日志 | ✅ | 扩充 action 枚举 |

**Phase 1 范围（本计划交付）**：L0→L2 管道、`recall`、`forget`、`stats`、`maintenance`、CLI、单元测试、`examples/embed_demo.py`。
**Phase 1 只做骨架不做实现**：`dream()`（返回说明字符串 + 写 journal）、向量索引（只做探测与降级，不建 vec0 表）、recall 的 L2→L1→L0 escalation（只留判定钩子）。
> 说明：验收标准第 6 条（模拟 vec 加载失败仍能 recall）必须在 Phase 1 通过，所以**扩展探测与降级链路 Phase 1 就完整实现**，只是不建向量表。

---

## 3. Schema 定稿（本文写于 v1 时期；当前 `user_version = 2`，差异见下）

### 3.1 raw_events（L0）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | = `ingest` 返回值 |
| session_id | TEXT | NOT NULL | 会话标识 |
| role | TEXT | NOT NULL | `user`/`assistant`/`system`/`tool`，其余归一为 `user` 并记 journal |
| content | TEXT | NOT NULL | 原文，不改写不打码 |
| meta_json | TEXT | NULL | 附加字段 |
| created_at | TEXT | NOT NULL | ISO8601 UTC，`2026-10-26T07:51:10Z` |

索引：`(session_id, id DESC)`、`(created_at)`。**无 UPDATE / 无 DELETE 路径**（`forget --hard` 也不动本表）。

### 3.2 candidates（L1）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | INTEGER | PK | |
| raw_event_id | INTEGER | NOT NULL FK→raw_events | 证据链 |
| fact_type | TEXT | NOT NULL | `fact`/`decision`/`task`/`preference`/`constraint`/`bugroot` |
| content | TEXT | NOT NULL | 规范化后的事实句 |
| status | TEXT | NOT NULL DEFAULT 'pending' | `pending`/`promoted`/`rejected`/`merged` |
| confidence | REAL | NOT NULL DEFAULT 0.5 | 门控/抽取置信度 |
| entity_hint | TEXT | NULL | 门控阶段识别到的实体名 |
| created_at | TEXT | NOT NULL | |
| resolved_at | TEXT | NULL | 晋升/拒收时间 |

索引：`(status, id)`、`(raw_event_id)`。唯一约束：`(raw_event_id, fact_type, content)` 防同事件重复抽取。

### 3.3 atoms（L2）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | INTEGER | PK | |
| entity_id | INTEGER | NULL FK→entities | 实体链接结果 |
| fact_type | TEXT | NOT NULL | 同上枚举 |
| content | TEXT | NOT NULL | 事实正文，FTS5 索引对象 |
| confidence | REAL | NOT NULL | 0–1 |
| quality | REAL | NOT NULL DEFAULT 1.0 | 见 §7，缓存值，由 forget/maintenance 重算 |
| access_count | INTEGER | NOT NULL DEFAULT 0 | 曝光次数 |
| last_accessed_at | TEXT | NULL | ISO8601 UTC |
| created_at | TEXT | NOT NULL | |
| supersedes_id | INTEGER | NULL FK→atoms | 冲突时指向被取代的旧原子 |
| **state** | TEXT | NOT NULL DEFAULT 'active' | **规则缺口补充**：`active`/`silent`/`archived`（`forget` 唯一可改的列） |
| state_changed_at | TEXT | NULL | 静默/归档时间 |
| source_event_id | INTEGER | NULL FK→raw_events | `recall` 必须回传的溯源字段 |
| source_candidate_id | INTEGER | NULL FK→candidates | |

索引：`(state, quality)`、`(entity_id)`、`(fact_type)`、`(supersedes_id)`、`(last_accessed_at)`。

### 3.4 entities

`id, name, type(user/project/concept), aliases_json, created_at`；唯一索引 `name COLLATE NOCASE`。

### 3.5 journal（L4，append-only）

`id, action, target_table, target_id, detail_json, created_at`
action 枚举：`ingest` `gate_reject` `gate_llm_fallback` `promote` `merge` `conflict` `silence` `archive` `revive` `unlink` `dream` `maintenance` `degrade_wal` `degrade_vec` `schema_migrate`。

### 3.6 atoms_fts

`CREATE VIRTUAL TABLE atoms_fts USING fts5(content, content='atoms', content_rowid='id', tokenize='trigram')`
- 优先 `trigram`（要求 SQLite ≥ 3.34，Python 3.10 自带一般 3.37+，中文子串匹配正确）；
- 建表失败（`no such tokenizer: trigram`）→ 回退 `unicode61 remove_diacritics 2`，**查询侧做中文 bigram 展开**（`长期记忆` → `"长期" OR "期记" OR "记忆"`）。
- 同步方式：**不用触发器**（PRoot 下多一次 syscall + 触发器难调试），由 `promote.py` 在写事务内显式 `INSERT INTO atoms_fts(rowid, content)`，与 atoms 插入同事务，保证一致。

### 3.7 entity_pages / vec

Phase 1 只建 `entity_pages(id, entity_id, markdown, dirty, updated_at)` 空表；`atoms_vec`（vec0）**不建**，等 Phase 2 且探测通过才建。

> 现状：vec0 在 sqlite-vec 可用时会建，不可用则降级；向量本体存在 `atoms.embedding`，vec0 只是可选加速索引。

### 3.8 迁移

`PRAGMA user_version` 记录版本；`0 → 2` 建全表（v1 → v2 增加 `atoms.embedding`）。读到高于本库支持的版本 → 抛 `SchemaVersionError`，**不自动降级、不改库**。

---

## 4. 三条降级路径

### 4.1 WAL → DELETE

| 项 | 内容 |
|---|---|
| 探测 | 开库后执行 `PRAGMA journal_mode=WAL` 并**读回返回值**（只信读回值，不信不报错） |
| 成功条件 | 读回值 `wal` |
| 失败处理 | 读回非 `wal` 或抛 `sqlite3.OperationalError` → 执行 `PRAGMA journal_mode=DELETE`，写 journal(`degrade_wal`)，`logging.WARNING` |
| 必设 PRAGMA | `synchronous=NORMAL`、`mmap_size=0`、`busy_timeout=5000`、`temp_store=MEMORY`、`foreign_keys=ON` |
| 日志 | 启动固定打一行：`brain_memory: journal_mode=wal|delete (requested=wal) db=<path>` |
| 连带影响 | DELETE 模式下并发模型从「读写并行」降为「全局互斥」（见 §5） |

### 4.2 sqlite-vec → 纯 FTS5

| 触发点 | 检测 | 行为 |
|---|---|---|
| `import sqlite_vec` ImportError | 未装 extra | `vector_backend="none"` |
| `conn.enable_load_extension` 抛（编译期禁扩展） | AttributeError / OperationalError / NotSupportedError | 同上，日志一次，后续不再尝试 |
| `load_extension` / `vec_version()` 抛（.so 架构不符 aarch64） | 任意 Exception | 同上 |
| `vec0` 建表失败 / 维度与配置不符 | OperationalError / 维度比对 | 同上，必要时 `EmbeddingDimensionError`（仅 `require_vector=True` 时抛） |
| numpy 兜底 | 仅当 `enable_numpy_fallback=True` 且 `atoms < numpy_fallback_max_atoms(5000)` | `vector_backend="numpy"`，全表暴力余弦 |

**硬性**：整个过程不得让 `recall` 失败。降级后 `stats().vector_backend` 如实反映，写 journal(`degrade_vec`) 一次。

### 4.3 LLM → 关键词规则

| 触发点 | 检测 | 行为 |
|---|---|---|
| 未配置 `llm_endpoint` / `llm_api_key` / `llm_model` | config 检查 | `llm_mode="off"`，直接走规则 |
| 连接/超时（默认 8s，PRoot 下不放大） | `urllib` 异常 | 规则兜底 + `llm_mode="degraded"` |
| HTTP 非 2xx / 返回非 JSON / 字段缺失 | 解析异常 | 同上 |
| 熔断 | 同一进程内失败后 `llm_cooldown_s=60` 不再尝试 | 期间全走规则 |

**规则门控**（永远可用的基线）：关键词 + 长度 + 句式三维打分
- 收（→L1）：含决策/偏好/约束/Bug 根因/项目惯例特征词（`用 X 不用 Y`、`必须`、`不要`、`约定`、`根因`、`以后都`、`我习惯`、`这个项目`…）
- 拒：`今天天气不错`、纯寒暄、纯调试输出、纯日志行、纯代码块、单字、长度 < `gate_min_chars(4)`
- 打分 ≥ `gate_threshold(0.5)` 才进 L1；拒收写 journal(`gate_reject`)，**L0 照样落盘，不抛异常**

---

## 5. 并发模型

### 5.1 连接拓扑

```
process
├── MemoryDB
│   ├── writer   : 1 个 sqlite3.Connection  (check_same_thread=False，由 writer_lock 独占)
│   ├── readers  : 2 个 Connection 池        (默认 2，可配 1–3，queue 借还，各自 check_same_thread=False)
│   └── flusher  : 1 个 daemon 线程          (每 5s 或积压 ≥32 条，把内存计数批量刷盘)
└── 调用线程    : 任意多个，只能通过上述资源访问 DB
```

### 5.2 锁策略（按 journal_mode 分流）

| 模式 | 锁 | 效果 |
|---|---|---|
| WAL | `_rw_lock`（写优先读写锁）：writer 独占；readers 并发共享 | 读写并行，成功率最高 |
| DELETE | 降级为**单把全局互斥锁**（reader 与 writer 共用） | 吞吐下降，但杜绝 SQLITE_BUSY 抖动 |

- 所有锁 `acquire(timeout=busy_timeout/1000=5s)`，超时抛 `LockTimeoutError`（有超时上限 → 不会真死锁，只会明确失败）。
- **锁序铁律**：绝不在持有读锁时申请写锁；晋升 / 去重 / 冲突检测全在 writer 事务内完成，不跨锁。
- `recall` 只借读连接，**全程零写**；命中信息进 `_access_delta: dict[int,int]`（内存，`threading.Lock` 保护）。
- flusher 刷盘时取 writer 锁，一条 `UPDATE atoms SET access_count=access_count+?, last_accessed_at=? WHERE id=?` 走 `executemany`，单事务。
- `close()` 时序：`_closed=True` → 等在飞操作（计数器 + Condition，上限 `timeout=5s`）→ 强制 flush → `wal_checkpoint(TRUNCATE)`（仅 WAL）→ 关 3 个连接 → 幂等。

### 5.3 不死锁 / 不丢数据的保证

1. 单一写入口，写永远串行 → 不存在写写竞争。
2. 锁带超时 → 卡住必抛异常，不会永久挂起。
3. 所有多步写在同一事务内（`atoms` 插入 + `atoms_fts` 插入 + `journal` 插入要么全成要么全不成）。
4. 访问计数即使 flusher 尚未刷盘也不丢语义：丢的只是 `access_count` 增量，不影响数据完整性。

---

## 6. 性能预算

| 操作 | 目标 | 手段 | 验证方式 |
|---|---|---|---|
| `ingest` 单条 | < 50ms | WAL + `synchronous=NORMAL` + 单事务 + 预编译语句 | `tests/test_perf.py`（**计划中，尚未实现**）打 p50/p95 |
| `recall` | < 100ms（atoms < 10k） | FTS5 MATCH + `LIMIT top_k*4` 预筛 + 内存打分 + 零写 | 同上 |
| `forget(dry_run)` 全扫 10k | < 1.5s | 只查 `id, quality, access_count, last_accessed_at`（覆盖索引） | 同上 |
| `close` | < 300ms | checkpoint(TRUNCATE) 后立即关 | 验收脚本打印总耗时 |

DELETE 模式若实测超预算，退路是 `commit_batch`（默认 1，可配 8）：把连续 ingest 合并提交，代价是最多丢最后一批未提交事件。

---

## 7. 质量模型与遗忘

`quality = clamp01(feedback × decay × connectivity)`

| 因子 | 取值 |
|---|---|
| feedback | `used=1.0` / `useful=0.8` / 未反馈默认 `1.0` / `noise=0.0`（唯一归零项） |
| decay | `0.5 ** (Δdays / halflife)`；episodic=7d、semantic=90d、procedural=365d；Δ 由 `last_accessed_at`（空则 `created_at`）算 |
| connectivity | `1 + log2(1 + refs)`，`refs` = 被 entity_pages 引用 + 被其他原子 `supersedes` 指向，再 `clamp01` |

| 判定 | 条件（默认值） | 动作 | 可检索 |
|---|---|---|---|
| 静默 | `quality < 0.15` 且 `access_count < 3` | `state='silent'` | 否（仍在库，不删） |
| 归档 | `quality < 0.25` 且 `access_count ≥ 3` | `state='archived'` | 否（仍在库，可 revive） |
| 保留 | 其余 | 不变 | 是 |
| 物理删除 | 仅 `forget(hard=True, confirm=True)` | `DELETE FROM atoms` + `atoms_fts` 对应行，journal 记内容摘要与 id | 已删 |

`raw_events` 任何情况下都不删。

---

## 8. 包结构

```
brain_memory/
  __init__.py      # 只导出 MemoryDB / Memory / Stats / 异常
  types.py         # Memory, ForgetDecision, Stats (dataclass, frozen)
  errors.py        # 异常层级
  config.py        # MemoryConfig + 默认值 + env 覆盖
  db.py            # 开库、PRAGMA、降级探测、迁移、连接池、flusher、close
  schema.sql       # DDL（user_version=1）
  locks.py         # 写优先读写锁 + 超时
  gate.py          # 规则门控 + LLM 门控（可选）
  promote.py       # 五项检查（价值/证据/实体链接/去重/冲突）
  recall.py        # 查询解析 + FTS5 构造 + 打分 + 预算截断
  forget.py        # 判定 + 状态迁移
  dream.py         # 巩固：L3 实体页 + DREAMS.md
  vec.py           # sqlite-vec 探测 / numpy 兜底
  cli.py           # python -m brain_memory ...
tests/             # 临时目录真库，不 mock 存储层
examples/embed_demo.py
PLAN.md  PUBLIC_API.md
```

---

## 9. Phase 1 任务拆解

| # | 任务 | 依赖 | 产出 |
|---|---|---|---|
| T1 | `errors/config/types` + 包骨架 | — | 可 import，异常体系就位 |
| T2 | `db.py`：开库、六条 PRAGMA、WAL→DELETE 判定、日志打印 | T1 | 验收 1 |
| T3 | `schema.sql` + 迁移 + FTS5 建表（trigram→unicode61 回退） | T2 | 表结构可用 |
| T4 | `gate.py` 规则门控 + LLM 可选 + 熔断 | T2 | 验收 3 |
| T5 | `promote.py` 五检查（含 dedup 与 supersede 边） | T3,T4 | L1→L2 通路 |
| T6 | `recall.py`：解析、MATCH、打分、预算截断、escalation 钩子 | T5 | 验收 2 |
| T7 | `forget.py` + `stats()` + `maintenance()` + `dream()` 桩 | T5 | 验收 4 |
| T8 | `locks.py` 接入 + flusher + 并发测试 + perf + CLI + demo | T6,T7 | 验收 5,6,7,8 |

预估 Phase 1：**3–4 个工作日**（我这边一轮交付 T1–T7，T8 与实测一起）。

---

## 10. 验收映射

| 规则验收条 | 实现点 | 测试 |
|---|---|---|
| 1 初始化成功 / 重复初始化不报错 / 打印 journal_mode | `db.py` 幂等打开 + 日志 | `test_init.py` |
| 2 ingest→recall 召回关键事实且带 source_event_id | `promote` 回填 `source_event_id`，`recall` 透传 | `test_roundtrip.py` |
| 3 「今天天气不错」被拒收 | `gate.py` 规则 | `test_gate.py` |
| 4 `forget(dry_run=True)` 返回列表且库不变 | `forget.py` 只读分支 + 前后 `stats` 比对 | `test_forget.py` |
| 5 两线程各 100 次 ingest+recall 不报错不死锁 | 读写锁 + 超时 | `test_concurrency.py` |
| 6 不装 vec 仍能 recall | `vec.py` 探测降级 | `test_vec_fallback.py` |
| 7 demo 可运行 | `examples/embed_demo.py` | 手动跑 |
| 8 PRoot 实测耗时打印 | `demo` 与 `test_perf.py`（**计划中**） | 实测报告 |

---

## 11. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| PRoot 下 WAL 建不起来（`-shm` 受限） | 并发降级为互斥 | 已设计 DELETE 分支；实测确认 |
| 中文 FTS5 分词差 | 召回漏 | trigram 优先；unicode61 时查询侧 bigram 展开 |
| SQLite 版本 < 3.34 无 trigram | 建表失败 | 已留回退；启动日志打印 `sqlite3.sqlite_version` |
| DELETE 模式 `forget` 全扫超时 | 卡 UI | 分页 + `limit` 参数 + App 空闲时调用 |
| 规则缺口：`atoms` 无状态列 | forget 无处落状态 | 已补 `state`/`state_changed_at`（需确认，见 §12-d） |
| LLM 门控被 PRoot 网络拖慢 | ingest 变慢 | 8s 超时 + 60s 熔断；默认 `enable_llm_gate=False` |

---

## 12. 待拍板的 6 个决策

| 编号 | 问题 | 我的建议 |
|---|---|---|
| a | 规则说「五个 API」但列了 6 条（含 close） | 界定为：1 构造 + 5 方法（ingest/recall/forget/dream/close），另加 2 个运维方法 stats/maintenance |
| b | quality 公式需要 feedback，但五个 API 里没有反馈入口 | 新增 `mem.feedback(atom_ids, signal)`（signal: used/useful/noise）；`recall` 自动记 used |
| c | 「绝不物理删除」与「`forget --hard`」冲突 | `hard=True` 仅删 `atoms` 行（`raw_events` 永久保留），且需 `hard=True and confirm=True` 双开关，journal 留痕 |
| d | `atoms` 表缺 `state` 列，forget 无法表达静默/归档 | 加 `state`/`state_changed_at` 两列（默认 `active`） |
| e | Phase 1 是否建向量表 | 不建；只做探测 + 降级（验收 6 依赖此链路） |
| f | 中文分词依赖 SQLite ≥ 3.34 的 trigram | 接受双路回退；若爸爸要求绝对稳，就把 trigram 改为主路径并写入 README 依赖说明 |
