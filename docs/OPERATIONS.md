# 运维手册 —— 出问题的时候看这一页

> 这份文档只回答一个问题：**记忆胶囊表现异常时，怎么判断是哪一层的问题、怎么修。**
> 九轮审计挖出的坑按「症状」重新组织，不按「模块」。

---

## 0. 三条命令先跑一遍

```bash
sh check.sh --full                       # 静态 + 建库 + 21 个测试脚本
python -m brain_memory --db mem.db stats # 各层计数与降级状态
```

或者直接在代码里要一份快照：

```python
st = db.stats()
print(st.counts)          # 各层计数
print(st.journal_mode)    # wal / delete / memory
print(st.vector_backend)  # sqlite-vec / numpy / pure / none
print(st.fts_tokenizer)   # trigram / unicode61
print(st.llm_mode)        # on / off / degraded
print(st.avg_quality)     # 快照平均值（不是现算值）
```

这四个字段是降级链的探针。**任何一项不是预期值，先看第 4 节。**

---

## 1. 症状速查表

| 症状 | 最可能的原因 | 跳到 |
|---|---|---|
| 说什么它都记不住 | 门控拒收（多数是正常行为）/ 问句 / 内容太短 | 2.1 |
| 记得住但召回不到 | silent 状态 / FTS 索引没建 / 向量熔断 | 2.2 |
| 记着旧版本，不认新说法 | 冲突下线 / 去重合并 | 2.3 |
| 越用越慢 | 纯 Python 向量 O(n) / 缺索引 | 2.4 |
| `DREAMS.md` 里全是陌生实体 | 孤儿页（1.3.6 已修，老库要跑一次全量 dream） | 2.5 |
| 库文件越来越大 | L0 只增不减（设计如此） | 2.6 |
| 偶发 `LockTimeoutError` | 长事务 / 嵌套写事务 / 多进程争锁 | 2.7 |
| 偶发 `ClosedError` | 关闭期还在调 API | 2.8 |
| 配置改了没效果 | 1.1.0 起删掉的 4 个键会抛错；其余看 2.9 | 2.9 |

---

## 2. 逐个展开

### 2.1 「它记不住」

先分清两件事：**没进 L1（门控拒收）** 还是 **进了但没晋升**。

```python
st = db.stats().counts
print('L0 原始事件', st['l0_events'])
print('L1 待审', st['l1_pending'], '已晋升', st['l1_promoted'],
      '被拒', st['l1_rejected'], '已合并', st['l1_merged'])
```

| 现象 | 含义 |
|---|---|
| `l0_events` 涨了，`l1_*` 全是 0 | 门控拒收。查 journal 里的 `gate_reject` 与 reason |
| `l1_rejected` 多 | 晋升阶段被拒：太短、confidence 低于 `promote_min_confidence` |
| `l1_merged` 多 | 被判重复，合并进旧原子 |

**门控拒收是设计行为**，不是故障。它会拒：寒暄、纯链接、代码块、日志行、**疑问句**，以及不带任何信号词的句子。

想让某条一定进来，用 `db.remember()`（跳过门控）：

```python
db.remember('s', '在杭州', fact_type='fact')
```

### 2.2 「记得住但召回不到」

按这个顺序排：

```sql
-- 1) 它在不在？状态是什么？
SELECT id, state, quality FROM atoms WHERE content LIKE '%关键词%';

-- 2) 有没有进 FTS 索引？
SELECT rowid FROM atoms_fts WHERE atoms_fts MATCH '关键词';

-- 3) 是不是被静默了？
SELECT id, action FROM journal WHERE action IN ('silent','archive') ORDER BY id DESC LIMIT 20;
```

| 第 1 步 | 第 2 步 | 结论 |
|---|---|---|
| 查不到 | — | 根本没进 L2，回看 2.1 |
| `silent` / `archived` | — | 被遗忘了。调 `feedback` 或阈值；重说一遍会触发复活 |
| `active` | 查得到 | 是打分或预算问题：调 `min_quality` / `top_k` / `budget_tokens` |
| `active` | **查不到** | **FTS 与 atoms 不同步**，需要重建索引 |

重建：

```python
db.conn.execute("INSERT INTO atoms_fts(atoms_fts) VALUES('rebuild')")
```

