# 更新记录

## v1.5.0 —— 2026-10-31

第二阶段：内分泌心境引擎。

### `brain_memory/endocrine.py`

四个激素量，各自有基线，偏离后按指数回归：

```
h(t+dt) = h_base + (h(t) - h_base) * exp(-LAM_H * dt)

h_sero 0.6  血清素（稳定）
h_dopa 0.4  多巴胺（兴奋）
h_cort 0.2  皮质醇（压力）
h_oxy  0.5  催产素（亲近）
LAM_H = 0.02 / 分钟  ->  半衰期约 35 分钟
```

存在 `system_state(key, value, updated_at)` 里，一行一个量。

### 事件扰动

```python
db.endocrine.update_hormones("insult", 1.0)   # 骂：cort +0.30, sero -0.20
db.endocrine.update_hormones("praise", 1.0)   # 夸：oxy +0.30, sero +0.10
```

内置七种：`insult` / `praise` / `joy` / `intimate` / `stress` / `calm` / `ignored`；
未知事件按「无扰动」处理，不抛异常。每次扰动写 journal 的 `hormone_shift`。

### f_endo：情绪系数乘在检索得分上

```
f_endo = 1 + 0.35 × (当前心情 × 记忆写入时的心情)
```

| 当前心情 | 记忆心情 | f_endo |
|---|---|---|
| +0.8 | +1.0（开心的事） | 1.28 |
| +0.8 | -1.0（难过的事） | 0.72 |
| 0 | 任意 | 1.00 |

所以**心情好时更容易想起开心的事**，心情差时反过来。

每条原子在写入时记一个 `mood`（当时的 valence 快照），recall 用它做同向放大。
乘完之后**重新排一次序** —— 只乘不排等于没生效，这个坑在实现时踩过。

### 零写的保证

`recall` 承诺不写库，所以 `mood()` 默认**纯读**（只做衰减计算，不落盘），
并且带 `endo_cache_seconds`（60 秒）缓存 —— 每条消息都读一次 `system_state` 太贵。

### schema v3 → v4

- `atoms` 加 `mood REAL NOT NULL DEFAULT 0`
- 新增 `system_state` 表（老库打开时由 `EndocrineSystem.ensure()` 幂等建）

### 演示

`examples/mood_demo.py` 走一遍对话主流程：骂一句 → 夸一句 → 心情好时写记忆 →
心情差时写记忆 → 心情好时检索看排序 → 三小时后回归基线。

### 关掉它

```python
db = bm.MemoryDB(path, {"enable_endocrine": False})   # f_endo 恒为 1.0
```
## v1.4.0 —— 2026-10-31

第一阶段升级：**不破坏现有结构，强化核心记忆**。四件事。

### 1. 时间压缩 dt_eff

衰减不再用真实年龄，先折算成等效天数：

| 真实年龄 | dt_eff |
|---|---|
| < 7 天 | 原样（近期该淡就淡） |
| 7~30 天 | `7 + (age-7) × 0.5` |
| > 30 天 | `18.5 + (age-30) × 0.15` |
| 任何情况 | 不超过 `age_compress_cap_d`（90） |

效果：180 天前的事实，原来 quality 衰减到 0.25（`dream` 已经不巩固它了），
压缩后等效 41 天、quality 0.73 —— **不会被当成「忘了」**。7 天内的行为完全不变。

五个参数全部可配（`age_compress_*`）。

### 2. 核心事实 `is_core`

`atoms` 加 `is_core` 列（schema v2 → v3，自动迁移）。

冲突检测时，如果**旧事实**是核心，新事实一律被拒：

```python
db.mark_core(atom_id)          # 标记
db.mark_core(atom_id, False)   # 取消
```

被拒时写 journal 的 `core_protect`，带上被拒内容的摘要 —— 谁想改、被谁挡了，都留痕。
适合身份、硬约束这类「不可能被改主意」的内容。

### 3. 预算式召回 `recall_with_budget()`

