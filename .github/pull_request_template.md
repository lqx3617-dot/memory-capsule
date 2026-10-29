<!--
  这个模板只干一件事：把「合并前清空 bot findings」变成默认动作。
  v1.2.0 之前漏看过三次 Codex 的意见，其中一条是 P1（DNS rebinding 可读写记忆）。
-->

## 这个 PR 做什么

（一句话。修什么、为什么，别写「优化代码」）

## 动到了哪一层

- [ ] L0 / L1 / L2 / L3 表结构或迁移
- [ ] 门控（gate）
- [ ] 晋升 / 去重 / 冲突（promote）
- [ ] 检索（recall / vec / RRF）
- [ ] 遗忘或巩固（forget / dream）
- [ ] 并发 / 锁 / 关闭时序
- [ ] 宿主接入（mcp_server / memory_proxy）
- [ ] 只改文档或测试

## 合并前检查（缺一项别点 Merge）

- [ ] 读过 chatgpt-codex-connector[bot] 的每一条行内意见
- [ ] 每条要么已修（附 commit），要么在 thread 里写明为什么不该改
- [ ] 所有 bot 意见的 thread 已点 Resolve conversation —— review-gate 会拦未解决的
- [ ] sh check.sh --full 本地全绿
- [ ] 没有引入第三方依赖（stdlib only，numpy / sqlite-vec 只能是可选路径）
- [ ] 三条降级链仍完整：WAL→DELETE、vec→numpy→pure→none、LLM→规则+熔断
- [ ] 涉及数值阈值的改动，测试断言里没有为了「让它过」而放宽的界限

## 测试与复现

（新加的测试脚本 + 手动验证步骤。修 bug 的话，写清修之前是什么现象）
