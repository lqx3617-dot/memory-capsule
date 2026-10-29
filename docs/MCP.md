# 把记忆胶囊接成 MCP 插件

> 一句话：**不用把它「重写成插件」** —— 起一个 MCP 服务器进程，DSH、Operit、Claude Desktop、Cline 全都能用同一份。

## 为什么是 MCP

记忆胶囊是纯 stdlib + SQLite 的单文件库，天生适合被「一个常驻进程」包起来：

| 路线 | 工作量 | 评价 |
|---|---|---|
| 重写成 DSH 的 Cordis 插件（TypeScript） | 大：db / recall / gate 全要移植，FTS5 还得换 better-sqlite3 | 不推荐 |
| 重写成 Operit 的 ToolPkg | 中：要 JS 运行时 + SQLite 绑定 | 不推荐 |
| **包成 MCP 服务器** | 小：一个 mcp_server.py，纯 stdlib | 一次编写，两端通用 |

DSH 官方文档《连接第三方记忆 MCP 服务》已经把这个接口定死了：宿主负责「启动 stdio 命令 / 连 Streamable HTTP URL → 发现工具 → 以 mcp__<serverName>__<tool> 的形式暴露」，**不负责**下载服务器、初始化数据库、选 embedding 提供方 —— 这些正是 mcp_server.py 自己做的事。

参考：https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/mcp-memory.zh.md

## 启动

    # stdio（宿主拉子进程，最常用）
    python3 mcp_server.py --db ~/.brain-memory/memory.db

    # HTTP（宿主连 URL，适合常驻服务）
    python3 mcp_server.py --db ~/.brain-memory/memory.db --http 8788

Termux / proot-distro 里直接跑，不需要联网、不需要 root。

--config 可以覆盖任意 MemoryConfig 项（可重复）：

    python3 mcp_server.py --db mem.db \
      --config enable_escalation=true \
      --config embedding_backend=http \
      --config embedding_endpoint=http://127.0.0.1:8788/v1

## 暴露的 7 个工具

| 工具 | 用途 | 谁该调 |
|---|---|---|
| memory_ingest | 写入一句原始对话，过门控 | 用户说偏好 / 决定 / 约束 / 踩坑 / 待办时 |
| memory_remember | 确认过的事实，**跳过门控** | 模型自己压缩出的摘要条目 |
| memory_recall | 语义 + 关键词检索（FTS5 + 向量 RRF） | 历史信息可能相关时 |
| memory_context | 一次列出偏好 / 约束 / 决定 | 拼 system prompt，比检索可靠 |
| memory_forget | 遗忘扫描，默认 dry-run | 定期维护 |
| memory_dream | 巩固成 L3 实体页 + DREAMS.md | 低频（每天一次），别进热路径 |
| memory_stats | 各层计数与降级状态 | 排障 |

## DSH（DeepSeek Harness）

抄 examples/dsh-memory-mcp.cordis.yml，改成自己的路径，然后：

    dsh web --patch "$PWD/examples/dsh-memory-mcp.cordis.yml"

要长期生效，把里面那个 insert 条目并进 $DSH_HOME/cordis.patch.yml（全局）或
$DSH_HOME/profiles/<name>/cordis.patch.yml（单 profile）。**不要覆盖已有文件**，那里可能还有别的 patch。

挂上之后工具名是 mcp__brain-memory__memory_recall 这种形式。

## Operit AI

Operit 的 MCP 配置是 (name, endpoint, description, capabilities, extraData) 五元组，
抄 examples/operit-memory-mcp.json，按你那个 Operit 版本支持的传输改 endpoint：

- HTTP 传输：http://127.0.0.1:8788/mcp（先把 server 起起来）
- 本地命令传输：python3 /path/to/mcp_server.py --db /path/to/memory.db（若该版本支持）

注意：不同 Operit 版本对 endpoint 的解析不一致，以你机器上实际能连通的为准。

## 建议加进模型指令的一句话

工具描述不一定能让模型自觉调用，把这条加到系统提示词里（DSH 文档里的原文建议）：

> 用户要求记住某事时调用记忆写入工具；历史信息可能相关时，检索记忆并使用相关结果。

更省事的做法：每轮对话开头先调 memory_context，把偏好和约束整批注入 —— 这类信息
（「别给我发语音」）靠关键词检索是召不回来的，只能列。

## 验证（三步，与官方文档口径一致）

1. 会话 A：记住，我的验证饮料是正山小种-<随机后缀> → 模型应调 memory_ingest
2. 新开会话 B（不用重启宿主）：我的验证饮料是什么？查一下记忆 → 应调 memory_recall 并答出
3. 继续：用这个偏好给会议推荐一款茶 → 回答应引用召回值

## 全自动模式：不用等模型自觉

MCP 有个绕不开的短板：**要不要调工具，由模型自己决定**。模型这一轮偷懒，这次对话就没被记住。

解决办法不是逼模型，是把决定权从它手里拿走 —— 起一个 **OpenAI 兼容代理**，把宿主的 baseURL 指过去：

    python3 memory_proxy.py --db ~/.brain-memory/memory.db \
        --upstream https://api.deepseek.com/v1 --port 8790

    宿主 baseURL 填 http://127.0.0.1:8790/v1

它做两件事，都在转发前后顺手完成：

| 时机 | 动作 |
|---|---|
| 请求前 | 拿最后一条 user 消息去 recall，把【相关记忆】+【长期偏好与约束】拼进 system |
| 响应后 | 把这一轮的 user / assistant 原文写回 L0（assistant 会被门控拒收，只记用户说的） |

好处是**宿主完全不用支持 MCP**，任何 OpenAI 兼容客户端都能用；代价是每轮多一次本地 SQLite 查询（毫秒级）。

三种模式怎么选：

| 场景 | 用哪个 |
|---|---|
| 宿主支持 MCP、模型听话 | mcp_server.py（工具调用，最灵活） |
| 宿主只认 baseURL / 模型懒得调工具 | memory_proxy.py（全自动，最稳） |
| 两个都要 | 同时开，共用同一个 db 文件（SQLite WAL 支持多进程读写） |

代理的开关：

    --top-k 5            每轮注入几条记忆
    --budget-tokens 800  注入预算
    --no-facts           不注入长期偏好（省 token）
    --no-write-back      只注入，不记录对话（隐私模式）
    X-Memory: off        单个请求跳过（请求头）

## Termux 保活

    sh scripts/run-memory-mcp.sh                          # MCP HTTP :8788
    sh scripts/run-memory-proxy.sh https://api.deepseek.com/v1   # 代理 :8790

脚本会自动 `termux-wake-lock`。但**还要手动关掉电池优化**（电池 → 应用启动管理 → Termux → 允许后台），
否则息屏几分钟照样被杀。国产 ROM（MIUI / ColorOS / OriginOS）这一步尤其必须做。

## 已知边界

- **进程寿命**：Android 会杀后台进程。Termux 要 termux-wake-lock；Operit 若走 HTTP 传输，
  建议把 mcp_server.py 挂在 OperitTerminal 或 proot 里跑。
- **调用时机靠模型**：MCP 没有「每条消息都自动写」的钩子。要 100% 不漏，得在宿主侧加 hook
  （DSH 有插件钩子，那是另一条路）。
- **纯 Python 向量是 O(n)**：超过万条建议 pip install -e ".[numpy]" 或 ".[vec]"。
- **stdio 与 HTTP 二选一**：stdio 更简单（宿主管生命周期），HTTP 更适合多客户端共存。
- **代理在请求路径上，会看到明文对话**：不想记录就加 `--no-write-back`，或单个请求带 `X-Memory: off`。
