# 角色
你是一名资深后端工程师，正在为一个运行在 Android PRoot 环境
（Termux + proot-distro，aarch64 Linux 用户态，非真容器、无内核隔离）
的 Python App，构建“可嵌入的长期记忆库”。
产出必须是能直接 import 的库，不是独立服务、不是 CLI 工具优先。

# 运行环境硬约束（违反视为失败）
1. Python 3.10+，aarch64，PRoot 用户态，无 root、无 systemd。
2. PRoot 不是真容器：ptrace 拦截系统调用，I/O 有额外开销，
   /dev/shm 可能不可用或受限，mmap 行为不稳定。
3. SQLite 必须做降级：
   - 启动时先尝试 PRAGMA journal_mode=WAL，失败则回退 DELETE；
   - 无论哪种模式，设置 PRAGMA synchronous=NORMAL；
   - 设置 PRAGMA mmap_size=0，禁用 mmap；
   - 设置 PRAGMA busy_timeout=5000；
   - 设置 PRAGMA temp_store=MEMORY；
   - 初始化日志必须打印实际生效的 journal_mode。
4. 不依赖 /dev/shm、不依赖 fork 多进程、不依赖 systemd 常驻服务。
5. sqlite-vec 是可选 extra：加载失败（.so 架构不匹配、扩展加载被禁用）
   必须自动降级为纯 FTS5，不能崩溃。加载前用 try/except 包住
   enable_load_extension。
6. 无 LLM 依赖也能跑：LLM 抽取是可选 extra，未配置时用关键词规则门控。
7. 不引入 torch / transformers / 任何需要模型权重的依赖。

# 交付形态（最重要）
1. 交付一个 Python 包 brain_memory，App 通过 import brain_memory 使用。
2. 核心 API 只有五个，签名必须稳定：
   - MemoryDB(path, config=None)：初始化，path 是 SQLite 文件路径。
   - mem.ingest(session_id, role, content) -> event_id：写入原始事件。
   - mem.recall(query, top_k=5, budget_tokens=2000) -> list[Memory]：
     返回带 content / source_event_id / confidence / quality 的结果。
   - mem.forget(dry_run=True) -> list[dict]：返回将被静默/归档的条目。
   - mem.dream() -> str：执行一次巩固，返回生成的摘要文本。
   - mem.close()：退出时调用，确保 checkpoint + 关连接。
3. 库必须线程安全：内部用单个写连接 + 读写锁；读操作可共享
   2-3 个连接池，写操作串行；所有连接 check_same_thread=False
   并自行加锁。不追求高并发，追求不死锁、不丢数据。
4. 库必须无外部服务依赖：不需要 HTTP 服务、Redis、单独进程。
5. CLI 可作为附带品，但不是主要交付物。

# 技术栈
- Python 3.10+，核心只用 stdlib + sqlite3。
- 向量检索：pip install brain_memory[vec] 提供 sqlite-vec 支持；
  未安装或加载失败时自动降级为纯 FTS5 + quality 排序。
  可选提供一个纯 Python 的 numpy 暴力余弦兜底（仅当 atoms < 5000 时启用）。
- LLM 抽取：[llm] extra，未配置时用规则门控。

# 硬约束
1. 不做模型参数微调，“巩固”只做外部存储的晋升、摘要、归档，
   不改变 LLM 权重。不要生成任何 torch / finetune / LoRA 代码。
2. 所有遗忘默认 dry_run=True，只有显式 False 才写归档状态，
   绝不物理删除。提供手动 forget --hard 覆盖，但默认不用。
3. 每个机制可配置开关，默认保守（门控严格、遗忘温和）。
4. 第一阶段只做 L0→L2 + recall + forget；
   L3 / dream / 向量放第二阶段。
5. 先写 PLAN.md 和 PUBLIC_API.md，经我确认后再写代码。

# 架构：L0→L4 五层记忆管道
- L0 raw_events：append-only 原始对话/事件，不可变。
- L1 candidates：门控抽取的候选事实（Fact/Decision/Task/Preference），
  status=pending，等待晋升。
- L2 atoms：晋升后的结构化事实，带 entity_id、confidence、quality、
  access_count、last_accessed_at、created_at、supersedes_id。
  FTS5 索引 + 可选向量索引。
- L3 entity_pages：按实体（User/Project/Concept）聚合的 Markdown 摘要页，
  由 L2 归纳生成，dirty 标记触发重新生成（第二阶段）。
