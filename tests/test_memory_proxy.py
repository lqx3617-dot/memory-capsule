#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆代理自测：假上游 + 真代理 + 真 SQLite。

验证三件事：
1. 第一轮没有记忆时，请求原样直通（不塞空 system）；
2. 有记忆后，system 里出现【相关记忆】/【长期偏好与约束】；
3. 每轮结束后 user / assistant 都写回了 L0；X-Memory: off 时两头都不做。
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY = os.path.join(HERE, "memory_proxy.py")
sys.path.insert(0, HERE)

import brain_memory as bm  # noqa: E402

FAILS = []
CAPTURED = []
REPLY = "好的，我记住了。"


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


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            CAPTURED.append(json.loads(raw.decode("utf-8")))
        except Exception:
            CAPTURED.append({})
        body = json.dumps({
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": REPLY}}],
            "usage": {"total_tokens": 10},
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def post(port, payload, headers=None):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_ready(port, proc):
    for _ in range(50):
        if proc.poll() is not None:
            raise RuntimeError("代理进程提前退出，code=%r" % proc.returncode)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def main() -> int:
    up_port = free_port()
    proxy_port = free_port()
    tmpdir = tempfile.mkdtemp(prefix="bm_proxy_")
    db_path = os.path.join(tmpdir, "mem.db")

    upstream = ThreadingHTTPServer(("127.0.0.1", up_port), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    proc = subprocess.Popen(
        [sys.executable, PROXY,
         "--db", db_path,
         "--upstream", "http://127.0.0.1:%d/v1" % up_port,
         "--port", str(proxy_port)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        check(wait_ready(proxy_port, proc), "代理 /health 就绪")

        print("1. 空库第一轮：直通，不塞空记忆")
        resp = post(proxy_port, {"model": "m", "messages": [
            {"role": "user", "content": "今天天气不错"}]})
        check(resp["choices"][0]["message"]["content"] == REPLY, "响应透传正常")
        msgs = CAPTURED[-1].get("messages") or []
        check(all(not (isinstance(m.get("content"), str) and "【相关记忆】" in m["content"])
                  for m in msgs), "空库时不注入记忆块")

        print("2. 写入记忆后第二轮：system 必须被注入")
        db = bm.MemoryDB(db_path)
        db.remember("s", "爸爸最喜欢的框架是 PySide6", fact_type="preference")
        db.remember("s", "这个项目所有配置必须走环境变量", fact_type="constraint")
        db.close()

        post(proxy_port, {"model": "m", "messages": [
            {"role": "user", "content": "我在选 GUI 框架，有什么建议？"}]})
        msgs = CAPTURED[-1].get("messages") or []
        system_text = "\n".join(
            m.get("content", "") for m in msgs if m.get("role") == "system"
        )
        check(msgs[0].get("role") == "system", "system 被插到最前面")
        check("长期偏好与约束" in system_text, "注入了长期偏好段")
        check("PySide6" in system_text, "偏好内容出现（PySide6）")

        print("3. 回写：user 与 assistant 都要进 L0")
        db = bm.MemoryDB(db_path)
        st = db.stats()
        l0 = st.counts.get("l0_events", 0)
        print("   L0 事件数 = %d" % l0)
        check(l0 >= 4, "两轮对话共写入 ≥4 条原始事件（实际 %d）" % l0)
        db.close()

        print("4b. 纯图片消息：跳过 recall，仍注入长期偏好")
        post(proxy_port, {"model": "m", "messages": [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]})
        msgs = CAPTURED[-1].get("messages") or []
        system_text = "\n".join(
            str(m.get("content")) for m in msgs if m.get("role") == "system"
        )
        check("长期偏好与约束" in system_text, "纯图片轮次仍然注入偏好（Codex 指出的边界）")

        print("4c. 注入预算：preference 与 recall 共用一份")
        tiny = free_port()
        proc3 = subprocess.Popen(
            [sys.executable, PROXY, "--db", db_path,
             "--upstream", "http://127.0.0.1:%d/v1" % up_port,
             "--port", str(tiny), "--budget-tokens", "5"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            check(wait_ready(tiny, proc3), "小预算代理就绪")
            post(tiny, {"model": "m", "messages": [
                {"role": "user", "content": "PySide6 和 Qt 怎么选"}]})
            msgs = CAPTURED[-1].get("messages") or []
            system_text = "\n".join(
                str(m.get("content")) for m in msgs if m.get("role") == "system"
            )
            # 固定提示语本身就超过 5 token 的额度 -> 应当整体放弃注入，
            # 而不是注入了提示语还声称「受预算约束」
            check(len(system_text) < 20,
                  "小预算下整体不注入（system 长度 %d）" % len(system_text))
        finally:
            proc3.terminate()
            try:
                proc3.wait(timeout=10)
            except Exception:
                proc3.kill()

        print("4. X-Memory: off 两头都不做")
        before = len(CAPTURED)
        post(proxy_port, {"model": "m", "messages": [
            {"role": "user", "content": "这条不该被记住：临时口令 ABC"}]},
            headers={"X-Memory": "off"})
        msgs = CAPTURED[-1].get("messages") or []
        check(not any("【相关记忆】" in str(m.get("content")) for m in msgs),
              "off 时不注入")
        db = bm.MemoryDB(db_path)
        rows = db.recall("临时口令 ABC", 5)
        check(not rows, "off 时不回写（检索不到该条）")
        db.close()

        print("5. 上游不可达时返回 502，不崩")
        bad_port = free_port()
        proc2 = subprocess.Popen(
            [sys.executable, PROXY, "--db", os.path.join(tmpdir, "b.db"),
             "--upstream", "http://127.0.0.1:%d/v1" % bad_port,
             "--port", str(bad_port + 1)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            check(wait_ready(bad_port + 1, proc2), "第二个代理就绪")
            try:
                post(bad_port + 1, {"model": "m", "messages": [
                    {"role": "user", "content": "hi"}]})
                check(False, "上游不可达时应报错")
            except urllib.error.HTTPError as exc:
                check(exc.code == 502, "上游不可达返回 502（实际 %d）" % exc.code)
        finally:
            proc2.terminate()
            proc2.wait(timeout=10)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        upstream.shutdown()

    print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