### 2.3 「记着旧版本，不认新说法」

查 journal：

```sql
SELECT action, target_id, detail_json FROM journal
WHERE action IN ('conflict','merge','revive') ORDER BY id DESC LIMIT 20;

SELECT id, content, supersedes_id FROM atoms WHERE state = 'superseded';
```

| journal 里有 | 含义 |
|---|---|
| `conflict` | 新说法把旧的下线了（正常） |
| `merge` | 新说法被判重复，合并进旧原子 —— **但合并只改 confidence/quality，不改 content** |
| `revive` | 重述唤醒了 silent 原子 |

**1.3.4 之前有个坑**：极性相反的两句（「统一用 SQLite」/「不用 SQLite」）会被当重复合并，
而合并不改 content —— 表现为「改主意它不认」。1.3.4 起去重会先比极性，不会了。

### 2.4 「越用越慢」

先看 `stats().vector_backend`：

| backend | 5000 条 recall 耗时 | 处置 |
|---|---|---|
| `sqlite-vec` | 最快 | 无需处理 |
| `numpy` | ~30ms | 无需处理 |
| `pure` | **~200ms** | 过万条就装 `.[numpy]` 或 `.[vec]` |

再确认索引在不在：

```python
print([r[0] for r in db.conn.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")])
```

`idx_atoms_state_id` 是 1.3.2 加的；老库打开时 `_ensure_indexes()` 会自动补，补不上就说明建索引时报错了，看日志。

### 2.5 「DREAMS.md 里全是陌生的实体」

孤儿实体页。**1.3.6 已修**：全量巩固时删掉本次没生成的页。

老库升级后要跑一次全量才会清：

```python
db.dream()                # 全量：会清理孤儿页
db.dream(max_atoms=100)   # 限量：不清理（本次没扫到不代表该消失）
```

清理条数记在 journal 的 `entity_pages_pruned`。

### 2.6 「库文件越来越大」

`raw_events` 只增不减，这是**设计不变量**，增长主要来自它。

```python
st = db.stats()
print('L0 事件', st.counts['l0_events'], '库大小', st.db_bytes)
```

| 情况 | 处置 |
|---|---|
| L0 大、L2 小 | 正常，门控在干活。VACUUM 能回收空间但不减行数 |
| silent 很多 | 跑 `forget(dry_run=False)` 落状态；silent 不占检索但占空间 |
| 想物理删除 | `forget(dry_run=False, hard=True, confirm=True)`，且 `allow_hard_delete=True`。**`raw_events` 仍不动** |

```python
db.maintenance('full')   # optimize + checkpoint + VACUUM，别在热路径跑
```

### 2.7 偶发 `LockTimeoutError`

两层锁：进程内读写锁 + SQLite 的 `busy_timeout`。

| 场景 | 判断 |
|---|---|
| 同时跑 `maintenance('full')` | VACUUM 占写锁数秒。**这条最常见** |
| 嵌套写事务 | 1.3.4 起直接抛 `DatabaseError: 不支持嵌套写事务`，不再等超时 |
| 多进程争写 | WAL 下只允许一个写者，另一个等 `busy_timeout`（默认 5s） |

```python
print(db.sync_mode)                 # 'rw' = 读写并行；'global' = 全局互斥
print(db.stats().journal_mode)      # 不是 wal 就说明降级了
```

### 2.8 偶发 `ClosedError`

关闭之后还有代码在调 API。`close()` 会立起 `_closing`，新操作立即被拒 —— 这是 1.1.0 修的竞态。

检查生命周期：`close()` 只在 Service 销毁时调一次，别每条消息 new/close。

### 2.9 配置改了没效果

| 情况 | 说明 |
|---|---|
| 传了 `commit_batch` / `raise_on_llm_error` / `max_candidates_per_ingest` / `numpy_fallback_max_atoms` | 这 4 个 1.1.0 删了，现在抛 `ConfigError` |
| 开了 `enable_llm_gate` 但不出网 | 还要 `llm_endpoint` + `llm_model`，且只在 0.42 ≤ conf < 0.78 才调 |
| 配了 `embedding_backend="http"` 但没走远程 | 还要 `embedding_endpoint` + `embedding_model`，缺一个就完全不做向量 |
| 想用便宜模型跑门控 | 配 `extract_llm_*`，留空则回落到 `llm_*` |

