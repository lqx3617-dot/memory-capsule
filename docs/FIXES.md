# 记忆胶囊 · 全部问题合并清单（30 处补丁）

> 来源：三轮独立评审合并 —— ① 本地全量源码审读（P0/P1/P2） ② Claude 截图评审（4 条）
> ③ 两轮交叉验伪。每条都标了「谁先发现」，去重后按根因分组。

## 一、根因归纳（16 类问题，其实只有 5 个根）

| 根因 | 说明 | 涉及补丁 |
|---|---|---|
| **A. quality 双轨** | `atoms.quality` 既是写入初值（=confidence）又是实时重算值，谁说了算没定义 | P1-1 P2-2 P2-3 P2-4 |
| **B. 判据用词面代替语义** | 门控、冲突、去重都靠字符串相似度 + 词表，边界靠人肉枚举 | P1-2 P1-3 P2-1 |
| **C. 向量层没有规模概念** | 5000 条上限当性能预算用，实际变成正确性上限；维度混杂无防护 | P0-1 P0-2 P1-4 P1-5 P1-6 P1-7 P1-8 |
| **D. 并发收尾只挡一半** | `_closing` 立了旗，但 `_begin_op` 只认 `_closed` | P0-3a ~ P0-3e |
| **E. 文档比代码慢半拍** | 改了实现没回写文档，读者按旧文档理解新代码 | P2-8 P2-9 P2-10 DOC-1 ~ DOC-5 |

## 二、总表

| 编号 | 问题 | 位置 | 后果 | 严重度 | 谁发现 |
|---|---|---|---|---|---|
| P0-1 | numpy 分支 id 与分数错位 | vec.py `_search_bruteforce` | 返回**别的记忆**的 atom_id + 分数，静默错答 | P0 | 我 |
| P0-2 | 候选窗只取最老 5000 条 | vec.py `LIMIT` 无 `ORDER BY` | 库过 5000 条，新记忆从向量路永久消失 | P0 | 我 |
| P0-3 | close 期仍可进新操作 | db.py `_begin_op` | 收尾瞬间写连接被关 → `Cannot operate on a closed database` | P0 | 我 |
| P1-1 | silent 记忆无法复活 | promote.py merge 分支 | 用户重说一遍仍召回为空；`JOURNAL_ACTIONS` 里 `revive` 从未使用 | P1 | Claude |
| P1-2/3 | 冲突判定单向 | promote.py `check_conflict` | 误杀兼容事实 + 漏杀「九点/十点」数字改写 | P1 | Claude（漏杀侧我补） |
| P1-4/5/6 | 纯向量分支用名次冒充相似度 | recall.py | 第一名与第十名只差 0.9 倍，escalate 判定被污染 | P1 | 我 |
| P1-7/8 | vec0 用 `INSERT OR REPLACE` | vec.py `index_atom` | 报「后端可用」但索引写不进，KNN 永远空 | P1 | 我 |
| P2-1 | 疑问句判定扫句尾 8 字且含单字「么」 | gate.py `_is_question` | 「必须这么写」「不要问我为什么」「没什么好商量」被硬拒 | P2 | Claude（我独立命中，例子它更准） |
| P2-2/3/4 | connectivity 死代码（恒为 1） | forget.py | CHANGELOG 说「召回越多越稳」，实现里 refs 统计的是 supersedes，恒 0 | P2 | Claude（我补「dream 调用处连 refs 都没传」） |
| P2-5/6 | dream 的 SELECT 缺 access_count | dream.py | 与 forget 口径不一致 | P2 | 我 |
| P2-7 | 实体页标题判断恒假 | dream.py `_build_page` | 显示「entity，3 条」 | P2 | 我 |
| P2-8 | checkpoint 帧数取错下标 | db.py `maintenance` | 运维数字失真（row[1] 是日志帧数） | P2 | 我 |
| P2-9/10 | docstring/help 仍写「Phase 1 是桩」 | db.py / cli.py | 读者以为 dream 是空实现 | P2 | 我 |
| DOC-1 | README 写 62 个配置项 | README | 实际 73 项 | P2 | 我 + Claude |
| DOC-2/3 | README 写 `_smoke.py` 旧文件名 | README | 与 `tests/test_smoke.py` 不符 | P2 | Claude |
| DOC-4 | README 说 escalation「只留判定钩子」 | README | 实际已实现且有测试 | P2 | 我 + Claude |
| DOC-5 | CHANGELOG v1.0.0 vs pyproject 0.1.0.dev1 | 两文件 | 版本号两套 | P2 | 我 + Claude |

## 三、Claude 评审自身的错误（不修代码，但要知道）

1. **「目前只有 1 个 commit」—— 实际 41 个**（GitHub API 逐条数过，`2026-10-25 Initial commit` → `2026-10-27 fix(ci)`）。这一条直接错判了项目成熟度。
2. **把「4 条测试全绿」当质量结论**。它跑的测试只覆盖几条原子、无维度混杂、无并发 close，与 P0-1/2/3 完全不冲突。
3. **「代码约 7.7k 行（含文档和测试）」口径混淆**：包体 4217 行、测试 976、文档 1614、杂项 836，合计 7643。
4. **停在不该停的地方**：「要不要我把 1 和 2 的补丁写出来给你？」—— 出 diff 不需要写权限。本清单第 P1-1 就是它该直接给的东西。

## 四、应用顺序

```bash
python3 memory_capsule_fixes/apply_fixes.py /path/to/memory-capsule --dry-run
python3 memory_capsule_fixes/apply_fixes.py /path/to/memory-capsule
cp memory_capsule_fixes/tests/*.py /path/to/memory-capsule/tests/
cd /path/to/memory-capsule && sh check.sh --full
python3 tests/test_gate_assertive.py && python3 tests/test_revive.py \
  && python3 tests/test_connectivity.py && python3 tests/test_vec_window.py
```

顺序有依赖：P2-2（加参数）-> P2-3（用参数）-> P2-4（传参）；P2-5（取列）-> P2-6（传列）。
其余互不依赖。脚本对每个锚点都先验唯一，命中数 ≠1 就跳过报错，绝不做模糊替换。

## 五、还没修的（留给你决定）

- **去重称呼归一的误合并面**：`_call_name` 会把「这个接口叫 /api/v2」也提取成称呼；
  两条都提取到同一短串就合并（sim 直接当 1.0）。建议加「名字长度 ≥2 且不含路径符号」的白名单。
- **5 个死配置**：`commit_batch` / `raise_on_llm_error` / `max_candidates_per_ingest` /
  `enable_numpy_fallback` / `numpy_fallback_max_atoms`，全代码零引用。删会破坏 `--config` 兼容，建议保留但在 config.py 标 `# TODO 未接线`。
- **dream 孤儿实体页**：`entity_pages` 只 upsert 不清理，实体消失后旧页永留。
- **quality 双轨语义没根治**：本次只把三处调用口径拉齐，没定义「谁是真源」。
  建议下一步：`quality` 明确为「最近一次重算的快照」，写库时不再赋 =confidence。
- **sqlite-vec 路径仍无实测环境**（作者自述），P1-7 只是按 API 文档消掉一个已知坑。
- **纯 Python 向量仍是 O(n) 全扫**：P0-2 只解决了「失忆」，没解决 5000 条以上的耗时。
