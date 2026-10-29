# 更新记录

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