```python
items = db.recall_with_budget(user_text, total_budget=2500, facts_budget=800)
```

先用 800 token 把 `facts()`（偏好 / 约束 / 核心事实）钉死，余量才给 FTS5 / 向量。

为什么顺序不能反：「别给我发语音」按关键词根本检索不到，但它比任何一条语义命中都重要。
返回列表 PIN 在前、检索在后，超预算时最后一条会截断而不是整条丢掉。

### 4. `examples/soul_prompt_builder.py`

称呼规则放 prompt 最顶部：

```python
prompt = build_system_prompt(user_name="李跃铭", persona="你是爸爸的助手", memory_items=items)
```

第一行就是「用户叫李跃铭，必须用这个名字称呼，严禁使用「您」」。
`app_integration.py` 加了 `compose_system_prompt_v2()` 展示接法。

> 注：需求里的 `app/ai_autoreply.py` 与 `soul_prompt_builder` 不在本仓库，
> 这里给的是可复用的实现 + 接入示例，搬到你的 App 里改 import 即可。

### 测试

- `test_age_compress`：四个区间数值、封顶、180 天前后的对比
- `test_core_fact`：标记、保护、取消后恢复、非核心行为不变
- `test_recall_budget`：PIN 前缀、去重、极小预算、空 query

`check.sh --full` 覆盖 **24 个测试脚本 + 2 个示例**。
## v1.3.8 —— 2026-10-29

第十轮：追 `test_concurrency` 在 CI 上的偶发失败。

### 现象

同一个 commit，四档 Python 里随机挂一档；测试自己打印「全部通过」，但 `check.sh` 判它失败 ——
**断言全过，退出码非 0**。

### 这一轮做的（诊断，不是修复）

| 改动 | 目的 |
|---|---|
| `main()` 包 `try/except BaseException` + `traceback.print_exc()` | 下次挂的时候能看到栈，而不是只有一个非 0 退出码 |
| `os._exit(code)` 交确定退出码 | 让退出码只反映断言结果，绕过解释器关闭阶段 |
| `check.sh` 用 `-X faulthandler` 跑所有测试 | 崩在 C 层（segfault 等）能看到 Python 栈 |
| `check.sh` 失败时打印 `exit=$rc` | 区分断言失败（1）与被信号杀（137 等） |

### 新增一条断言

```python
check(not alive, "close 之后没有残留的 brain_memory 线程")
```

`close()` 的承诺之一是停掉 flusher。如果它还在跑，进程退出阶段就会去碰已经关掉的连接 ——
**那正是「断言全过但退出码非 0」最可能的来源**。这条断言把猜测变成可观测的事实。

### 状态：未结案

根因还没定位。这一轮只是把「一个非 0 退出码」变成「有栈、有退出码、有线程残留检查」的可诊断状态。
CI 上再挂一次就能拿到有效信息。
## v1.3.7 —— 2026-10-29

纯文档：新增 `docs/OPERATIONS.md` 运维手册。

九轮审计挖出的 31 条问题，散在 9 个 Release 说明和 21 个测试文件里 —— **真出事的时候没人会去翻**。
这份手册按「症状」重新组织，不按模块：

| 章节 | 内容 |
|---|---|
| 0 | 三条命令先跑一遍（`check.sh`、`stats`、关键字段） |
| 1 | 症状速查表：9 种现象 → 跳到哪一节 |
| 2 | 逐个展开：先分清是哪一层，再给 SQL / Python 排查片段 |
| 3 | 定期维护节奏（每天 / 每周 / 空闲） |
| 4 | **看起来像坏了，其实正常**（去重合并、快照 quality、门控拒收……） |
| 5 | 升级与回滚（原地升级、回滚不安全、WAL 备份要 close 后拷） |
| 6 | 版本与修过的坑对照表 |
| 7 | 还是没头绪时的信息收集顺序 |

第 4 节是这份手册里最省时间的部分：**六种「用户以为坏了」的情况，其实都是设计行为**。
## v1.3.6 —— 2026-10-29

