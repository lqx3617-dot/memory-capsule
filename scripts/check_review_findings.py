#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并前自查：PR 上还有没有未 resolve 的 bot 审查意见。

    GITHUB_TOKEN=ghp_xxx python3 scripts/check_review_findings.py 5
    GITHUB_TOKEN=ghp_xxx python3 scripts/check_review_findings.py    # 查最近 3 个开着的 PR

CI 里的 review-gate 做同一件事（GraphQL reviewThreads）。这个脚本给本地用：
不想等 CI，或者想在 push 之前先看一眼。

退出码：0 全部已处理 / 1 有未处理 / 2 参数或环境问题。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPOSITORY") or "lqx3617-dot/memory-capsule"


def _call(path: str, token: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(
        API + path,
        data=data,
        headers={
            "Authorization": "token " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "brain-memory-review-check",
            "Content-Type": "application/json",
        },
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      title
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          comments(first: 1) {
            nodes { author { login __typename } path line body }
          }
        }
      }
    }
  }
}
"""


def pending_threads(number: int, token: str) -> tuple[str, list[dict]]:
    owner, repo = REPO.split("/", 1)
    out: list[dict] = []
    title = ""
    cursor = None
    for _page in range(20):          # 分页：超过 100 条 thread 时不能只看第一页
        data = _call("/graphql", token, {
            "query": QUERY,
            "variables": {"owner": owner, "repo": repo, "number": number, "cursor": cursor},
        })
        if "errors" in data:
            raise RuntimeError(str(data["errors"])[:300])
        pr = data["data"]["repository"]["pullRequest"]
        title = pr.get("title") or ""
        block = pr["reviewThreads"]
        for thread in block["nodes"]:
            if thread.get("isResolved"):
                continue
            first = (thread.get("comments") or {}).get("nodes") or []
            if not first:
                continue
            comment = first[0]
            author = comment.get("author") or {}
            login = author.get("login") or ""
            # GraphQL 里 bot 的 login 不带 [bot] 后缀（REST 才带），
            # 只判后缀会让脚本永远报「已处理」。认 __typename。
            if author.get("__typename") != "Bot" and not login.endswith("[bot]"):
                continue
            out.append(comment)
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return title, out



def main(argv: list[str]) -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("需要环境变量 GITHUB_TOKEN", file=sys.stderr)
        return 2
    try:
        if argv:
            numbers = [int(argv[0])]
        else:
            prs = _call("/repos/" + REPO + "/pulls?state=open&per_page=5", token)
            numbers = [int(p["number"]) for p in prs[:3]]
    except (urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
        print("取 PR 列表失败: %s" % exc, file=sys.stderr)
        return 2

    if not numbers:
        print("没有打开的 PR，无事可做")
        return 0

    bad = 0
    for number in numbers:
        title, items = pending_threads(number, token)
        print("\n#%d %s" % (number, title))
        if not items:
            print("  [OK]   没有未处理的 bot 意见")
            continue
        bad += 1
        for comment in items:
            head = (comment.get("body") or "").split("\n")[0]
            head = head.replace("**", "").replace("`", "")[:110]
            print("  [TODO] %s:%s — %s" % (
                comment.get("path") or "(文件级)",
                comment.get("line") if comment.get("line") is not None else "?",
                head,
            ))
    print("\n%s" % ("全部已处理" if not bad else "还有 %d 个 PR 有待处理意见" % bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
