# 贡献指南

先说清楚这个项目的边界 —— 它刻意做成这样，不是能力不足：

## 设计红线（PR 会因为这些被拒）

| 红线 | 原因 |
|---|---|
| **不加第三方依赖** | 整个库只用 stdlib + sqlite3。torch / transformers / numpy / requests 一律不引入。numpy 和 sqlite-vec 是**可选**加速路径，必须能在缺失时降级 |
| **不要求网络** | 核心路径（ingest / recall / facts / forget）必须离线可用。LLM 只用于「候选提炼」和「巩固」，且必须可降级到规则 |
| **不破坏三个降级路径** | WAL→DELETE、sqlite-vec→numpy→pure→none、LLM→规则+60s 熔断。任何改动都要保证这三条链完整 |
| **不改 schema 语义** | 加字段可以（走 `_migrate`），改已有字段含义不行 |

## 怎么跑测试

```bash
sh check.sh          # 基础四步：静态扫描 / 文档代码块 / 编译 / 建库体检
sh check.sh --full   # 加上 5 个测试脚本
```

CI 会在 Python 3.10–3.13 上跑全量，PR 必须全绿。

## 提 PR 之前

1. **先开 issue 说清楚要解决什么** —— 尤其是改 schema 或改检索逻辑
2. **带上复现** —— 数据是什么、期望什么、实际什么
3. **写清楚降级行为** —— 你的改动在「没有 numpy」「没有 sqlite-vec」「LLM 挂了」三种情况下分别怎样
4. **别动注释里的实测记录** —— 那些形如 `⚠ 2026-10-22 实测：…` 的注释是踩坑档案，删了就没人知道为什么这么写

## 合并纪律（2026-10-29 补，因为踩过）

这个仓库装了 chatgpt-codex-connector[bot]。**一开 PR 它就自动审**，不用 @。

v1.2.0 之前有三轮 PR 在没读它意见的情况下被合并，其中一条是 **P1**：
MCP 的 HTTP 端点不校验 Origin，任何网页都能借 DNS rebinding 打到 127.0.0.1，
读写用户的记忆。那 12 条意见里**没有一条是风格建议** —— 全是「某个输入会让某个功能静默出错」。

现在由 review-gate 强制：

| 规则 | 落地方式 |
|---|---|
| 每条 bot 行内意见必须点 Resolve conversation | .github/workflows/review-gate.yml 查 GraphQL reviewThreads，未解决就红 |
| 确实不该改的 | 在 thread 里写理由，再 resolve —— 别硬改，也别硬合 |
| 紧急放行 | 打 review-gate:skip 标签，会在 step summary 留痕 |
| 意见处理完但 check 还是红 | 在 thread 里**回一句话**（触发重跑），或到 Actions 里点 **Re-run jobs**。别用 workflow_dispatch —— 它产生的 check 不计入 PR 的 required status checks，清不了红 |
| 本地先看 | GITHUB_TOKEN=... python3 scripts/check_review_findings.py 5 |

顺序固定：**读意见 → 逐条处置 → Resolve → 再看 CI 绿不绿**。
跳步的代价是 v1.2.0 带着 P1 出门、v1.2.1 才补回来。

## 代码风格

- 全中文注释，说明「为什么」而不是「做了什么」
- 历史踩坑用 `⚠ 日期 实测：` 开头，写清现象和当时的判断
- 函数超过 40 行考虑拆分，但**别为了短而拆** —— 拆成两个各 20 行的函数、中间传 8 个参数，比 40 行直白更难读

## 想加新能力？

先读 `PLAN.md` 的「不做清单」。很多想法是**故意不做的**：

- 不做多模态记忆（图/音）—— 那是上层的事
- 不做知识图谱推理 —— L3 实体页只是索引，不是推理引擎
- 不做分布式 —— 单机 SQLite 就是明确取舍

## 报告问题

带上这些信息会快很多：

```
python -V
python -c "import sqlite3; print(sqlite3.sqlite_version)"
python -c "import brain_memory; print(brain_memory.__version__)"
```

以及 `db.stats()` 的输出。