第九轮审计：`dream()` 的巩固逻辑。这是唯一一个「会自己改数据」的模块，前八轮没碰过。

### 孤儿实体页不再永留

`_persist` 只 upsert 从不清理。后果：

- 实体改名 → 旧名字的页永远留着
- 原子全被归档 → 页还在，`DREAMS.md` 里继续显示已经不存在的实体

跑几个月的库，`entity_pages` 和 `DREAMS.md` 会越积越多，而且**没有任何东西会告诉你哪些是死的**。

现在全量巩固时会删掉「本次没有生成的实体页」。两个前提缺一不可：

```python
prune = max_atoms is None and len(pages) < int(cfg.dream_max_clusters)
```

`max_atoms` 限定了扫描范围、或 `dream_max_clusters` 截断了页数时**不能清理** ——
本次没生成不代表它该消失。清理条数记进 `journal` 的 `entity_pages_pruned`。

### DREAMS.md 改成原子写

原来是 `open(path, "w")` 直接覆盖。写到一半崩掉，上一份完好的文件就变成了半截 Markdown。
现在先写 `.tmp` + `fsync` + `os.replace()`，失败时清掉临时文件。

### 新增 `tests/test_dream.py`

| 用例 | 断言 |
|---|---|
| 正常巩固 | 生成实体页 + `DREAMS.md` 落盘 |
| 原子全归档后再巩固 | 孤儿页被清成 0 |
| `persist=False` | 库一行不改 |
| 恢复后再巩固 | 页能重新生成 |
| 落盘 | 不残留 `.tmp` 文件 |
## v1.3.5 —— 2026-10-29

第八轮审计：**升级路径与向后兼容**。前七轮一次都没验过「把 v1 的老库交给 1.3.x 打开会怎样」。

### 新增 `tests/test_migration.py`

手工造一个 v1 库（`atoms` 没有 `embedding` 列、没有 `atoms_fts`、没有新索引），然后用当前代码打开，逐项检查：

| 检查 | 期望 |
|---|---|
| `embedding` 列 | `ALTER TABLE` 补上 |
| `user_version` | 1 → 2 |
| `idx_atoms_state_id` | `_ensure_indexes` 幂等补齐 |
| `atoms_fts` | 新建并 `rebuild` 索引 |
| 旧原子 | 还在，且能被 `recall` 到 |
| 迁移后写入 | 新数据正常进 L2 |

顺带钉住三件向后兼容的事：

| 场景 | 期望 |
|---|---|
| `user_version=99`（未来版本的库） | 抛 `SchemaVersionError`，不瞎改 |
| 已移除的 4 个配置项 | 抛 `ConfigError`，不静默无效 |
| `extract_llm_*` 未配置 | 回落到 `llm_*`；配了就优先用便宜模型 |
## v1.3.4 —— 2026-10-29

第七轮审计（并发与生命周期）的连带发现：**上一版加的一条冲突判据会误杀正常数据**。

### P0：冲突判据把 40 条正常记忆下线了 32 条

v1.1.0 为了修「十点下班 / 九点下班并存」的漏杀，加了这条规则：

```python
if mine_nums and other_nums and not (mine_nums & other_nums):
    stem = similarity(_NUM_RE.sub("#", content), _NUM_RE.sub("#", other))
    if stem >= 0.6:      # ← 问题在这里
        return int(row[0]), stem
```

看起来合理，实际会误杀：两条随机内容 `a1b2c3d4-alpha` 和 `e5f6a7b8-beta`，
把数字替换成 `#` 之后都退化成 `#a#b#c#d-alpha` 这种**同形骨架**，相似度轻易过 0.6 ——
于是被判成「同一件事的数字改写」，旧的被标 `superseded` 下线。

多进程并发测试把它暴露了出来：40 条随机内容，最后只剩 8 条 active、32 条 superseded。