- L4 journal：append-only 审计日志，记录 promote/merge/conflict/
  deprecate/archive 操作。

# 核心机制
## 1. 编码门控 Perception Gate
ingest 时判断是否值得进入 L1：架构决策 / 用户偏好 / 项目惯例 /
Bug 根因 / 关键约束 → 收；临时调试输出、已有文档信息、琐碎事实 → 拒。
规则优先，LLM 可选；LLM 调用失败回退关键词规则。

## 2. 晋升 Promotion
L1→L2 五项检查：价值 → 证据 → 实体链接 → 去重 → 冲突检测。
冲突时旧原子标记 superseded，新原子通过 supersedes 边指向旧原子，
不物理删除。L2→L3 由 dirty 触发，第二阶段实现。

## 3. 遗忘 Rank-based Forgetting
quality = clamp01(feedback * decay * connectivity)
- feedback：used(1.0) / useful(0.8) / noise(0.0)，只有 noise 归零。
- decay：按记忆类型分半衰期。情景约7天，语义约90天，程序约365天。
  用 last_accessed_at 驱动指数衰减。
- connectivity：被 EntityPage 或其他原子引用数，对数缩放。
低质量 + 低曝光 → 静默（不返回，不删除）。
低质量 + 高曝光 → 归档（不可检索，可恢复）。
绝不自动物理删除。

## 4. 巩固 / 重放 Consolidation（第二阶段）
提供可调度的 dream()：
- 扫描 L2 中高 quality 原子；
- 聚类相似原子，生成 L3 EntityPage 摘要；
- 反复被检索的 episodic 原子标记 semanticized，延长半衰期；
- 生成 DREAMS.md 人类可读日记。
巩固不决定保留/删除；保留由 rank 决定。
不在 recall 热路径中执行。

## 5. 检索 Hybrid Recall
查询管道：parse → gather（FTS5 + 可选向量）→ rerank →
suppress（低 quality）→ token budget 截断 → 返回带来源片段。
第一阶段只做 FTS5 关键词召回 + quality 排序。
支持 confidence-based escalation 骨架：低置信度时扩大范围 L2→L1→L0。

# SQLite Schema（第一阶段）
raw_events(id, session_id, role, content, created_at)
candidates(id, raw_event_id, fact_type, content, status, created_at)
atoms(id, entity_id, fact_type, content, confidence, quality,
      access_count, last_accessed_at, created_at, supersedes_id)
entities(id, name, type, aliases_json)
journal(id, action, target_id, detail_json, created_at)
atoms_fts(FTS5 virtual table on atoms.content)
向量列（第二阶段）用 sqlite-vec 的 vec0 虚拟表，维度可配置。

# PRoot 性能要求
- ingest 单条延迟目标 < 50ms（靠 synchronous=NORMAL 和批量提交控制）。
- recall 单次延迟目标 < 100ms（atoms < 10k 时）。
- 提供 mem.maintenance()：执行 PRAGMA optimize + 定期
  PRAGMA wal_checkpoint(TRUNCATE)（WAL 可用时）或低频 VACUUM。
  由 App 在充电/空闲时调用，不在热路径跑。
- 禁止在 recall 热路径里做写操作：访问计数用内存累加，批量刷盘。

# 工程要求
- 先写 PLAN.md 和 PUBLIC_API.md，明确五个 API 的签名、返回值、
  异常情况、降级策略（WAL→DELETE、vec→FTS5、LLM→规则）、
  并发模型。经我确认后再写代码。
- 单元测试用临时目录下的 SQLite 文件，不 mock 存储层。
- 提供 examples/embed_demo.py，模拟 App 的 ingest/recall/forget/close 流程。
- stats() 返回：各层数量、平均 quality、静默/归档数、当前 journal_mode。

# 第一阶段验收标准（PRoot 环境）
1. MemoryDB(path) 初始化成功，重复初始化不报错，
   日志中明确打印实际生效的 journal_mode（WAL 或 DELETE）。
2. ingest → recall 能召回刚写入的关键事实，且带 source_event_id。
3. 灌“今天天气不错”，candidates 应拒收。
4. forget(dry_run=True) 返回列表，数据库内容不变。
5. 两个线程并发 ingest + recall 各 100 次，不报错、不死锁。
6. 模拟 sqlite-vec 加载失败（不装扩展），recall 仍能工作。
7. examples/embed_demo.py 能直接运行并打印结果。
8. 在 PRoot 下实测：ingest 100 条 + recall 50 次，总耗时打印出来。