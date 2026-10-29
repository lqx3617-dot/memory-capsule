# EMBEDDING.md —— 在 App 里怎么用记忆胶囊

给业务代码看的。只讲结论和坑，不讲原理。

---

## 一、一句话

**一个进程一个实例，启动时建、退出时关；不要每条消息开关库，不要 fork 子进程调 CLI。**

---

## 二、生命周期

| 时机 | 动作 | 理由 |
|---|---|---|
| App 启动 / Service 创建 | `MemoryDB(path)` 建一次 | 构造即建库，重复构造幂等 |
| 收到消息 | `db.ingest(...)` | 门控与晋升都在内部完成 |
| 需要上下文 | `db.recall(...)` | 零写，可高频调用 |
| 进程退出 / Service 销毁 | `db.close()` | flush 访问计数 + checkpoint |
| 充电 + 空闲 | `db.maintenance("normal")` | 合并 WAL、优化索引 |
| 每周一次（充电 + 深夜） | `db.maintenance("full")` | VACUUM 回收空间 |

**不要**为每条消息 new 一个 MemoryDB —— 每次构造都要开 3 个连接、建表检查、启 flusher 线程。

**不要**在不 close 的情况下换路径重开 —— 旧实例的 flusher 线程还在跑。

---

## 三、线程模型

```
主线程      → 只读 stats()，不要做 ingest
IO 线程池   → ingest / recall（都可以并发）
flusher     → 库内部线程，业务不用管
```

- 实例**线程安全**：写串行、读并行（WAL）；DELETE 模式下全局互斥，仍然安全。
- 单次 ingest 会拿写锁，但只拿几毫秒。**别在写锁里做网络请求** —— 门控的 LLM 调用已经在事务外了。
- 不要自己再套一把大锁 —— 库内部已经管了。

---

## 四、ingest 怎么调

```python
db.ingest(
    session_id="user-12345",      # 会话标识，别用空串
    role="user",                  # user / assistant / system / tool
    content=text,                 # 原文，不要提前截断或改写
    meta={"channel": "dm"},       # 可选，存 meta_json
)
```

要点：

1. **原文直接扔进去**，别自己做清洗。门控认的是原文里的信号词，你提前删了标点或截断了，它就不认了。
2. `promote=True`（默认）会顺带晋升 L2，一条消息走完 L0→L2，够用。
3. 批量灌历史数据时用 `promote=False`，先囤 L1，再单独跑晋升，避免每条都做去重比对。
4. 返回的是 `event_id`，**门控拒收也返回** —— 这说明原始事件进库了，只是没被提取成记忆。别拿它判断"成功/失败"。

---

## 五、recall 怎么调

```python
hits = db.recall(
    query="用户之前说的数据库配置",
    top_k=5,              # 要几条
    budget_tokens=800,    # 拼进提示词的预算，别超过模型上下文的 1/4
)
for m in hits:
    prompt += "- " + m.content + "\n"
```

| 参数 | 建议 |
|---|---|
| `top_k` | 3–8。给 LLM 拼接时 5 足够，多了稀释注意力 |
| `budget_tokens` | 按模型上下文算：2000 上下文给 400–600，32k 给 2000+ |
| `min_quality` | 默认 0；想让结果更"干净"设 0.3 |
| `fact_types` | 只要约束类就传 `["constraint"]`，能砍掉大半噪声 |
| `session_id` | 只查某个会话的历史时传 |

**recall 不写盘**，只在内存累加访问计数。所以可以放心在每条消息前调一次。

**质量反馈**：用户对某条记忆表示"对/就是这个"时，调 `db.feedback(m.atom_id, "useful")`；明确说"记错了"调 `"noise"`。这直接影响它会不会被遗忘。

---

### 注入偏好别用 recall，用 facts()

用户问「今天心情不好」时，「别给我发语音」这几个字对不上任何关键词，语义检索召回不到 ——
但它必须每轮都注入。这类需求要**按类型列**，不要按语义搜：