修法：判据从「去掉数字后**相似**」收紧成「去掉数字后**完全相同**」，且骨架长度 ≥ 4。
真正的数字改写（`十点下班` / `九点下班`）去掉数字后一模一样，仍然抓得住。

### P0：用户的更正被静默吞掉

同一轮测试还带出第三个问题：**去重跑在冲突判断之前，而且不认否定极性**。

```
"数据库统一用 SQLite 做存储"     ← 旧
"数据库不用 SQLite 做存储"       ← 用户改主意了
```

两句只差一个「不」字，相似度 0.96 → `check_duplicate` 判重复 → 合并进旧原子。
而合并只更新 `confidence`/`quality`，**不动 `content`** —— 所以库里留着的还是旧说法。
用户说「改主意了」，系统当没听见。

修法：`check_duplicate` 里先比极性，极性相反的一律不做「重复」处理，交给冲突检测把旧的下线。
称呼归一（「叫老李」/「喊他老李」）极性相同，不受影响。

### 另外三处并发加固

| 问题 | 修法 |
|---|---|
| 嵌套写事务会把外层 `ROLLBACK` 掉 | 线程局部深度检测，立刻抛 `DatabaseError` |
| 同线程重复申请写锁要等满 5 秒才报超时 | 两个锁都记录写持有者，立刻抛 `LockOrderError` |
| `GlobalLock` 用不可重入的 `Lock`，DELETE 模式下嵌套读会自己等自己 | 改 `RLock` |

### 新增测试

- `tests/test_concurrency.py`：嵌套事务 / 锁重入 / 双进程共用库 / 并发 close
- `tests/test_conflict.py`：冲突判据两侧都钉住（十点/九点要抓，随机内容不能误抓）
## v1.3.3 —— 2026-10-29

第七轮审计：**并发与生命周期**。这类故障的共同点是「偶发」，出问题时日志里只有一句
`LockTimeoutError`，看不出是别人占着锁，还是自己等自己。

### 嵌套写事务会静默回滚外层

`_write_tx` 里有这么一段：

```python
if conn.in_transaction:
    conn.execute("ROLLBACK")
```

本意是清理上个错误留下的残留事务。但如果有人嵌套调用，第二次进来就把**外层正在跑的事务**回滚了，
而外层完全不知情 —— 更早的问题是它还会先卡在锁上等满 5 秒超时。

现在用线程局部深度提前拒绝：`DatabaseError: 不支持嵌套写事务`。

### 写锁重入会等满 5 秒才报错

`WritePriorityRWLock` 只在「持有读锁时申请写锁」这一种情况下报 `LockOrderError`；
同线程在写锁内再申请写锁时，它会安静地等满 `timeout`，然后抛一个看起来像「别人占着锁」的超时。

现在两种锁都记录写持有者，同线程重入立刻抛 `LockOrderError`。

### `GlobalLock` 用不可重入的 `Lock`

DELETE 模式下走 `GlobalLock`，它内部是 `threading.Lock()` —— 同线程嵌套读会**自己等自己**。
改成 `RLock`（读可重入），写重入单独检测。

### 新增 `tests/test_concurrency.py`

| 用例 | 断言 |
|---|---|
| 嵌套写事务 | 立刻 `DatabaseError`，不是 5 秒超时 |
| 两种锁的写重入 | 立刻 `LockOrderError` |
| 两个进程并发 open 同一个新库并各写 20 条 | 都不崩，合并后 ≥ 40 条 |
| 4 个线程同时 `close()` | 不抛异常，最终处于已关闭 |
| 关闭后再 `ingest` | 抛 `ClosedError` |
## v1.3.2 —— 2026-10-29

第六轮审计：**SQL 层的隐式契约** —— 索引是否覆盖实际的查询模式。

### 补一个组合索引

`forget.scan` 的查询是：

```sql
SELECT ... FROM atoms WHERE state IN ('active','silent','archived') ORDER BY id
```

