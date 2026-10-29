# 记忆胶囊

[![test](https://github.com/lqx3617-dot/memory-capsule/actions/workflows/test.yml/badge.svg)](https://github.com/lqx3617-dot/memory-capsule/actions/workflows/test.yml)
[![review-gate](https://github.com/lqx3617-dot/memory-capsule/actions/workflows/review-gate.yml/badge.svg)](https://github.com/lqx3617-dot/memory-capsule/actions/workflows/review-gate.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> 中文名 **记忆胶囊** · 包名 `brain_memory` · 当前版本 `1.2.1`

给 AI 一套**会遗忘、会巩固、会做梦**的长期记忆。

纯 stdlib + sqlite3，没有 Redis、没有模型权重、不需要 root。
**库本身不开端口、不主动联网** —— `import brain_memory` 不会监听任何端口。
专为 Android PRoot（Termux / proot-distro，aarch64 用户态）设计：所有降级路径自动兜底，
没有 numpy 也能跑，没有 sqlite-vec 也能跑，LLM 挂了照样跑。

---

## 它解决什么问题

聊天模型的上下文只有几千到几万 token，聊过的事一刷新就没了。
把历史一股脑塞进去会爆，塞一部分又常常塞错 —— 用户三个月前说过的「别给我发语音」，
和你昨天说的「今天天气不错」，在关键词检索眼里一样重要。

记忆胶囊做的不是「存更多」，而是**分层 + 判断值不值得记 + 按重要性衰减**：

| 问题 | 它的做法 |
|---|---|
| 原话要不要留 | 全部留（L0），永不删，任何一条记忆都能溯源到当初那句话 |
| 哪些值得长期记 | 门控判断：偏好 / 决定 / 约束 / 踩坑 / 待办留下，寒暄和问句丢掉 |
| 怎么找得到 | FTS5 关键词 + 向量语义，两路 RRF 融合，不靠单边 |
| 旧信息会污染 | 质量随时间衰减（情节 7 天、语义 90 天、程序 365 天半衰期） |
| 忘了怎么办 | 冲突只加边不删行，遗忘默认只改状态；物理删除必须显式 hard + confirm |
| 记忆能不能自己整理 | `dream()` 把高质量原子聚成实体页，写进 `DREAMS.md` |

---

## 四种用法，按宿主能力选

| 场景 | 用哪个 | 一句话 |
|---|---|---|
| 自己在 Python 里写 | `import brain_memory` | 直接调 API，最灵活 |
| 宿主支持 MCP | `mcp_server.py` | 7 个工具，模型按需调用 |
| 宿主只认 baseURL | `memory_proxy.py` | 全自动注入 + 回写，不依赖模型自觉 |
| Android 常驻 | `scripts/*.sh` | wake-lock + 后台启动 |

前三种可以并用，共用一个 `memory.db`（SQLite WAL 支持多进程读写）。

**谁开端口、谁联网：**

| 用法 | 监听端口 | 出站网络 |
|---|---|---|
| `import brain_memory`（库） | 不开 | 不开（除非你配了 `enable_llm_gate` 或 `embedding_backend=http`） |
| `mcp_server.py`（stdio，默认） | 不开 | 默认不开（例外见下方注） |
| `mcp_server.py --http 8788` | 开 `127.0.0.1:8788` | 默认不开（例外见下方注） |
| `memory_proxy.py` | 开 `127.0.0.1:8790` | **会**（要转发到上游 LLM） |

> **注**：上面「出站网络」一列说的是**默认配置**。要真的产生出站，光把开关打开不够 ——
>
> - **门控调 LLM**：要 `enable_llm_gate=True` **且** 有可用的 endpoint + model —— 优先取 `extract_llm_endpoint` + `extract_llm_model`（主从分离，省额度），没配就回落到 `llm_endpoint` + `llm_model`；
>   而且只在置信度落在边界区间（0.42 ≤ conf < 0.78）时才真的发请求，高置信度和低置信度都走纯规则。
> - **远程 embedding**：要 `embedding_backend="http"` **且** 配了 `embedding_endpoint` + `embedding_model`，
>   缺任何一个 `resolve_embedder()` 都返回 None —— 结果是**完全不做向量**（`vector_backend=none`），退回纯 FTS5，而不是换成本地哈希。都不出网。
>
> 这两条链默认都是关的：`enable_llm_gate=False`、`embedding_backend="hash"`。

想要绝对零端口：只用库，或者 MCP 走 stdio 模式（宿主拉子进程，不走网络）。
两个脚本的监听地址默认都是 `127.0.0.1`，不对外；`--host` 可以改，但别改成 `0.0.0.0`。

### 用法一：当库用

```python
import brain_memory as bm

db = bm.MemoryDB("/data/data/<pkg>/files/memory.db")

db.ingest("chat-1", "user", "我们决定数据库统一用 WAL 模式，别的都不用")
db.ingest("chat-1", "user", "今天天气不错")        # 门控拒收，但 L0 仍落盘

for m in db.recall("数据库选型", top_k=5):
    print(m.score, m.content, m.source_event_id)

print(db.forget(dry_run=True))   # 只算不写
print(db.dream())                # 巩固：生成 L3 实体页 + DREAMS.md
db.close()
```

```bash
pip install -e .            # 基础：纯 stdlib + 纯 Python 向量
pip install -e ".[numpy]"   # 可选：numpy 矩阵点积
pip install -e ".[vec]"     # 可选：sqlite-vec 加速索引
```

### 用法二：当 MCP 插件用（DSH / Operit / Claude Desktop）

```bash
python3 mcp_server.py --db ~/.brain-memory/memory.db              # stdio
python3 mcp_server.py --db ~/.brain-memory/memory.db --http 8788  # Streamable HTTP
```

暴露 7 个工具：

| 工具 | 用途 |
|---|---|
| `memory_ingest` | 写一句原始对话，过门控 |
| `memory_remember` | 确认过的事实，**跳过门控** |
| `memory_recall` | FTS5 + 向量 RRF 检索 |
| `memory_context` | 一次列出偏好 / 约束，拼 system prompt |
| `memory_forget` | 遗忘扫描，默认 dry-run |
| `memory_dream` | 巩固成 L3 实体页 |
| `memory_stats` | 各层计数与降级状态 |

DSH 接法（改路径即可）：

```bash
dsh web --patch "$PWD/examples/dsh-memory-mcp.cordis.yml"
```

挂上后工具名形如 `mcp__brain-memory__memory_recall`。详细说明见 [`docs/MCP.md`](docs/MCP.md)。

### 用法三：当 OpenAI 兼容代理用（全自动）

MCP 的短板是「要不要调工具，由模型决定」。模型偷懒，这轮对话就白聊。代理把决定权拿走：

```bash
python3 memory_proxy.py --db ~/.brain-memory/memory.db \
    --upstream https://api.deepseek.com/v1 --port 8790

# 宿主 baseURL 填 http://127.0.0.1:8790/v1
```

| 时机 | 动作 |
|---|---|
| 请求前 | 最后一条 user 消息去 recall，把【相关记忆】+【长期偏好与约束】拼进 system |
| 响应后 | 这一轮的 user / assistant 原文写回 L0 |

| 参数 | 作用 |
|---|---|
| `--top-k 5` | 每轮注入几条记忆 |
| `--budget-tokens 800` | 注入预算（记忆与偏好共用，含固定提示语） |
| `--no-facts` | 不注入长期偏好，省 token |
| `--no-write-back` | 只注入不记录（隐私模式） |
| 请求头 `X-Memory: off` | 单次跳过 |

Authorization 原样透传，代理不存不管密钥；上游挂了返回 502，进程不崩。

### 用法四：Android 保活一键起

```bash
sh scripts/run-memory-mcp.sh                                # MCP HTTP :8788
sh scripts/run-memory-proxy.sh https://api.deepseek.com/v1   # 代理 :8790
```

脚本自带 `termux-wake-lock`。但**电池优化还得手动关**（电池 → 应用启动管理），
否则息屏几分钟照样被杀。国产 ROM 这一步尤其必须做。

---
## 五层结构

```
L0 raw_events   原话，append-only，永不删 —— 任何记忆都能溯源到这里
      ↓ 门控（值不值得记）
L1 candidates   候选事实，pending / promoted / rejected / merged
      ↓ 晋升五项检查（价值 / 证据 / 实体 / 去重 / 冲突）
L2 atoms        真正的记忆单元，FTS5 索引 + 可选向量
      ↓ dream() 巩固
L3 entity_pages 实体摘要页 + DREAMS.md
L4 journal      所有状态变更的审计轨迹，append-only
```

### 门控：判断值不值得记

规则优先，LLM 只在边界区间加分（可关、可熔断）：

1. **硬拒**：太短、纯标点、纯链接、代码块、日志行、JSON、寒暄、复读、疑问句
2. **计分**：偏好 / 决策 / 约束 / 惯例 / Bug 根因 / 待办，命中越狠分越高
3. **边界区间**才问 LLM；LLM 失败立即回退规则并熔断 60 秒

几条踩过坑的规则：

- **只记用户说的话**：assistant 复述「别给我发语音」不能当用户偏好存下来
- **疑问句不是事实**：问「我喜欢玩什么游戏」不代表这是偏好，存下去下一轮模型会当已知事实复述
- **以强疑问词收尾优先于断言词**：否则「我们统一用什么数据库」会因为含「统一」被当成决策收下

### 晋升：L1 → L2 的五项检查

| 检查 | 判据 |
|---|---|
| 价值 | 长度、confidence、不超过 `max_atom_chars` |
| 证据 | `raw_event_id` 必须能在 L0 找到 |
| 实体 | 引号 > 英文标识符 > 「X 项目/模块/库」，挖出实体名 |
| 去重 | 相似度 ≥ 0.9 合并；**称呼归一**（叫老李 = 喊他老李）；**数字不相交则不合并**（十点 ≠ 九点） |
| 冲突 | 否定极性相反且相似度高 → 旧原子标 `superseded`，新原子记 `supersedes_id`，**不删** |

去重命中 `silent` 原子时会**复活**它（state + feedback + last_accessed_at 一起改）——
用户重新说一遍，说明这条记忆还活着。

## 检索：FTS5 + 向量 + RRF

```
FTS5 召回 top-N  ─┐
                  ├─> RRF(score = Σ 1/(k + rank))  ─> quality 过滤 ─> token 预算截断
向量召回 top-N  ─┘
```

只看名次不看分数量纲，两路第一名权重相同 —— 不会因为 bm25 是负数、余弦是 0~1 被单边压制。
`rrf_k` 默认 60。

### 向量后端，逐级降级

| backend | 条件 | 检索方式 |
|---|---|---|
| `sqlite-vec` | 扩展加载 + vec0 建表成功 | vec0 KNN |
| `numpy` | 装了 numpy | 矩阵点积 |
| `pure` | 纯 stdlib | 分块逐条余弦（**默认**） |
| `none` | `enable_vector=False` 或全挂 | 退化纯 FTS5，静默 |

向量存在 `atoms.embedding`（小端 float32 BLOB），vec0 只是**可选加速索引** ——
换后端不用重建数据，扩展挂了也不丢向量。

### Embedding 三选一

```python
# 1) 默认：纯 Python 哈希向量（零依赖零网络，跨进程稳定）
db = bm.MemoryDB(path)

# 2) 自备函数（任意模型，同步调用即可）
db = bm.MemoryDB(path, {"embedding_callable": my_embed})

# 3) OpenAI 兼容 /embeddings 端点
db = bm.MemoryDB(path, {
    "embedding_backend": "http",
    "embedding_endpoint": "http://127.0.0.1:8788/v1",
    "embedding_model": "text-embedding-3-small",
})
```

哈希向量是**词形重叠**的高维投影，对换序、跨标点、部分字面重叠比 FTS5 宽容；
「SQLite ↔ 本地数据库」这种真同义要靠真模型。

### 两道安全阀

| 配置 | 默认 | 作用 |
|---|---|---|
| `vec_min_similarity` | 0.10 | 相似度低于此值的向量结果直接丢，避免噪声灌进 RRF |
| `vec_fail_limit` | 3 | embedding 连续失败 3 次 → 停用向量后端，退回 FTS5 |

第二道很关键：接了远程 embedding 后网络断了，没有熔断就会**每条消息都等一次超时**。

### 低置信度下探（默认关）

L2 无结果或最高分低于 `escalate_threshold`（0.35）时，往 L1 候选、再往 L0 原话找。
L0 只看最近 `escalate_l0_limit`（50）条，**绝不全表扫**。

返回结果用 `layer` 标层级，`record_id` 指当前层主键（L2=atom_id、L1=candidate.id、L0=raw_events.id）。

## 遗忘与巩固

### quality 怎么算

```
quality = clamp01( feedback × decay × connectivity )
  feedback      used(1.0) / useful(0.8) / noise(0.0)
  decay         0.5 ** (age_days / halflife)
                情节 7 天 / 语义 90 天 / 程序 365 天
  connectivity  1 + log2(1 + refs + 被召回次数)
```

**存储的 `atoms.quality` 只是快照，读的时候一律现场重算。**
这一点很重要：写入时它等于 confidence，之后不会自己衰减；如果拿它做过滤，
半年前该忘的记忆照样会被返回。`recall` / `facts` / `forget` / `dream` 现在用同一个函数。

唯一例外是 `stats().avg_quality` —— 它统计的是**快照平均值**，不是现算值。

### 判定

| 条件 | 动作 | 说明 |
|---|---|---|
| quality 低且曝光少 | `silent` | 不返回，不删除 |
| quality 低且曝光多 | `archive` | 不可检索，可恢复 |
| 其他 | `keep` | 保留 |

`forget()` 默认 `dry_run=True`，只报告不写库。

**物理删除是显式选项，不是默认行为**：只有 `hard=True` + `confirm=True`，且 `allow_hard_delete=True`（注意它默认就是 True），
才会从 `atoms` 删行；`raw_events` 始终一行不动，溯源链不断。生产环境建议显式设 `allow_hard_delete=False`，把这条路彻底关掉。

### dream()：巩固

1. 扫 `quality >= dream_min_quality`（0.6）的活跃原子（现场重算）
2. 按 entity 分组，没有 entity 的按 fact_type 兜底
3. 组内按相似度聚类（`dream_cluster_similarity` = 0.55）
4. 每个簇生成 Markdown，写 `entity_pages`
5. 汇总写 `DREAMS.md`（DB 同目录，可用 `dreams_path` 覆盖）

**不做的事**：不改模型权重、不决定删除、不在 recall 热路径执行。

## API

| API | 签名 | 返回 |
|---|---|---|
| 构造 | `MemoryDB(path, config=None)` | 实例 |
| 写入 | `ingest(session_id, role, content, *, meta=None, created_at=None, promote=True)` | `raw_events.id` |
| 确认写入 | `remember(session_id, content, *, fact_type="fact", confidence=0.8)` | `raw_events.id`（**跳过门控**） |
| 检索 | `recall(query, top_k=5, budget_tokens=2000, *, min_quality=None, fact_types=None, session_id=None, layer="L2", escalate=None)` | `list[Memory]` |
| 列举 | `facts(*, session_id=None, fact_types=None, min_quality=0.0, limit=50)` | `list[Memory]`（不做语义检索） |
| 遗忘 | `forget(dry_run=True, *, hard=False, confirm=False, limit=None, older_than_days=None)` | `list[dict]` |
| 巩固 | `dream(*, persist=True, max_atoms=None)` | `str`（Markdown） |
| 反馈 | `feedback(atom_ids, signal)` | 改动行数；signal = used / useful / noise |
| 运维 | `stats()` / `maintenance(level)` | `Stats` / dict |
| 关闭 | `close(timeout=5.0)` | `None` |

`ingest` 和 `remember` 的区别：前者让门控判断值不值得记，后者是调用方已经确认过的事实
（比如模型压缩出来的摘要条目「在杭州」「有只猫叫豆豆」），不该被「太短」挡掉。

`facts()` 按类型直接列，用来把「这个用户的所有偏好与约束」整批注入 system prompt ——
这类需求不能靠关键词检索：用户问「今天心情不好」时，「别给我发语音」不会因为字面相似被召回。

兼容别名：`Database` = `MemoryDB`，`Config` = `MemoryConfig`。dict 配置请走 `Config.resolve(dict)`。

---

## 配置

`MemoryConfig` 共 **74 项**，优先级：显式 config > 环境变量 `BRAIN_MEMORY_*` > 默认值。

```python
db = bm.MemoryDB(path, {
    "silent_quality": 0.15,        # 低于此值且曝光少 -> silent
    "archive_quality": 0.25,       # 低于此值且曝光多 -> archive
    "enable_escalation": True,     # 开 L2->L1->L0 下探
    "dedup_similarity": 0.9,       # 去重阈值
    "vec_dim": 384,
})
```

最常调的几组：

| 组 | 关键项 | 默认 |
|---|---|---|
| 存储 | `journal_mode_target` / `busy_timeout_ms` / `mmap_size` | WAL / 5000 / 0（PRoot 下必须 0） |
| 门控 | `gate_threshold` / `gate_min_chars` / `enable_llm_gate` | 0.5 / 4 / False |
| 晋升 | `promote_min_confidence` / `dedup_similarity` / `max_atom_chars` | 0.55 / 0.9 / 2000 |
| 检索 | `w_bm25` / `w_quality` / `w_recency` / `top_k_max` | 0.55 / 0.35 / 0.10 / 100 |
| 向量 | `enable_vector` / `vec_min_similarity` / `pure_fallback_max_atoms` | True / 0.10 / 5000 |
| 遗忘 | `halflife_*_d` / `silent_quality` / `archive_quality` | 7/90/365 天 / 0.15 / 0.25 |
| 巩固 | `dream_min_quality` / `dream_cluster_similarity` / `dream_max_clusters` | 0.6 / 0.55 / 20 |
| 主从模型 | `extract_llm_enabled` / `extract_llm_endpoint` / `extract_llm_model` | True / 空 / 空（留空则回落到 `llm_*`） |

**v1.1.0 起移除了 4 个零引用配置**（`commit_batch` / `raise_on_llm_error` /
`max_candidates_per_ingest` / `numpy_fallback_max_atoms`）：传进来会明确抛 `ConfigError`，不再静默无效。

---

## 命令行

```bash
python -m brain_memory --db mem.db ingest "记得明天要改配置"
python -m brain_memory --db mem.db recall "数据库选型" --top-k 3 --json
python -m brain_memory --db mem.db forget                  # dry-run
python -m brain_memory --db mem.db forget --apply          # 落 silent/archive
python -m brain_memory --db mem.db stats
python -m brain_memory --db mem.db dream
python -m brain_memory --db mem.db maintain --level full
python -m brain_memory --db mem.db --config journal_mode_target=DELETE stats
```

`ingest` 既收位置参数也收 `--content/-c`；`-c -` 从 stdin 读；`--session` 默认 `cli`。

## 测试与 CI

```bash
sh check.sh          # 静态扫描 + 文档代码块 + 语法编译 + 真建库体检
sh check.sh --full   # 再加 17 个测试脚本 + examples/embed_demo.py
```

| 脚本 | 钉死的问题 |
|---|---|
| `test_smoke` | PRAGMA / 并发 / 锁 / close 时序 |
| `test_t4` | 门控 / 晋升 / 检索 / 遗忘 / 护栏 |
| `test_phase2` | 向量后端 / RRF / 迁移 / 真 dream |
| `test_escalation` | L2→L1→L0 下探 |
| `test_gate_assertive` | 陈述句不许被误判成疑问（三层断言） |
| `test_revive` | silent 复活（state + recall 两个面） |
| `test_connectivity` | 曝光次数真的影响 quality |
| `test_vec_window` | 大库不丢新记忆（cap=3 + 9 条原子） |
| `test_quality_live` | recall / facts 用现算 quality，不是快照 |
| `test_call_name` | 称呼归一不误合并（getUserInfo ≠ getUserList） |
| `test_vec_metric` | vec0 距离与余弦同量纲 |
| `test_mcp_server` | MCP 握手 / 7 工具 / Origin 403 / GET 405 / 通知 202 |
| `test_memory_proxy` | 注入 / 回写 / X-Memory: off / 上游 502 / 预算截断 |
| `test_version` | `pyproject.toml` 与 `__version__` 不许漂移 |
| `test_reindex` | `reindex_vectors()` 可用、`convention` 能存能查 |
| `test_journal` | journal 里出现的 action 必须都已声明（审计完整性） |
| `test_query_plan` | 关键查询不许退化成全表扫（EXPLAIN QUERY PLAN 实跑） |
| `embed_demo` | 全流程演示 |

CI 在 Python 3.10 / 3.11 / 3.12 / 3.13 四档跑全套。

### review-gate：合并前必须清空审查意见

仓库装了自动代码审查 bot。`.github/workflows/review-gate.yml` 会查 PR 上所有
**未 resolve 的 bot 意见**，有就红；`main` 已开启分支保护，未 resolve 的对话无法合并。

```bash
# 本地自查（不用等 CI）
GITHUB_TOKEN=ghp_xxx python3 scripts/check_review_findings.py 5
```

| 情况 | 结果 |
|---|---|
| 有未 resolve 的 bot 意见 | 红 |
| 都 resolve 了 | 绿（在 thread 里回一句或 Re-run jobs 触发） |
| thread 超过 2000 条翻不完 | 红（**fail closed**，让人来看） |
| 打了 `review-gate:skip` 标签 | 绿 + summary 留痕 |

---

## 目录结构

```
brain_memory/
├── db.py          MemoryDB：连接 / PRAGMA / 锁 / 连接池 / flusher / 业务 API（1231 行）
├── recall.py      FTS5 + 向量 + RRF + 现算 quality + 下探
├── promote.py     晋升五项检查：价值 / 证据 / 实体 / 去重 / 冲突 / 复活
├── gate.py        门控：规则 + 可选 LLM + 熔断
├── forget.py      quality 打分与三态判定
├── dream.py       巩固：L3 实体页 + DREAMS.md
├── vec.py         Embedder 接口 / 三级后端 / 距离换算
├── locks.py       写优先读写锁 + 全局锁
├── config.py      MemoryConfig（74 项）
├── types.py       Memory / Stats / ForgetDecision + 枚举
├── cli.py         python -m brain_memory
├── errors.py      异常层级（9 类）
└── schema.sql     L0-L4 全部 DDL（v2）

mcp_server.py      MCP 服务器（7 工具，stdio + HTTP）
memory_proxy.py    OpenAI 兼容记忆代理（自动注入 + 回写）
scripts/           Termux 保活脚本 + 审查自查
docs/              MCP / PLAN / PUBLIC_API / EMBEDDING / AGENT_RULES
tests/             17 个测试脚本
examples/          DSH 与 Operit 的接入配置示例
```

---

## 不变量

1. `raw_events` 只增不改不删
2. `atoms` 除 `state` / `quality` / `access_count` / `last_accessed_at` / `feedback` / `embedding` 外不可变
3. 冲突只产生 `supersedes_id` 边，旧原子标 `superseded`，不删
4. `forget` 默认 `dry_run=True` 不写库；只有 `hard=True` + `confirm=True` 且 `allow_hard_delete=True` 才会删 `atoms` 行 —— `raw_events` 永远不动
5. 降级不抛异常（只有 `require_wal` / `require_vector` 才抛）
6. `atoms` / `atoms_fts` / 向量 同事务写入
7. `journal` 只增不改不删

---

## 降级矩阵

| 降级 | 判定 | 行为 | 可观测 |
|---|---|---|---|
| WAL → DELETE | `PRAGMA journal_mode=WAL` 读回 ≠ wal | 并发转全局互斥 | `stats().journal_mode` |
| sqlite-vec → numpy → pure → none | 逐级 try/except | 静默降级，最短路径仍是 FTS5 | `stats().vector_backend` |
| LLM 门控 → 规则 | 未配置 / 超时 / 非 2xx / JSON 失败 | 规则门控 + 60s 熔断 | `stats().llm_mode` |
| trigram → unicode61 | 建 FTS 表抛 no such tokenizer | 查询侧 bigram 展开 | `stats().fts_tokenizer` |
| 向量 embedding 连续失败 | `vec_fail_limit`（3） | 停用向量后端 | `stats().vector_backend = none` |
| escalation 查询异常 | L1/L0 查询抛错 | 只返回 L2 结果 | 无 |

---

## 性能（本机 Android，非 PRoot）

| 操作 | 实测 |
|---|---|
| ingest 单条（含门控 + 晋升 + 向量） | < 3 ms |
| recall（FTS5 + 向量 + RRF，4 条库） | < 10 ms |
| recall（纯 FTS5，100 条库） | < 5 ms |
| 2 线程 × 50 次 ingest + recall | 115 ms |
| dream（4 条原子） | < 15 ms |
| 建库（首次，含 FTS + 向量探测） | ~15 ms |

PRoot 下按 1.5–3 倍 I/O 放大估算，仍远低于 ingest < 50ms / recall < 100ms 的预算。

---

## 已知边界

- **纯 Python 向量是 O(n)**：分块扫描只解决「丢新记忆」，不解决「慢」。超过万条建议装 `[numpy]` 或 `[vec]`。
- **`sqlite-vec` 路径没有实测环境**：逻辑按官方 API 写，降级链已实测；装得上的环境请跑一次 `check.sh --full` 复验。
- **哈希向量不是语义模型**：真同义（SQLite ↔ 本地数据库）需要 `embedding_callable` 或 http 端点。
- **L3 实体页只是聚类索引**，不做推理。
- **`entity_pages` 只 upsert 不清理**：实体消失后旧页会留在 `DREAMS.md` 里（已知，待修）。
- **Android 后台保活靠系统配合**：wake-lock + 关电池优化能顶住大部分 ROM，但没有更可靠的纯用户态办法。
- **代理在请求路径上会看到明文对话**：不想记录就 `--no-write-back`。

---

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/MCP.md`](docs/MCP.md) | 接成 MCP 插件：DSH / Operit AI / Claude Desktop，含全自动代理模式与保活 |
| [`docs/PUBLIC_API.md`](docs/PUBLIC_API.md) | API 契约、异常、降级总表、不变量 |
| [`docs/PLAN.md`](docs/PLAN.md) | 实施计划、schema、并发模型、验收映射 |
| [`docs/EMBEDDING.md`](docs/EMBEDDING.md) | App 集成最佳实践 |
| [`docs/AGENT_RULES.md`](docs/AGENT_RULES.md) | 项目硬约束（原始规则） |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 贡献指南、设计红线、合并纪律 |
| [`CHANGELOG.md`](CHANGELOG.md) | 版本记录 |

---

## 版本

| 版本 | 内容 |
|---|---|
| `1.2.1` | 修 18 条审查意见：Origin 校验（P1）、协议状态码、参数透传、门控判定顺序、注入预算 |
| `1.2.0` | MCP 服务器 + OpenAI 兼容记忆代理 + Termux 保活脚本 |
| `1.1.0` | quality 单一真源、称呼误合并、vec 距离量纲、暴力检索分块、清 4 个死配置 |
| `1.0.0` | 首发：L0-L3 全链路 + 混合检索 + 真巩固 |

MIT License