```python
prefs = db.facts(
    session_id="dm:u1001",
    fact_types=("preference", "constraint", "convention"),
    min_quality=0.3,
    limit=10,
)
block = "\n".join("- " + m.content for m in prefs)
system = persona + "\n\n以下是该用户的历史偏好，必须遵守：\n" + block
```

recall 留给「上次聊到哪了」这类语义查询，两者互补。

---

## 六、forget 怎么用

```python
# 每天夜里跑一次，只看不动
preview = db.forget(dry_run=True, limit=200)
log("待静默 %d 条" % len(preview))

# 每周跑一次，真的落状态（仍然不删数据）
db.forget(dry_run=False, limit=200)
```

**不要自动调 `hard=True`。** 那条路是给人工排障用的，删了 atoms 行就查不到溯源了。

默认阈值：quality < 0.15 且曝光 < 3 → 静默；quality < 0.25 且曝光 ≥ 3 → 归档。
想更温和就调 `silent_quality=0.10` / `archive_quality=0.18`。

---

## 七、maintenance 的时机

| level | 做什么 | 建议频率 | 耗时 |
|---|---|---|---|
| `light` | PRAGMA optimize | 每天 | < 10ms |
| `normal` | + wal_checkpoint(TRUNCATE) | 每天 | 10–50ms |
| `full` | + VACUUM | 每周 | 数百 ms 到数秒 |

Android 上接到充电 + 灭屏事件里：

```python
if charging and screen_off:
    db.maintenance("normal")
    if is_weekly:
        db.maintenance("full")
```

**绝不要**在消息处理的同一条路径上调 maintenance —— VACUUM 会占着写锁几秒。

---

## 八、错误处理

```python
from brain_memory import ClosedError, DatabaseError, LockTimeoutError, ValueError

try:
    db.ingest(sid, "user", text)
except ClosedError:
    pass          # 已关闭，说明 Service 生命周期管错了
except LockTimeoutError:
    pass          # 5s 没拿到锁，通常是别的线程在跑 maintenance("full")
                  # 降级：先把消息写本地队列，稍后重试
except DatabaseError:
    pass          # 磁盘满 / 库损坏，需要上报
except ValueError:
    pass          # 参数问题（session_id 空、content 非字符串），是调用方的锅
```

**记忆库挂了不能拖垮主链路。** 推荐包一层：

```python
def safe_recall(query, **kw):
    try:
        return db.recall(query, **kw)
    except Exception as exc:
        log.warning("memory recall failed: %s", exc)
        return []      # 拿不到记忆就空着，回复质量降一点，但不能不回
```

---

## 九、一份可直接抄的骨架

```python
import threading
import brain_memory as bm

class MemoryService:
    _db = None
    _lock = threading.Lock()

    @classmethod
    def init(cls, path):
        with cls._lock:
            if cls._db is None:
                cls._db = bm.MemoryDB(path, config={
                    "read_pool_size": 2,
                    "access_flush_interval_s": 5.0,
                })
        return cls._db

    @classmethod
    def db(cls):
        if cls._db is None:
            raise RuntimeError("MemoryService.init() 还没调用")
        return cls._db

    @classmethod
    def shutdown(cls):
        with cls._lock:
            if cls._db is not None:
                cls._db.close()
                cls._db = None

# Service.onCreate()
MemoryService.init("/data/data/com.example.app/files/memory.db")

# 收到一条消息
MemoryService.db().ingest(session_id, role, text)

# 生成回复前
mems = MemoryService.db().recall(user_text, top_k=5, budget_tokens=800)
context = "\n".join("- " + m.content for m in mems)

# Service.onDestroy()
MemoryService.shutdown()
```

---

## 十、反面清单