已有的 `idx_atoms_state_quality(state, quality)` 能过滤 state，但给不出 `id` 顺序，
所以要额外排一次序（temp b-tree）。新增 `idx_atoms_state_id(state, id)` 让两者都走索引。

老库不需要 bump `user_version`：`_ensure_indexes()` 用 `CREATE INDEX IF NOT EXISTS` 幂等补齐。

### 试过但放弃的：查询计划回归测试

先用 `EXPLAIN QUERY PLAN` 写了个断言「关键查询走某条索引」的测试，跑了两轮 CI 后放弃：

| 数据分布 | 优化器的选择 |
|---|---|
| 400 条全是 `active` | 全表扫（选择性 100%，扫全表本来就更快） |
| 混合 state | 可能走索引，也可能不走 |

**优化器的选择依赖数据分布，而真实库的分布千变万化** —— 这种断言只能产出噪音。
改成 `tests/test_indexes.py`：只守「schema 声明的索引在库里都存在」，并给每个索引标注它服务哪条查询。
走不走由优化器按数据决定，人不去替它拍板。

### 发现但未动的：几个疑似死索引

| 索引 | 情况 |
|---|---|
| `idx_atoms_entity` | `entity_id` 只在 SELECT 列里出现，没有 WHERE / GROUP BY 用它 |
| `idx_atoms_last_access` | 同上（时间衰减在 Python 侧算） |
| `idx_journal_action` / `idx_journal_created` | journal 是 append-only，每次写都要维护，但没有查询按这两列过滤 |

删索引要迁移、而且可能影响未来运维查询，这轮只记录不动。等有真实数据量再决定。
## v1.3.1 —— 2026-10-29

第五轮审计：查**代码之间的交叉一致性**（不是文档），在 L4 审计层挖到三个洞。

### `JOURNAL_ACTIONS` 里的 `silence` 是从未产生的值

`forget.apply` 写 journal 时用的是 `d.action`，取值来自 `FORGET_ACTIONS = ("silent", "archive", "unlink")`。
而 `JOURNAL_ACTIONS` 里写的是 `"silence"` —— 声明了一个永远不会出现的动作，真实产生的 `"silent"` 反而没声明。

### 冲突不留痕

`promote` 检测到冲突时只把旧原子标 `superseded`，**不写 journal**。
`conflict` 这个动作在 `JOURNAL_ACTIONS` 里声明了，但代码里从未产生 —— L4 声称「记录所有状态变更」，实际漏了最该留痕的一类。

### LLM 降级与 WAL 降级也不留痕

- `mark_llm_degraded` 只改内存状态与日志，`gate_llm_fallback` 声明了没用
- WAL 降级发生在建表之前（那时还没有 journal 表），日志里有一行 warning，库里没有记录

现在：LLM 降级只在「从正常掉到降级」的那一次写 journal（避免熔断期内每条消息都写）；WAL 降级在建表完成后补记一笔。

### 新增 `tests/test_journal.py`

跑完整流程（ingest / 门控拒收 / 未知 role / remember / 冲突 / 遗忘 / 巩固 / 维护），
然后**反过来检查 journal 表里出现的每个 action 是否都在 `JOURNAL_ACTIONS` 里**。
这类「运行时反查声明」的测试，比静态扫描更能抓住漂移。
## v1.3.0 —— 2026-10-29

第四轮审计：查 `docs/EMBEDDING.md` 与 `docs/PLAN.md` 时查出三处真问题。

### 新增 `reindex_vectors()`

换 embedding 模型（维度或语义变了）之后必须重算所有向量，否则新旧混在一起、余弦没有可比性。
以前文档教用户直接调 `db._writer` 和 `db.vector_store.index_atom()` —— 那是私有属性，绕过锁与事务。
现在有公开方法，内部分批（每批 500 条一个事务），大库不会开超长写事务。

```python
n = db.reindex_vectors()          # 全量
db.reindex_vectors(limit=500)     # 只跑 500 条
```

### `FACT_TYPES` 补上 `convention`

