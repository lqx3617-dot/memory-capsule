#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆胶囊 · MCP 服务器（纯 stdlib，零依赖）。

把 brain_memory 暴露成 Model Context Protocol 工具，任何支持 MCP 的宿主
（DeepSeek Harness / Operit AI / Claude Desktop / Cline …）都能直接调用，
不需要被宿主"重写成插件"。

两种传输：
    python3 mcp_server.py --db /path/memory.db                 # stdio（宿主拉子进程，最常用）
    python3 mcp_server.py --db /path/memory.db --http 8788     # Streamable HTTP（宿主连 URL）

三条硬约束（与 brain_memory 本身同一套哲学）：
1. 只用 stdlib —— 不引入 mcp SDK，不给宿主添依赖，Termux/proot 里也能跑。
2. stdout 只走协议报文，日志一律 stderr。宿主按行解析 stdout，混一行日志就整个会话废掉。
3. 单进程常驻、MemoryDB 懒加载复用。进程被系统杀掉也不丢数据 —— SQLite 是 WAL。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any

SERVER_NAME = "brain-memory"
SERVER_VERSION = "1.1.0"
# 客户端给的版本在列表里就原样回显，否则回最新的
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
FALLBACK_PROTOCOL = PROTOCOL_VERSIONS[0]


def _log(message: str) -> None:
    print("[%s] %s" % (SERVER_NAME, message), file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 业务层


class MemoryService:
    """包一层 MemoryDB：懒加载 + 参数校验 + 统一文本输出。"""

    def __init__(self, db_path: str, config: dict[str, Any] | None = None) -> None:
        self.db_path = os.path.abspath(db_path)
        self.config = config or {}
        self._db: Any = None

    @property
    def db(self) -> Any:
        if self._db is None:
            import brain_memory as bm

            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._db = bm.MemoryDB(self.db_path, self.config or None)
            _log("已打开 %s（journal=%s vec=%s）" % (
                self.db_path, self._db.journal_mode, self._db.vector_backend))
        return self._db

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None

    # ---- 工具实现 ----

    def ingest(self, args: dict[str, Any]) -> str:
        content = str(args.get("content") or "").strip()
        if not content:
            raise ValueError("content 不能为空")
        session_id = str(args.get("session_id") or args.get("session") or "mcp")
        role = str(args.get("role") or "user")
        event_id = self.db.ingest(session_id, role, content)
        st = self.db.stats()
        return "已写入原始事件 #%d（L0）。当前 L2 活跃原子 %d 条。" % (
            event_id, st.counts.get("l2_active", 0))

    def remember(self, args: dict[str, Any]) -> str:
        content = str(args.get("content") or "").strip()
        if not content:
            raise ValueError("content 不能为空")
        session_id = str(args.get("session_id") or args.get("session") or "mcp")
        fact_type = str(args.get("fact_type") or "fact")
        try:
            confidence = float(args.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        event_id = self.db.remember(session_id, content, fact_type=fact_type,
                                    confidence=max(0.0, min(1.0, confidence)))
        return "已作为 %s 记住（事件 #%d，跳过门控）。" % (fact_type, event_id)

    def recall(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query 不能为空")
        top_k = int(args.get("top_k") or 5)
        budget = int(args.get("budget_tokens") or 2000)
        kwargs: dict[str, Any] = {}
        if args.get("min_quality") is not None:
            kwargs["min_quality"] = float(args["min_quality"])
        if args.get("fact_types"):
            kwargs["fact_types"] = [str(t) for t in args["fact_types"]]
        if args.get("session_id"):
            kwargs["session_id"] = str(args["session_id"])
        hits = self.db.recall(query, top_k, budget, **kwargs)
        if not hits:
            return "没有检索到相关记忆。"
        lines = ["检索到 %d 条（按相关度）:" % len(hits)]
        for i, m in enumerate(hits, 1):
            tag = m.fact_type if m.layer == "L2" else m.layer
            lines.append("%d. [%s q=%.2f] %s" % (i, tag, m.quality, m.content))
        return "\n".join(lines)

    def context(self, args: dict[str, Any]) -> str:
        """整批注入用：把偏好 / 约束这类"必须一直知道"的事实直接列出来。"""
        fact_types = args.get("fact_types") or ["preference", "constraint", "decision"]
        limit = int(args.get("limit") or 20)
        min_quality = float(args.get("min_quality") or 0.0)
        rows = self.db.facts(fact_types=[str(t) for t in fact_types],
                             min_quality=min_quality, limit=limit)
        if not rows:
            return "（暂无需要常驻的偏好与约束）"
        lines = ["需要一直记住的 %d 条:" % len(rows)]
        for m in rows:
            lines.append("- [%s q=%.2f] %s" % (m.fact_type, m.quality, m.content))
        return "\n".join(lines)

    def forget(self, args: dict[str, Any]) -> str:
        dry_run = bool(args.get("dry_run", True))
        older = args.get("older_than_days")
        decisions = self.db.forget(
            dry_run=dry_run,
            older_than_days=float(older) if older is not None else None,
        )
        if not decisions:
            return "没有需要遗忘的条目。"
        head = "扫描到 %d 条%s:" % (len(decisions), "（dry-run，未改动）" if dry_run else "（已落盘）")
        lines = [head]
        for d in decisions[:30]:
            lines.append("- #%s %s（quality=%.2f 曝光=%d）%s" % (
                d.get("atom_id"), d.get("action"), d.get("quality", 0.0),
                d.get("access_count", 0), str(d.get("content", ""))[:60]))
        return "\n".join(lines)

    def dream(self, args: dict[str, Any]) -> str:
        persist = bool(args.get("persist", True))
        text = self.db.dream(persist=persist)
        return text[:4000]

    def stats(self, args: dict[str, Any]) -> str:
        st = self.db.stats()
        c = st.counts
        return "\n".join([
            "L0 事件 %d 条 / L1 待审 %d / L2 活跃 %d / L3 实体页 %d" % (
                c.get("l0_events", 0), c.get("l1_pending", 0),
                c.get("l2_active", 0), c.get("l3_pages", 0)),
            "平均质量 %.3f / 平均置信 %.3f" % (st.avg_quality, st.avg_confidence),
            "journal=%s vec=%s fts=%s llm=%s" % (
                st.journal_mode, st.vector_backend, st.fts_tokenizer, st.llm_mode),
            "库大小 %d 字节 / sqlite %s" % (st.db_bytes, st.sqlite_version),
        ])


def _obj(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": desc,
        "inputSchema": {"type": "object", "properties": props, "required": required},
    }


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_STRARR = {"type": "array", "items": {"type": "string"}}

TOOLS = [
    _obj("memory_ingest",
         "写入一句原始对话。经过编码门控判断值不值得长期记住；不值得的只留 L0（可溯源，不会被检索）。"
         "用户说出偏好、决定、约束、Bug 根因、待办时调用。",
         {"content": _STR, "session_id": _STR, "role": _STR}, ["content"]),
    _obj("memory_remember",
         "应用层已确认的事实，跳过门控直接入库。适合模型自己压缩出来的摘要条目（「在杭州」「有只猫叫豆豆」）。",
         {"content": _STR, "session_id": _STR,
          "fact_type": {"type": "string",
                        "enum": ["fact", "decision", "task", "preference",
                                 "constraint", "bugroot"]},
          "confidence": _NUM},
         ["content"]),
    _obj("memory_recall",
         "按语义 + 关键词检索长期记忆（FTS5 + 向量 RRF 融合）。历史信息可能相关时先调这个。",
         {"query": _STR, "top_k": _INT, "budget_tokens": _INT,
          "min_quality": _NUM, "fact_types": _STRARR, "session_id": _STR},
         ["query"]),
    _obj("memory_context",
         "一次性列出需要常驻的偏好 / 约束 / 决定，用来拼进 system prompt。这类需求不能靠关键词检索。",
         {"fact_types": _STRARR, "limit": _INT, "min_quality": _NUM}, []),
    _obj("memory_forget",
         "遗忘扫描。默认 dry-run 只报告不写库；落盘只会把条目转成 silent/archived，永不物理删除。",
         {"dry_run": _BOOL, "older_than_days": _NUM, "limit": _INT}, []),
    _obj("memory_dream",
         "巩固：把高质量原子聚成 L3 实体页并写 DREAMS.md。低频调用（比如每天一次），不要放进对话热路径。",
         {"persist": _BOOL, "max_atoms": _INT}, []),
    _obj("memory_stats", "查看各层计数、降级状态与库大小。", {}, []),
]

_DISPATCH = {
    "memory_ingest": "ingest",
    "memory_remember": "remember",
    "memory_recall": "recall",
    "memory_context": "context",
    "memory_forget": "forget",
    "memory_dream": "dream",
    "memory_stats": "stats",
}


# ---------------------------------------------------------------- 协议层


def _result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(svc: MemoryService, req: dict[str, Any]) -> dict[str, Any] | None:
    """处理一条 JSON-RPC。返回 None 表示这是 notification，不需要回应。"""
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        want = str(params.get("protocolVersion") or "")
        version = want if want in PROTOCOL_VERSIONS else FALLBACK_PROTOCOL
        return _result(req_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": ("长期记忆库。用户要求记住某事时调用 memory_ingest；"
                             "历史信息可能相关时调用 memory_recall。"),
        })

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        fn = _DISPATCH.get(name)
        if fn is None:
            return _result(req_id, {
                "content": [{"type": "text", "text": "未知工具: " + name}],
                "isError": True,
            })
        if not isinstance(args, dict):
            args = {}
        try:
            text = getattr(svc, fn)(args)
            return _result(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:
            _log("工具 %s 失败: %s" % (name, exc))
            _log(traceback.format_exc())
            return _result(req_id, {
                "content": [{"type": "text", "text": "%s 失败: %s" % (name, exc)}],
                "isError": True,
            })

    if method in ("resources/list", "prompts/list"):
        key = "resources" if method.startswith("resources") else "prompts"
        return _result(req_id, {key: []})

    if req_id is None:
        return None
    return _error(req_id, -32601, "未实现的方法: %s" % method)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# ---- stdio ----


def serve_stdio(svc: MemoryService) -> int:
    _log("stdio 就绪，等待宿主……")
    while True:
        # 必须用 readline：for line in sys.stdin 走内部块缓冲，
        # 管道场景下宿主发了请求也读不到，整条链会挂死。
        raw = sys.stdin.readline()
        if not raw:
            break
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            sys.stdout.write(_dumps(_error(None, -32700, "JSON 解析失败")) + "\n")
            sys.stdout.flush()
            continue
        if not isinstance(req, dict):
            continue
        try:
            resp = handle(svc, req)
        except Exception as exc:
            _log("处理失败: %s" % exc)
            resp = _error(req.get("id"), -32603, "内部错误: %s" % exc)
        if resp is not None:
            sys.stdout.write(_dumps(resp) + "\n")
            sys.stdout.flush()
    _log("stdin 关闭，退出")
    return 0


# ---- HTTP ----


def serve_http(svc: MemoryService, host: str, port: int) -> int:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _write(self, payload: dict[str, Any] | None) -> None:
            body = b"" if payload is None else _dumps(payload).encode("utf-8")
            self.send_response(204 if payload is None else 200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Mcp-Session-Id", "brain-memory")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception as exc:
                self._write(_error(None, -32700, "请求体解析失败: %s" % exc))
                return
            if isinstance(payload, list):          # 批量
                out = [r for r in (handle(svc, one) for one in payload) if r is not None]
                self._write(out or None)
                return
            self._write(handle(svc, payload))

        def do_GET(self) -> None:  # noqa: N802
            self._write({"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION})

        def log_message(self, *args: Any) -> None:   # 别往 stderr 刷访问日志
            return

    httpd = ThreadingHTTPServer((host, port), Handler)
    _log("HTTP 就绪 http://%s:%d/mcp" % (host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


# ---------------------------------------------------------------- 入口


def build_config(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit("--config 需要 K=V 形式: " + item)
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="记忆胶囊 MCP 服务器")
    ap.add_argument("--db", required=True, help="SQLite 路径，例如 ~/.brain-memory/memory.db")
    ap.add_argument("--http", type=int, default=None, metavar="PORT",
                    help="走 Streamable HTTP 而不是 stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--config", action="append", default=[], metavar="K=V",
                    help="覆盖 MemoryConfig，可重复")
    ap.add_argument("--version", action="version", version=SERVER_NAME + " " + SERVER_VERSION)
    args = ap.parse_args(argv)

    svc = MemoryService(args.db, build_config(args.config))
    try:
        _ = svc.db          # 启动即建库，配置错了立刻死，别等第一次调用
        if args.http:
            return serve_http(svc, args.host, args.http)
        return serve_stdio(svc)
    finally:
        svc.close()


if __name__ == "__main__":
    sys.exit(main())