| 别做 | 后果 |
|---|---|
| 每条消息 new/close 一次 | 反复建表检查 + 线程启停，延迟翻十倍 |
| 用 `db.conn` 直接执行 SQL | 绕过锁与事务，可能写出不一致状态（`-O` 下该属性直接不可用） |
| 在写事务里发 HTTP 请求 | 写锁被占住，其他线程全部 `LockTimeoutError` |
| 把 `raw_events` 当对话历史全表扫 | 那是 append-only 归档，查历史用 `recall` |
| 手动 `DELETE FROM atoms` | 破坏 `atoms_fts` 一致性；要用 `forget(hard=True, confirm=True)` |
| 备份时只拷 `.db` 文件 | WAL 模式下要连 `-wal` / `-shm` 一起拷，或先 `close()` |
| 在 32 位设备上跑 | sqlite3 与 PRoot 都是 aarch64 假设，没测过 32 位 |

---

## 十一、冷启动

首次建库要跑 schema.sql（约 10ms）+ 建 FTS5 表。之后每次启动只是打开 + 校验 user_version。

如果 App 在 Application.onCreate 里建库，注意别阻塞主线程——放到后台线程，用 `CompletableFuture` 或直接开个线程，UI 先用空上下文。

---

## 十二、向量怎么配（Phase 2）

默认就是开的（`enable_vector=True`），后端自动选 `pure`（纯 Python 哈希向量）。
开箱即用，但要认清它的能力边界。

| 目标 | 配置 |
|---|---|
| 默认（零依赖） | 什么都不填 |
| 更准（真语义） | `embedding_callable=你的函数` 或 `embedding_backend="http"` |
| 更快（大库） | `pip install -e ".[vec]"`，自动切 sqlite-vec |
| 彻底关掉 | `enable_vector=False` |
| 只想要 FTS5 | `vector_backend_policy="none"` |

```python
# 自备模型（同步调用，返回等长向量列表）
import my_model

def embed(texts):
    return my_model.encode(list(texts)).tolist()

db = bm.MemoryDB(path, {"embedding_callable": embed, "vec_dim": 768})
```

三条硬要求：

1. **同步**。库在写事务里调用它，别在里面 await 或起线程。
2. **稳定**。同一段文本必须永远给同一个向量，模型换了要重建索引。
3. **快**。每次 ingest 会调一次；超过 50ms 就该换成异步预热或批量。

开销参考：纯 Python 384 维哈希向量约 0.3ms/条；numpy 点积扫 5000 条约 30ms；
纯 Python 扫 5000 条约 200ms。

```python
# 换模型后重建向量（Phase 2 提供的运维方式）
for atom_id, content in db._writer.execute("SELECT id, content FROM atoms WHERE state='active'"):
    db.vector_store.index_atom(db._writer, atom_id, content)
```

---

## 十三、接入真实 Embedding（可直接抄）

哈希向量只做兜底，要真语义就把 embedding 接上。两种写法，选一种。

### 方式 A：零依赖（urllib，推荐）

```python
import json
import urllib.request

import brain_memory as bm


def make_embedder(base_url, api_key, model, timeout=10.0, batch_size=16):
    """OpenAI 兼容 /embeddings 的 wrapper。返回一个 fn(texts) -> list[list[float]]。"""
    url = base_url.rstrip("/") + "/embeddings"

    def embed(texts):
        out = []
        for start in range(0, len(texts), batch_size):
            chunk = list(texts[start:start + batch_size])
            body = json.dumps({"model": model, "input": chunk}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + api_key,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            rows = sorted(payload["data"], key=lambda r: r.get("index", 0))
            out.extend(row["embedding"] for row in rows)
        return out

    return embed


embed = make_embedder(
    "https://api.openai.com/v1",
    "sk-xxxxxxxx",
    "text-embedding-3-small",
)

db = bm.MemoryDB("/data/data/com.example/files/mem.db", {
    "embedding_callable": embed,
    "vec_dim": 1536,          # 必须等于模型输出维度
    "vec_fail_limit": 3,      # 连续失败 3 次自动停用向量
    "vec_min_similarity": 0.25,   # 真模型的相似度分布更集中，阈值可以调高
})
```