门控的 `SIGNAL_WORDS` 一直有 `convention`（项目惯例）这个类别，但白名单 `FACT_TYPES` 里没有它。
后果：`remember(fact_type="convention")` 会被**静默归一成 `fact`**，而 `facts(fact_types=["convention"])`
能查到门控产出的、查不到 remember 写入的 —— 同一类型两种命运。

### `__version__` 从 `0.1.0` 对齐到当前版本

`brain_memory.__version__` 一直停在 `0.1.0`，而 `pyproject.toml` 已经是 1.2.2。
新增 `tests/test_version.py` 钉死这两处一致，以后不会再漂。

### 文档

- `EMBEDDING.md`：`from brain_memory import ValueError` 会 ImportError（那是内置异常），已改
- `EMBEDDING.md`：重建向量的示例改用公开方法
- `PLAN.md`：`user_version` 由 1 更正为 2；vec0 与 entity_pages 的描述对齐现状
## v1.2.2 —— 2026-10-29

工具链与文档，**没有代码行为变更**。

### 新增 review-gate：合并前必须清空审查意见

起因：v1.2.0 之前三轮 PR 在没读自动审查意见的情况下被合并，其中一条是 **P1**
（MCP HTTP 端点缺 `Origin` 校验，网页可借 DNS rebinding 读写本机记忆）。

- `.github/workflows/review-gate.yml`：查 `reviewThreads`，有未 resolve 的 bot 意见就红
- `.github/pull_request_template.md`：合并前检查清单
- `scripts/check_review_findings.py`：本地自查
- `main` 开启分支保护：五个必过检查 + 未 resolve 的对话无法合并 + 禁强推

实现上踩过三个坑，都写进注释了：GraphQL 的 bot `login` 不带 `[bot]` 后缀（必须认 `__typename`）、
`workflow_dispatch` 产生的 check 不计入 PR 的 required checks、分页翻不完必须 fail closed。

### 文档准确性审计（两轮）

拿代码反核文档，改了这些：

- **配置项 69 → 74**：漏数了 `extract_llm_*` 五项（主从模型分离）
- **「没有 HTTP 服务」**：对包成立，对仓库不成立（`mcp_server --http` 与 `memory_proxy` 会监听 127.0.0.1）
- **出站条件写精确**：光开 `enable_llm_gate` 不够，还要 endpoint + model，且只在置信度边界区间才调
- **`PUBLIC_API.md` 补 `remember` / `facts`**：两个核心 API 此前完全没有文档
- **escalation 契约**：补上「只在 L2 结果不足 `top_k` 时才下探」这个前提
- **「永不物理删除」**：改成「默认不改库；物理删除是显式选项，`allow_hard_delete` 默认 True」
- `PLAN.md` 标注为 v1.0 历史计划；`AGENT_RULES.md` 加「原文未改，差异以代码为准」对照表
## v1.2.1 —— 2026-10-29

修 Codex 在 PR #1 / #2 / #3 上提的 9 条意见（全部成立，逐条对应）。

### P1 · 安全

- `mcp_server.py --http`：补 **Origin 校验**。原来任何网页都能借 DNS rebinding 打到
  `127.0.0.1`，读写用户记忆。现在只放行 localhost 系来源，其余 403；无 Origin 的
  非浏览器客户端照常放行。

### P2 · 协议一致性

- HTTP `GET` 不再对所有路径回 JSON：非 `/health` 的 GET 按 Streamable HTTP 规范回 **405 + Allow: POST**。
- 接受的通知（`notifications/initialized` 等）返回 **202**，不再用 204。

### P2 · 参数被吞

- `memory_forget` 的 `limit` 真正透传给 `MemoryDB.forget`。原来 `dry_run=false` 时它被丢掉，
  一次维护调用会把全库符合条件的条目全归档。
- `memory_dream` 的 `max_atoms` 真正透传，不再固定用 100000 的默认上限。

### P2 · 门控

