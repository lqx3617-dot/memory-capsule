#!/usr/bin/env python3
"""app_integration.py —— 把 brain_memory 嵌进「抖音自动回复系统」的完整示例。

三个场景，对应你现有的三条链路：

    场景 A  私信      检索该用户历史偏好 -> 注入 system prompt -> 记住新内容
    场景 B  评论      量大，关掉 escalation，只做极简事实抽取
    场景 C  空闲休眠  定时跑 dream() + forget() + maintenance()

直接运行：
    python3 app_integration.py

放进 App 时，把「模拟」那几处换成你真实的抖音接口调用即可。
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Iterable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain_memory as bm

log = logging.getLogger("douyin_agent")

# ---------------------------------------------------------------- 路径与常量

# App 私有目录：Android 上一般是 /data/data/<pkg>/files/.douyin_agent
# 这里用 ~/.douyin_agent 演示；设 DOUYIN_AGENT_HOME 可覆盖（测试用）
APP_HOME = os.environ.get("DOUYIN_AGENT_HOME") or os.path.join(
    os.path.expanduser("~"), ".douyin_agent"
)
DB_PATH = os.path.join(APP_HOME, "memory.db")

# 会被当成「用户偏好」注入提示词的事实类型
PREFERENCE_TYPES = ("preference", "constraint", "convention")

# 人设（你 .env 里的 BOT_PERSONA）
BOT_PERSONA = (
    "你是江白，性格像嘴很溜的年轻朋友：活泼、有梗、句子短。"
    "默认幽默可爱，会接梗、会自嘲。对方真生气或真求助时立刻收住玩笑。"
    "不承诺价格、不索要联系方式、不发外链、不提 AI。控制在五十字以内。"
)


# ---------------------------------------------------------------- 服务单例


class MemoryService:
    """全进程一个实例。启动建、退出关，中间不要反复开关。"""

    _instance: "MemoryService | None" = None
    _lock = threading.Lock()

    # ---- 生命周期 ----

    @classmethod
    def start(cls, db_path: str = DB_PATH) -> "MemoryService":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(db_path)
            return cls._instance

    @classmethod
    def get(cls) -> "MemoryService":
        if cls._instance is None:
            raise RuntimeError("MemoryService.start() 还没调用")
        return cls._instance

    @classmethod
    def shutdown(cls) -> None:
        with cls._lock:
            if cls._instance is not None:
                cls._instance.close()
                cls._instance = None

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

        self.db = bm.MemoryDB(
            db_path,
            config={
                # 私信场景允许下探（用户常问「上次说的那个」）
                "enable_escalation": True,
                "escalate_threshold": 0.35,
                "read_pool_size": 2,
                "access_flush_interval_s": 5.0,
                # 接了真 embedding 就把这块打开：
                # "embedding_callable": my_embed,
                # "vec_dim": 1536,
                # App 里建议 INFO，方便看降级日志；demo 用 WARNING 降噪
                "log_level": "WARNING",
            },
        )
        # 写操作丢后台，绝不阻塞回复
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mem-w")
        self._futures: list[Future] = []
        print("[memory] 数据库 = %s" % db_path)
        print("[memory] journal_mode=%s vector=%s lock=%s"
              % (self.db.journal_mode, self.db.vector_backend, self.db.sync_mode))

    def close(self) -> None:
        self._pool.shutdown(wait=True)
        self.db.close()

    # ---- 读写封装 ----

    def recall(self, query: str, **kw: Any) -> list[bm.Memory]:
        """语义检索。失败绝不影响主链路 —— 拿不到记忆就空着，回复照发。"""
        try:
            return self.db.recall(query, **kw)
        except Exception as exc:
            log.warning("recall 失败，按无记忆处理: %s", exc)
            return []

    def preferences(self, session_id: str, limit: int = 10) -> list[bm.Memory]:
        """按类型列举该用户的全部有效偏好。

        为什么不用 recall：用户问「今天心情不好」时，「别给我发语音」这几个字
        对不上任何关键词，语义检索召回不到 —— 但它必须每一轮都注入。
        所以偏好走 facts() 直接列，不做打分。
        """
        try:
            return self.db.facts(
                session_id=session_id,
                fact_types=PREFERENCE_TYPES,
                min_quality=0.3,
                limit=limit,
            )
        except Exception as exc:
            log.warning("preferences 查询失败: %s", exc)
            return []

    def remember(
        self, session_id: str, role: str, content: str, *, promote: bool = True
    ) -> Future:
        """异步写入。返回 Future 只为测试/退出时等待，业务侧不用管。"""
        future = self._pool.submit(self._safe_ingest, session_id, role, content, promote)
        self._futures.append(future)
        return future

    def _safe_ingest(self, session_id: str, role: str, content: str, promote: bool) -> Any:
        try:
            return self.db.ingest(session_id, role, content, promote=promote)
        except Exception as exc:
            log.warning("ingest 失败（不影响回复）: %s", exc)
            return None

    def flush(self, timeout: float = 10.0) -> None:
        """等所有后台写入落库。业务里一般不用调，退出前必须调。"""
        pending, self._futures = self._futures, []
        for future in pending:
            try:
                future.result(timeout=timeout)
            except Exception:
                pass

    # ---- 观测 ----

    def stats(self) -> dict[str, Any]:
        return self.db.stats().as_dict()


# ---------------------------------------------------------------- 提示词组装


def build_memory_block(
    memories: Iterable[bm.Memory], max_items: int = 5, max_chars: int = 600
) -> str:
    """把检索到的偏好类记忆拼成一段可直接塞进 system prompt 的文本。"""
    picks = [m for m in memories if m.fact_type in PREFERENCE_TYPES]
    if not picks:
        return ""
    lines = ["以下是这个用户的历史偏好与硬性要求，回复时必须遵守："]
    total = 0
    count = 0
    for mem in picks:
        line = "- " + mem.content.replace("\n", " ")
        if count >= max_items or total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
        count += 1
    if count == 0:
        return ""
    return "\n".join(lines)


def compose_system_prompt(persona: str, memory_block: str) -> str:
    """人设在前，记忆在后 —— 记忆是补充约束，不该盖住人设。"""
    if not memory_block:
        return persona
    return persona + "\n\n" + memory_block


# ---------------------------------------------------------------- 场景 A：私信


class DMHandler:
    """接收私信：先回忆，再回复，最后记住。"""

    def __init__(self, service: MemoryService, persona: str = BOT_PERSONA) -> None:
        self.svc = service
        self.persona = persona

    def on_message(self, uid: str, text: str) -> str:
        # 会话 ID 用 uid 而不是「uid + 单次对话」：
        # 这样同一个人的所有私信共享一份历史偏好
        session = "dm:" + uid

        # 1a) 偏好：按类型整批列出，不做语义检索（保证「别发语音」这类永远注入）
        preferences = self.svc.preferences(session)

        # 1b) 相关上下文：用当前这句话做语义检索，补充「上次聊到哪」这类记忆
        related = self.svc.recall(
            text,
            top_k=3,
            budget_tokens=400,
            min_quality=0.4,
            session_id=session,
            escalate=True,          # 私信场景允许下探 L1/L0
        )
        memories = preferences + related

        # 2) 拼 system prompt
        block = build_memory_block(memories)
        system = compose_system_prompt(self.persona, block)

        # 3) 调模型
        reply = self._call_llm(system, text)

        # 4) 记住用户这句（异步）。promote=True 会顺带做去重与冲突处理
        self.svc.remember(session, "user", text)
        # 自己的回复也记 —— 下轮可以判断「我之前是不是已经解释过了」
        self.svc.remember(session, "assistant", reply)
        return reply

    @staticmethod
    def _call_llm(system: str, user_text: str) -> str:
        """换成你现有的 LLM 调用。这里用假实现让示例能独立跑。"""
        return "[模拟回复] " + user_text.strip()[:24]


# ---------------------------------------------------------------- 场景 B：评论


class CommentHandler:
    """评论回复：量大，关掉 escalate，只做最轻的检索 + 事实抽取。"""

    # 评论要不要查历史？一般不需要 —— 设 False 就完全不查，只 ingest
    RECALL_ENABLED = True

    def __init__(self, service: MemoryService, persona: str = BOT_PERSONA) -> None:
        self.svc = service
        self.persona = persona

    def on_comment(self, aweme_id: str, uid: str, text: str) -> str:
        session = "comment:" + uid

        memories: list[bm.Memory] = []
        if self.RECALL_ENABLED:
            # 三条限制：escalate=False（不下探）、top_k=2（只要最相关的）、
            # min_quality=0.5（宁可空着也别塞噪声）
            memories = self.svc.recall(
                text,
                top_k=2,
                budget_tokens=200,
                min_quality=0.5,
                session_id=session,
                escalate=False,
            )

        block = build_memory_block(memories, max_items=2, max_chars=200)
        system = compose_system_prompt(self.persona, block)
        reply = self._call_llm(system, text)

        # 评论只记用户说了什么，不记我们的回复（回复量大，记了反而污染）
        self.svc.remember(session, "user", text)
        return reply

    @staticmethod
    def _call_llm(system: str, user_text: str) -> str:
        return "[模拟评论回复] " + user_text.strip()[:20]


# ---------------------------------------------------------------- 场景 C：空闲


class IdleWorker(threading.Thread):
    """空闲任务：巩固 + 遗忘 + 维护。

    Android 上不要常驻线程，用 WorkManager 挂「充电 + 灭屏 + 空闲」约束，
    系统会帮你调 run_once()。这里用线程只是为了让示例能独立跑。
    """

    def __init__(self, service: MemoryService, interval_s: float = 3600.0) -> None:
        super().__init__(daemon=True, name="idle-worker")
        self.svc = service
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._rounds = 0

    def run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.run_once()
            except Exception as exc:
                log.warning("空闲任务失败（下轮再试）: %s", exc)

    def run_once(self) -> dict[str, Any]:
        db = self.svc.db
        self.svc.flush(timeout=10.0)      # 先把排队中的写入落库

        # 1) 巩固：生成 L3 实体页 + DREAMS.md
        dream_text = db.dream()

        # 2) 遗忘：先 dry_run 看，再决定要不要真落
        #    生产建议：前一周只 dry_run 观察，确认没问题再打开 apply
        preview = db.forget(dry_run=True, limit=200)
        applied = db.forget(dry_run=False, limit=200) if preview else []

        # 3) 维护：平时 normal，一周一次 full
        level = "full" if self._rounds % 168 == 0 else "normal"
        maint = db.maintenance(level)
        self._rounds += 1

        result = {
            "dream_chars": len(dream_text),
            "forget_preview": len(preview),
            "forget_applied": len(applied),
            "maintenance": maint,
            "level": level,
        }
        log.info("空闲任务完成: %s", result)
        return result

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------- 演示


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    # 演示用临时目录，避免污染 ~/.douyin_agent
    demo_home = tempfile.mkdtemp(prefix="douyin_agent_")
    os.environ["DOUYIN_AGENT_HOME"] = demo_home
    db_path = os.path.join(demo_home, "memory.db")

    svc = MemoryService.start(db_path)
    dm = DMHandler(svc)
    comment = CommentHandler(svc)

    try:
        print("\n" + "=" * 66)
        print("场景 A：私信 —— 第一轮，用户提出偏好")
        print("=" * 66)
        r1 = dm.on_message("u1001", "别给我发语音，我不听语音，文字就行")
        print("  用户: 别给我发语音，我不听语音，文字就行")
        print("  回复:", r1)
        svc.flush()

        print("\n" + "=" * 66)
        print("场景 A：私信 —— 第二轮，偏好应该被自动注入")
        print("=" * 66)
        # 手工展示这一轮的 system prompt 长什么样
        mems = svc.preferences("dm:u1001") + svc.recall(
            "今天心情不好", top_k=3, min_quality=0.4, session_id="dm:u1001"
        )
        block = build_memory_block(mems)
        print("  召回 %d 条记忆：" % len(mems))
        for m in mems:
            print("    [%s q=%.2f] %s" % (m.fact_type, m.quality, m.content[:40]))
        print("\n  ---- 注入后的 system prompt ----")
        for line in compose_system_prompt(BOT_PERSONA, block).split("\n"):
            print("  | " + line)
        print("  -------------------------------")
        r2 = dm.on_message("u1001", "今天心情不好")
        print("\n  用户: 今天心情不好")
        print("  回复:", r2)
        svc.flush()

        print("\n" + "=" * 66)
        print("场景 B：评论 —— 关 escalation，极简召回")
        print("=" * 66)
        for text in ("哈哈哈这个视频笑死", "博主用的什么剪辑软件", "关注了，求更新"):
            reply = comment.on_comment("7699937944740394338", "c2001", text)
            print("  评论: %-18s -> %s" % (text, reply))
        svc.flush()
        st = svc.stats()
        print("\n  当前统计：L0=%d L1晋升=%d L2活跃=%d"
              % (st["counts"]["l0_events"], st["counts"]["l1_promoted"], st["counts"]["l2_active"]))

        print("\n" + "=" * 66)
        print("场景 C：空闲任务 —— dream + forget + maintenance")
        print("=" * 66)
        worker = IdleWorker(svc, interval_s=3600)
        result = worker.run_once()
        print("  dream 文本 %d 字" % result["dream_chars"])
        print("  forget 预览 %d 条 / 实际处理 %d 条"
              % (result["forget_preview"], result["forget_applied"]))
        print("  maintenance:", result["maintenance"])
        print("  DREAMS.md 是否生成:",
              os.path.exists(os.path.join(demo_home, "DREAMS.md")))

        st = svc.stats()
        print("\n  最终统计：", st["counts"])
        print("  L3 实体页：%d" % st["counts"]["l3_pages"])

    finally:
        svc.flush(timeout=5.0)
        MemoryService.shutdown()
        shutil.rmtree(demo_home, ignore_errors=True)
        print("\n已清理演示目录:", demo_home)
    return 0


if __name__ == "__main__":
    sys.exit(main())