### 方式 B：项目里已有 requests

```python
import requests


def make_embedder(base_url, api_key, model, timeout=10.0):
    url = base_url.rstrip("/") + "/embeddings"

    def embed(texts):
        resp = requests.post(
            url,
            json={"model": model, "input": list(texts)},
            headers={"Authorization": "Bearer " + api_key},
            timeout=timeout,
        )
        resp.raise_for_status()
        rows = sorted(resp.json()["data"], key=lambda r: r["index"])
        return [row["embedding"] for row in rows]

    return embed
```

注意：**库本身不依赖 requests**，上面这段是给你抄的；brain_memory 内部只用 stdlib。

### 哪些端点能用

只要是 OpenAI 兼容的 `/v1/embeddings` 就都行：

| 服务 | base_url | 常见模型 |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | text-embedding-3-small / large |
| 通义 DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` | text-embedding-v3 |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | embedding-3 |
| 本地 Xinference | `http://127.0.0.1:9997/v1` | bge-m3 / bge-large-zh |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | nomic-embed-text / bge-m3 |

**关于 DeepSeek**：截至本文档写作时，DeepSeek 官方 API 只有 chat/completions，**没有 embedding 接口**，
别硬接。要中文语义，用上面的通义 / 智谱 / 本地 BGE。

### 三条硬约束

1. **维度必须一致**。`vec_dim` 与模型输出维度不符 → vec0 建表失败，或暴力余弦因长度不等被跳过。
   `text-embedding-3-small` = 1536，`bge-m3` = 1024，`text-embedding-3-large` = 3072。
2. **同一模型从头用到尾**。中途换模型必须重建全部向量，否则新旧向量不在同一空间。
3. **批量别太大**。一次 16 条足够，PRoot 下网络本来就慢。

### 换模型后重建向量

```python
def reindex(db):
    store = db.vector_store
    with db._write_tx() as conn:
        rows = conn.execute(
            "SELECT id, content FROM atoms WHERE state = 'active'"
        ).fetchall()
        for atom_id, content in rows:
            store.index_atom(conn, atom_id, content)
    print("重建完成：%d 条" % len(rows))
```

### 失败时会怎样

| 情况 | 行为 |
|---|---|
| 单次调用超时 / 5xx | 这条不写向量（`index_atom` 返回 False），atoms 照常入库 |
| 连续失败 3 次 | 整个向量后端停用，`stats().vector_backend` 变 `none` |
| 停用后 | recall 退回 FTS5 + LIKE，业务不感知 |
| 想恢复 | 重启进程（重新 probe），或手动把 `backend` 设回 |

**永远不会因为 embedding 挂了而 ingest 失败。** 这条有测试覆盖。

---

## 十四、dream 什么时候跑

| 项 | 建议 |
|---|---|
| 频率 | 每天一次（凌晨）或充电空闲时 |
| 位置 | 后台线程 / WorkManager，**不要**在消息路径上 |
| 耗时 | 1000 条原子约 1–3 秒 |
| 副作用 | 写 `entity_pages` + 写 `DREAMS.md`（DB 同目录） |
| 幂等 | 是，可以重复跑，页面会被覆盖更新 |

```python
def nightly():
    db.dream()                    # 生成实体页与 DREAMS.md
    db.forget(dry_run=False, limit=200)   # 顺手把该静默的落状态
    db.maintenance("normal")
```

注意 `DREAMS.md` 默认落在 DB 同目录。如果 DB 在 App 私有目录，文件也在那，
不会出现在用户可见的文件管理器里；要放到 `/sdcard` 就配 `dreams_path`。

---

## 十五、阶段说明

- `enable_escalation` 默认关；开启后会真正下探 L2→L1→L0（`tests/test_escalation.py` 有断言，L0 只看最近 `escalate_l0_limit` 条）。
- 其余接口已全部落地，业务代码不需要为后续升级做适配 —— 签名不变。