- `_is_question` 判定顺序：**以强疑问词收尾**（什么/怎么/多少/是不是…）现在优先于断言词。
  原来「我们统一用什么数据库」含「统一」，被判成陈述、再由「统一用」计分当 decision 收下 ——
  一句问句就这样进了长期记忆。「为什么」仍放在断言词之后，保住「不要问我为什么」是祈使。

### P2 · 代理

- 注入预算改为 **recall 结果与 facts 共用一份**，长偏好不再把请求撑爆。
- 纯图片消息（content 无文本）不再整体跳过注入：仍注入长期偏好与约束，只跳过语义检索。

### P2 · 脚本

- `scripts/run-memory-proxy.sh`：第一个位置参数（上游 URL）现在会 `shift` 掉。
  原来它同时被当作 `--upstream` 的值和多余位置参数传给 argparse，代理直接起不来。

### 测试

- `test_gate_assertive.py` 增加「我们统一用什么数据库」用例
- `test_mcp_server.py` 增加 HTTP 段：Origin 403 / GET 405 / /health 200 / 通知 202
- `test_memory_proxy.py` 增加纯图片轮次注入与小预算截断两段
## v1.2.0 —— 2026-10-29

两种接入宿主的方式，任选或并用。

### 新增 `mcp_server.py`

把记忆胶囊包成 MCP 服务器：纯 stdlib 实现 JSON-RPC 2.0 over stdio，另带 `--http` 模式。
7 个工具：`memory_ingest` / `memory_remember` / `memory_recall` / `memory_context` /
`memory_forget` / `memory_dream` / `memory_stats`。
DSH、Operit AI、Claude Desktop、Cline 用同一份实现。

### 新增 `memory_proxy.py`

OpenAI 兼容记忆代理：把宿主 baseURL 指过来即可全自动 ——
请求前注入相关记忆与长期偏好，响应后把这一轮写回 L0。
解决了「MCP 工具调不调由模型决定」的短板，宿主不需要支持 MCP。

### 新增脚本与文档

- `scripts/run-memory-mcp.sh` / `scripts/run-memory-proxy.sh`：Termux 保活启动（自带 wake-lock）
- `docs/MCP.md`：两端接入、全自动模式、保活、验证步骤
- `examples/dsh-memory-mcp.cordis.yml` / `examples/operit-memory-mcp.json`

### 测试

- `tests/test_mcp_server.py`：子进程握手、7 个工具、错误路径（-32601 / -32700 / 未知工具 / 空参数）、退出码
- `tests/test_memory_proxy.py`：假上游 + 真代理 + 真 SQLite，覆盖注入 / 回写 / `X-Memory: off` / 上游 502
- `check.sh --full` 现在覆盖 14 个脚本
## v1.1.0 —— 2026-10-27

质量与判据修复，含 3 处 config 项移除（**breaking**：删掉的键会以 ConfigError 明确报错，不再静默无效）。

### 质量单一真源

- `atoms.quality` 从此只作「最近一次重算的快照」：`recall` / `facts` 的过滤与打分一律现场调用
  `compute_quality()`，与 `forget` / `dream` 同源。
- 修前：存储值恒等于写入时的 confidence，永不衰减 —— 半年前的记忆存储 0.70、实算 0.25，
  `recall(min_quality=0.3)` 照样放行；`dream_min_quality` 却已经拒绝它，两套标准互相打脸。

### 判据

- `_call_name` 不再截断到 6 字符：「叫 getUserInfo」「叫 getUserList」原来都取到 `getUse`，
  被判同名直接合并。现在收整词（含 `_ . -`），且长度 < 2、含 `/ \ :` 一律不参与同名判定。
- `distance_to_similarity()`：vec0 的 L2 距离改为余弦等价 `1 - d²/2`。
  原来 `1/(1+d)` 让最不像的一对（d=2）还有 0.33，配 `vec_min_similarity=0.10` 等于不过滤。

### 规模

