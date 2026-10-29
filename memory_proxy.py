#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆胶囊 · OpenAI 兼容记忆代理。

MCP 解决的是「宿主支持工具调用」，但它有个绕不开的短板：
**要不要调记忆工具，由模型自己决定**。模型这一轮偷懒，这次对话就没被记住。

这个代理把决定权从模型手里拿走：把宿主的 baseURL 指到这里，
它会在转发前后各做一件事 ——

    请求前：拿最后一条 user 消息去检索，把相关记忆拼进 system
    响应后：把这一轮的 user / assistant 原文写回 L0

宿主不需要支持 MCP，也不需要改代码，任何 OpenAI 兼容客户端都能用。

    python3 memory_proxy.py --db ~/.brain-memory/memory.db \
        --upstream https://api.deepseek.com/v1 --port 8790

    宿主 baseURL 填 http://127.0.0.1:8790/v1

三条纪律（与 brain_memory 一致）：
1. 纯 stdlib，不装 openai SDK。
2. 记忆出错绝不影响主链路：注入失败就直通，写入失败只记日志。
3. 不碰 Authorization —— 上游的 key 原样透传，代理不存不管。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROXY_NAME = "brain-memory-proxy"
PROXY_VERSION = "1.0.0"

_MEMORY_HEADER = (
    "以下是关于当前用户的长期记忆，可能与本轮相关。按需使用，不要逐条复述，"
    "也不要声称你记得没被列出的东西。"
)