---

## 3. 定期维护

| 频率 | 动作 | 调用 |
|---|---|---|
| 每天 | 遗忘扫描（只看） | `db.forget(dry_run=True)` |
| 每天 | optimize + checkpoint | `db.maintenance('normal')` |
| 每周 | 遗忘落状态 | `db.forget(dry_run=False)` |
| 每周 | VACUUM | `db.maintenance('full')` |
| 空闲 | 巩固 | `db.dream()` |

**全部放在充电 + 灭屏时跑**，别在消息路径上。

---

## 4. 看起来像坏了，其实正常

| 现象 | 为什么正常 |
|---|---|
| 灌 20 条相似内容只剩 1 条 | 去重：相似度 ≥ 0.9 合并（「第 1 条」「第 2 条」这类编号内容尤其容易被并） |
| `recall` 返回空但库里明明有 | 那几条可能是 `silent` / `archived` |
| `stats().avg_quality` 偏高 | 它是**快照平均值**。要现算值看 `recall()` 返回的 `quality` |
| 门控把一句有用的话拒了 | 它不含信号词，或是疑问句。用 `remember()` 绕过 |
| `DREAMS.md` 里没提某条记忆 | `dream_min_quality` 默认 0.6，低质量不进巩固 |
| 换 embedding 模型后召回变差 | 旧向量没重算，跑 `db.reindex_vectors()` |

---

## 5. 升级与回滚

**升级原地进行**，不需要导出导入：

```python
db = bm.MemoryDB(path)   # 打开时自动 ALTER TABLE / 补索引 / rebuild FTS
```

| 检查 | 期望 |
|---|---|
| `PRAGMA user_version` | 1 → 2（v1 老库） |
| `atoms.embedding` | 自动补上 |
| `idx_atoms_state_id` | 自动补上 |
| 旧原子 | 还在，且能被 FTS `MATCH` 到 |

**回滚不安全**：升级后 `user_version=2`，旧版代码会抛 `SchemaVersionError`（有意如此，不瞎改）。要回滚先备份。

备份（WAL 模式）：

```bash
# 只拷 .db 会丢 WAL 里的内容
# 正确做法：先 close()（它会 checkpoint），再拷
cp memory.db memory.db.bak
```

---

## 6. 版本与修过的坑

| 版本 | 修了什么 | 症状关键词 |
|---|---|---|
| 1.0.0 | 首发 | — |
| 1.1.0 | quality 单一真源、称呼误合并、vec 距离量纲、检索分块 | 分数不准、误合并 |
| 1.2.0 | MCP 服务器、记忆代理、保活脚本 | 宿主接不进来 |
| 1.2.1 | Origin 校验、协议状态码、参数透传、门控判定顺序 | 安全问题、问句被当决策 |
| 1.2.2 | review-gate、分支保护 | 合并前没人审 |
| 1.3.0 | `__version__` 对齐、`convention` 类型、`reindex_vectors()` | 版本号不对、惯例类型丢失 |
| 1.3.1 | journal `silent` 幽灵值、冲突与降级留痕 | 审计缺记录 |
| 1.3.2 | `idx_atoms_state_id` | forget 多一次排序 |
| 1.3.3 | 嵌套事务、写锁重入、`GlobalLock` 改 RLock | 偶发锁超时 |
| 1.3.4 | **冲突判据误杀**、**去重吞掉更正** | 记忆莫名下线、改主意不认 |
| 1.3.5 | 升级路径验证 | 老库升级 |
| 1.3.6 | 孤儿实体页清理、DREAMS.md 原子写 | DREAMS.md 里全是陌生实体 |

---

## 7. 还是没头绪

按这个顺序收集信息，基本能定位到层：

```python
import brain_memory as bm
print(bm.__version__)
db = bm.MemoryDB(path)
print(db.stats().as_dict())
print([(r[0], r[1]) for r in db.conn.execute(
    "SELECT action, COUNT(*) FROM journal GROUP BY action")])
```

journal 的 action 分布往往一眼看出问题：`gate_reject` 暴涨 → 门控太严；
`conflict` 暴涨 → 判据太宽；`degrade_*` 出现 → 降级了；`revive` 多 → 遗忘太激进。