- 纯 Python / numpy 暴力检索改为**分块扫描**（chunk=1000，新记忆优先）。
  `pure_fallback_max_atoms` 的语义从「只看最老这么多条」改为「单次最多扫这么多条」，
  5000 条以上不再静默丢失新记忆。
- `enable_numpy_fallback` 真正接线（原来改了没反应）。

### 移除的无效配置

`commit_batch`、`raise_on_llm_error`、`max_candidates_per_ingest`、`numpy_fallback_max_atoms` ——
四个在代码里零引用，改了不起作用。现在传进来会明确抛 `ConfigError: 未知配置项`。

### 版本

`pyproject.toml` 的 version 同步为 `1.1.0`（此前停留在 `0.1.0.dev1`，与 CHANGELOG 不一致）。

## v1.0.0 —— 2026-10-27


首个公开版本。

### 分层结构

- **L0 原始事件**：append-only 落盘，任何记忆都能溯源到原话
- **L1 候选**：门控过滤后的待提炼条目（rejected / promoted / merged 三态）
- **L2 原子**：真正的记忆单元，FTS5 全文索引 + 可选向量
- **L3 实体页**：`dream()` 巩固后生成的聚类页，写进 DREAMS.md
- **L4 日志**：所有状态变更的审计轨迹

### 质量模型

```
quality = clamp01(feedback × decay × connectivity)
```

- `feedback`：used(1.0) / useful(0.8) / noise(0.0)
- `decay`：三种半衰期 —— 情节 7 天、语义 90 天、程序 365 天
- `connectivity`：被召回次数越多越稳

### 检索

- FTS5 关键词检索 + sqlite-vec 向量检索
- RRF 融合：`score = Σ 1/(k+rank)`，k=60
- 升级下探：L2 空或最高分低于 0.35 时才往 L1→L0 查（默认关）

### 三条降级路径

| 能力 | 优先级链 |
|---|---|
| journal_mode | WAL → DELETE |
| 向量 | sqlite-vec → numpy → pure（纯 Python 点积）→ none |
| LLM | 云端 → 规则兜底 + 60 秒熔断 |

### Android 适配

- `mmap_size=0`（PRoot 下 mmap 会出问题）
- `busy_timeout=5000`、`cache_size=-2000`、`temp_store=MEMORY`
- 实测跑在 Android 15 / aarch64 / PRoot 容器里

### 这一版修掉的问题

发布前在真实项目里跑了几天，修了这几处：

**1. 事实排序取「最新」而不是「最重要」**

`db.facts()` 原来是 `ORDER BY quality DESC, id DESC` —— 同质量时取最新的。结果最近积累的口水话（「爱玩梗」「复读机」这类）把早期的核心事实（名字、爱好、约定）全挤出了 12 条注入预算，AI 表现为「把你忘了」。

改成 `id ASC`：同质量时取最早的。早期建立的是身份信息，越往后越是废话。

**2. 去重阈值降不了，但可以加规则**

实测数据：

```
0.571  叫老李  <>  喊他老李        ← 该合并，但 0.9 阈值下永远合不了
0.857  十点下班 <>  九点下班        ← 不该合并，但降阈值就会误伤
```

纯字符串相似度区分不了「同义」和「矛盾」。解法是加两条规则：

- **称呼归一**：`(叫|喊|称呼|昵称|名字是|姓名|外号) + 名字`，同名直接合并
- **数字冲突**：两条都带数字且完全不相交 → 绝不合并（且只认带单位的数字，避免「一般」里的「一」被当成数字）

**3. 疑问句被当成事实入库**

用户问「我喜欢玩什么游戏」，门控把它收进 L2 当偏好。下一轮注入 prompt，模型会把它当已知事实复述。

加疑问句检测：句尾含 `吗/呢/什么/怎么/哪/几点/多少` 等，直接拒收。判定用句尾特征而不是全文匹配，避免误伤「别问那么多」这种陈述。

### 已知限制

- L3 实体页只是聚类索引，不做推理
- 不支持多模态记忆（图/音），那是上层的事
- 单机 SQLite，不支持分布式