def _log(message: str) -> None:
    print("[%s] %s" % (PROXY_NAME, message), file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 记忆侧


class MemoryBridge:
    """包 MemoryDB。所有方法都不抛 —— 记忆坏了不该拖垮对话。"""

    def __init__(self, db_path: str, config: dict[str, Any] | None = None,
                 top_k: int = 5, budget_tokens: int = 800,
                 inject_facts: bool = True, fact_limit: int = 8,
                 write_back: bool = True) -> None:
        self.db_path = os.path.abspath(db_path)
        self.config = config or {}
        self.top_k = top_k
        self.budget_tokens = budget_tokens
        self.inject_facts = inject_facts
        self.fact_limit = fact_limit
        self.write_back = write_back
        self._db: Any = None
        self._lock = threading.Lock()

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

    def build_block(self, query: str) -> str:
        """把「相关记忆 + 常驻偏好」拼成一段可注入的文本。失败返回空串。"""
        if not query:
            return ""
        parts: list[str] = []
        try:
            hits = self.db.recall(query, self.top_k, self.budget_tokens)
            if hits:
                parts.append("【相关记忆】")
                for m in hits:
                    parts.append("- " + m.content)
        except Exception as exc:
            _log("检索失败（已忽略）: %s" % exc)
        if self.inject_facts:
            try:
                rows = self.db.facts(
                    fact_types=["preference", "constraint"], limit=self.fact_limit)
                if rows:
                    parts.append("【长期偏好与约束】")
                    for m in rows:
                        parts.append("- " + m.content)
            except Exception as exc:
                _log("facts 失败（已忽略）: %s" % exc)
        if not parts:
            return ""
        return _MEMORY_HEADER + "\n\n" + "\n".join(parts)

    def write_turn(self, session_id: str, user_text: str, assistant_text: str) -> None:
        """把这一轮写回 L0。assistant 的内容会被门控拒收，这是设计 —— 只记用户说的。"""
        if not self.write_back:
            return
        try:
            if user_text:
                self.db.ingest(session_id, "user", user_text)
            if assistant_text:
                self.db.ingest(session_id, "assistant", assistant_text)
        except Exception as exc:
            _log("写入失败（已忽略）: %s" % exc)


# ---------------------------------------------------------------- 报文工具


def _text_of(content: Any) -> str:
    """OpenAI 的 content 可能是字符串，也可能是 [{type:text,text:...}] 数组。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in (None, "text"):
                out.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(x for x in out if x)
    return ""


def inject_system(payload: dict[str, Any], block: str) -> bool:
    """把记忆块塞进 system。已有 system 就追加，没有就插在最前面。"""
    if not block:
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            msg["content"] = _text_of(msg.get("content")) + "\n\n" + block
            return True
    messages.insert(0, {"role": "system", "content": block})
    return True


def last_user_text(payload: dict[str, Any]) -> str:
    messages = payload.get("messages") or []
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return _text_of(msg.get("content")).strip()
    return ""


def assistant_text_of(payload: dict[str, Any]) -> str:
    try:
        return _text_of(payload["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError):
        return ""


# ---------------------------------------------------------------- HTTP 侧


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bridge: MemoryBridge
    upstream: str
    timeout: float

    # ---- 公共 ----

    def log_message(self, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _session_id(self, payload: dict[str, Any]) -> str:
        for key in ("user", "session_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return "proxy:" + value[:64]
        header = self.headers.get("X-Session-Id")
        if header:
            return "proxy:" + header[:64]
        return "proxy:default"

    # ---- GET ----

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/health", ""):
            self._send_json(200, {
                "status": "ok",
                "name": PROXY_NAME,
                "version": PROXY_VERSION,
                "upstream": self.upstream,
                "inject_top_k": self.bridge.top_k,
                "write_back": self.bridge.write_back,
            })
            return
        self._send_json(404, {"error": "not found"})

    # ---- POST ----

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._send_json(400, {"error": "请求体解析失败: %s" % exc})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "只接受 JSON 对象"})
            return

        memory_off = (self.headers.get("X-Memory") or "").lower() in ("off", "0", "false")
        user_text = last_user_text(payload)
        if not memory_off:
            try:
                block = self.bridge.build_block(user_text)
                if block and inject_system(payload, block):
                    _log("注入 %d 字记忆" % len(block))
            except Exception as exc:
                _log("注入失败（已忽略，直通）: %s" % exc)

        target = self.upstream.rstrip("/") + "/chat/completions"
        if payload.get("stream"):
            self._proxy_stream(target, payload, user_text, memory_off)
        else:
            self._proxy_once(target, payload, user_text, memory_off)

    # ---- 一次性响应 ----

    def _proxy_once(self, target: str, payload: dict[str, Any],
                    user_text: str, memory_off: bool) -> None:
        req = urllib.request.Request(
            target,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._forward_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                status = resp.status
                ctype = resp.headers.get("Content-Type") or "application/json"
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type") or "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as exc:
            self._send_json(502, {"error": "上游不可达: %s" % exc})
            return

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

        if not memory_off:
            try:
                reply = assistant_text_of(json.loads(body.decode("utf-8")))
            except Exception:
                reply = ""
            self.bridge.write_turn(self._session_id(payload), user_text, reply)

    # ---- 流式响应 ----

    def _proxy_stream(self, target: str, payload: dict[str, Any],
                      user_text: str, memory_off: bool) -> None:
        req = urllib.request.Request(
            target,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._forward_headers(),
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as exc:
            self._send_json(502, {"error": "上游不可达: %s" % exc})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        chunks: list[str] = []
        try:
            with resp:
                for line in resp:
                    self.wfile.write(line)
                    self.wfile.flush()
                    text = line.decode("utf-8", "replace").strip()
                    if not text.startswith("data:"):
                        continue
                    data = text[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        piece = json.loads(data)
                        delta = piece["choices"][0].get("delta") or {}
                        content = delta.get("content")
                        if isinstance(content, str):
                            chunks.append(content)
                    except Exception:
                        continue
        except Exception as exc:
            _log("流式转发中断: %s" % exc)
        finally:
            try:
                self.wfile.write(b"\n\n")
                self.wfile.flush()
            except Exception:
                pass

        if not memory_off:
            self.bridge.write_turn(self._session_id(payload), user_text, "".join(chunks))

    def _forward_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        auth = self.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth          # 上游 key 原样透传，代理不存不管
        for extra in ("OpenAI-Beta", "X-Request-Id"):
            value = self.headers.get(extra)
            if value:
                headers[extra] = value
        return headers


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
    ap = argparse.ArgumentParser(description="记忆胶囊 OpenAI 兼容代理")
    ap.add_argument("--db", required=True, help="SQLite 路径")
    ap.add_argument("--upstream", required=True,
                    help="上游 OpenAI 兼容 baseURL，例如 https://api.deepseek.com/v1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--top-k", type=int, default=5, help="每轮注入几条记忆")
    ap.add_argument("--budget-tokens", type=int, default=800)
    ap.add_argument("--no-facts", action="store_true", help="不注入长期偏好/约束")
    ap.add_argument("--no-write-back", action="store_true", help="只注入，不回写对话")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--config", action="append", default=[], metavar="K=V")
    ap.add_argument("--version", action="version", version=PROXY_NAME + " " + PROXY_VERSION)
    args = ap.parse_args(argv)

    bridge = MemoryBridge(
        args.db,
        build_config(args.config),
        top_k=args.top_k,
        budget_tokens=args.budget_tokens,
        inject_facts=not args.no_facts,
        write_back=not args.no_write_back,
    )
    _ = bridge.db          # 启动即建库，配置错立刻死

    ProxyHandler.bridge = bridge
    ProxyHandler.upstream = args.upstream
    ProxyHandler.timeout = args.timeout

    httpd = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    _log("就绪 http://%s:%d/v1  ->  %s" % (args.host, args.port, args.upstream))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        bridge.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
