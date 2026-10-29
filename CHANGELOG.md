# 更新记录

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
