#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 服务器协议自测：起子进程走 stdio，按 JSON-RPC 一问一答。

覆盖：initialize / notifications/initialized / tools/list / tools/call
（remember、ingest、recall、context、stats、forget、dream）/ 未知方法 / 未知工具 / 非法 JSON。
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(HERE, "mcp_server.py")

FAILS = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_http(port, proc, path="/health"):
    for _ in range(60):
        if proc.poll() is not None:
            raise RuntimeError("HTTP 服务提前退出，code=%r" % proc.returncode)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=1) as r:
                if r.status in (200, 405):
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code in (200, 405):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def post_json(port, payload, origin=None, path="/mcp"):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read()


class Client:
    def __init__(self, proc):
        self.proc = proc
        self.next_id = 0

    def send_raw(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("服务器没有响应（stdout 已关闭）")
        return json.loads(line)

    def call(self, method, params=None, notify=False):
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = self.next_id
        self.send_raw(json.dumps(payload, ensure_ascii=False))
        return None if notify else self.read()

    def tool(self, name, **kwargs):
        resp = self.call("tools/call", {"name": name, "arguments": kwargs})
        result = resp.get("result") or {}
        text = "".join(item.get("text", "") for item in (result.get("content") or []))
        return bool(result.get("isError")), text


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="bm_mcp_")
    db = os.path.join(tmpdir, "m.db")
    proc = subprocess.Popen(
        [sys.executable, SERVER, "--db", db],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", bufsize=1,
    )
    client = Client(proc)
    try:
        print("1. initialize")
        resp = client.call("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "selftest", "version": "1"},
        })
        res = resp.get("result") or {}
        check((res.get("serverInfo") or {}).get("name") == "brain-memory",
              "serverInfo.name = brain-memory")
        check(res.get("protocolVersion") in ("2025-06-18", "2025-03-26", "2024-11-05"),
              "protocolVersion 合法（%r）" % res.get("protocolVersion"))
        check("tools" in (res.get("capabilities") or {}), "声明 tools 能力")

        client.call("notifications/initialized", notify=True)

        print("2. tools/list")
        tools = (client.call("tools/list", {}).get("result") or {}).get("tools") or []
        names = [t.get("name") for t in tools]
        print("   " + ", ".join(names))
        check(len(tools) == 7, "7 个工具（实际 %d）" % len(tools))
        for want in ("memory_ingest", "memory_remember", "memory_recall",
                     "memory_context", "memory_forget", "memory_dream", "memory_stats"):
            check(want in names, "含 " + want)
        check(all((t.get("inputSchema") or {}).get("type") == "object" for t in tools),
              "每个工具都有 object 型 inputSchema")

        print("3. tools/call memory_remember")
        err, text = client.tool("memory_remember", content="爸爸最喜欢的框架是 PySide6",
                                fact_type="preference")
        check(not err, "写入成功: " + text)

        print("4. tools/call memory_ingest")
        err, text = client.tool("memory_ingest",
                                content="这个项目所有配置必须走环境变量，不能硬编码")
        check(not err, "ingest 成功: " + text)

        print("5. tools/call memory_recall")
        err, text = client.tool("memory_recall", query="最喜欢的框架")
        check(not err and "PySide6" in text,
              "召回命中 PySide6: " + text.replace("\n", " | ")[:120])

        print("6. tools/call memory_context")
        err, text = client.tool("memory_context")
        check(not err and "PySide6" in text, "偏好列表含 PySide6")

        print("7. tools/call memory_stats")
        err, text = client.tool("memory_stats")
        check(not err and "L2" in text, "stats 可读: " + text.replace("\n", " | ")[:100])

        print("8. tools/call memory_forget（dry-run）")
        err, text = client.tool("memory_forget")
        check(not err, "forget 可调: " + text.replace("\n", " | ")[:100])

        print("9. tools/call memory_dream")
        err, text = client.tool("memory_dream", persist=True)
        check(not err and text.startswith("# DREAMS"), "dream 返回 Markdown")

        print("10. 错误路径")
        resp = client.call("tools/call", {"name": "no_such_tool", "arguments": {}})
        check((resp.get("result") or {}).get("isError") is True, "未知工具返回 isError")
        resp = client.call("no/such/method", {})
        check((resp.get("error") or {}).get("code") == -32601, "未知方法返回 -32601")
        client.send_raw("{ this is not json")
        check((client.read().get("error") or {}).get("code") == -32700, "非法 JSON 返回 -32700")

        print("11. 空参数")
        err, text = client.tool("memory_ingest", content="")
        check(err, "空 content 被拒: " + text)

        print("12. HTTP 模式：Origin 校验 / GET 405 / 通知 202")
        http_port = free_port()
        proc2 = subprocess.Popen(
            [sys.executable, SERVER, "--db", os.path.join(tmpdir, "h.db"),
             "--http", str(http_port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            check(wait_http(http_port, proc2), "HTTP 服务就绪")
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}}
            status, body = post_json(http_port, init)
            check(status == 200, "无 Origin 放行（%d）" % status)
            check(json.loads(body.decode("utf-8")).get("result", {})
                  .get("serverInfo", {}).get("name") == "brain-memory", "initialize 正常回包")
            try:
                post_json(http_port, init, origin="http://evil.example")
                check(False, "恶意 Origin 应被拒")
            except urllib.error.HTTPError as exc:
                check(exc.code == 403, "恶意 Origin 返回 403（实际 %d）" % exc.code)
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/mcp" % http_port, timeout=5)
                check(False, "GET /mcp 应 405")
            except urllib.error.HTTPError as exc:
                check(exc.code == 405, "GET /mcp 返回 405（实际 %d）" % exc.code)
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % http_port, timeout=5) as r:
                check(r.status == 200, "GET /health 仍 200")
            status, _ = post_json(http_port, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            check(status == 202, "通知返回 202（实际 %d）" % status)
        finally:
            proc2.terminate()
            try:
                proc2.wait(timeout=10)
            except Exception:
                proc2.kill()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        code = proc.wait(timeout=20)
        check(code == 0, "stdin 关闭后进程正常退出（code=%r）" % code)

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
