"""MemoryDB：连接、PRAGMA、降级判定、schema、连接池、锁、flusher、关闭时序。

设计要点（对应 PLAN.md §5 / §6）：
- 1 个写连接（writer，独占）+ 2 个读连接（pool，可配 1..3）+ 1 个 flusher 线程。
- WAL 模式：读写并行（写优先读写锁）；DELETE / :memory: / read_pool_size=0：全局互斥。
- 所有锁带 busy_timeout 超时，超时抛 LockTimeoutError，绝不无限等待。
- recall 热路径零写：访问计数进内存，flusher 每 N 秒或满批次批量刷盘。
- 降级（WAL→DELETE、vec→FTS5、LLM→规则）只记日志与可观测字段，不抛异常。
"""
from __future__ import annotations

import importlib.resources
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import MemoryConfig
from .errors import (
    ClosedError,
    ConfigError,
    DatabaseError,
    ExtensionError,
    SchemaVersionError,
)
from .dream import dream as dream_impl
from .forget import apply as forget_apply, scan as forget_scan, unlink as forget_unlink
from .gate import RuleGate
from .locks import GlobalLock, WritePriorityRWLock, _LockBase
from .promote import promote_candidate
from .recall import live_quality, search as recall_search
from .types import DEFAULT_ROLE, FACT_TYPES, ROLES, SIGNALS, Memory, Stats, utc_now
from .vec import VectorStore, resolve_embedder

logger = logging.getLogger("brain_memory")

SCHEMA_VERSION = 2
_FTS_TRIGRAM = "trigram"
_FTS_UNICODE = "unicode61"


def _configure_logging(level_name: str) -> None:
    """只在用户没有配置任何真正的 handler 时补一个 StreamHandler。

    目的：保证「初始化日志必须打印实际生效的 journal_mode」这条硬约束成立。
    """
    log = logging.getLogger("brain_memory")
    has_real = any(not isinstance(h, logging.NullHandler) for h in log.handlers)
    if not has_real:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        log.addHandler(handler)
    try:
        log.setLevel(getattr(logging, str(level_name).upper()))
    except AttributeError as exc:  # 理论上已被 config 校验拦住
        raise ConfigError(f"非法 log_level: {level_name!r}") from exc


def _normalize_path(p: str | os.PathLike[str]) -> str:
    if isinstance(p, os.PathLike):
        p = os.fspath(p)
    if not isinstance(p, str):
        raise ValueError(f"path 需要 str 或 os.PathLike，收到 {type(p).__name__}")
    if p == "":
        raise ValueError("path 不能为空字符串")
    if p == ":memory:":
        return p
    return os.path.abspath(os.path.expanduser(p))


class MemoryDB:
    """可嵌入的长期记忆库。线程安全，进程内单实例使用。"""

    # ------------------------------------------------------------ 生命周期

    def __init__(
        self,
        path: str | os.PathLike[str],
        config: MemoryConfig | dict[str, Any] | None = None,
    ) -> None:
        self._cfg = MemoryConfig.resolve(config)
        _configure_logging(self._cfg.log_level)

        self._path = _normalize_path(path)
        self._is_memory = self._path == ":memory:"

        self._writer: sqlite3.Connection | None = None
        self._pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue()
        self._sync: _LockBase | None = None
        self._sync_mode = "unknown"

        self._closed = False
        self._closing = False
        self._close_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state_cond = threading.Condition(self._state_lock)
        self._in_flight = 0

        self._access_lock = threading.Lock()
        self._access_delta: dict[int, int] = {}

        self._stop = threading.Event()
        self._flush_wakeup = threading.Event()
        self._flusher: threading.Thread | None = None

        self._journal_mode = "unknown"
        self._degraded_wal = False
        self._vector_backend = "none"
        self._vectors: VectorStore | None = None
        self._llm_mode = "off"
        self._fts_tokenizer = "none"

        self._gate = RuleGate(self._cfg)
        self._open()

    def _open(self) -> None:
        try:
            if not self._is_memory and self._cfg.create_dirs:
                parent = os.path.dirname(self._path)
                if parent:
                    os.makedirs(parent, exist_ok=True)

            self._writer = self._connect()
            self._resolve_journal_mode()
            self._build_sync()          # 必须早于 _ensure_schema：后者要用 _write_tx
            self._ensure_schema()
            if self._degraded_wal:
                # WAL 降级发生在建表之前，那时还没有 journal 表；建完补记一笔。
                try:
                    with self._write_tx() as conn:
                        self._journal(conn, "degrade_wal", detail={
                            "requested": self._cfg.journal_mode_target,
                            "actual": self._journal_mode,
                        })
                except Exception:
                    pass
            self._probe_vector()
            self._probe_llm()

            if self._cfg.read_pool_size > 0 and not self._is_memory:
                for _ in range(self._cfg.read_pool_size):
                    self._pool.put(self._connect())

            self._start_flusher()
            self._log_ready()
        except BaseException:
            self._abort_open()
            raise

    def _abort_open(self) -> None:
        self._stop.set()
        self._flush_wakeup.set()
        flusher, self._flusher = self._flusher, None
        if flusher is not None and flusher.is_alive():
            flusher.join(timeout=1.0)
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                break
            self._safe_close_conn(conn)
        if self._writer is not None:
            self._safe_close_conn(self._writer)
            self._writer = None
        self._closed = True

    # ------------------------------------------------------------ 连接与 PRAGMA

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self._path,
                timeout=self._cfg.busy_timeout_ms / 1000.0,
                isolation_level=None,          # 自动提交，事务由 _write_tx 显式控制
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"无法打开数据库: {self._path} ({exc})") from exc
        conn.row_factory = sqlite3.Row
        self._tune(conn)
        return conn

    def _tune(self, conn: sqlite3.Connection) -> None:
        pragmas = [
            ("busy_timeout", str(self._cfg.busy_timeout_ms)),
            ("synchronous", str(self._cfg.synchronous).upper()),
            ("mmap_size", str(self._cfg.mmap_size)),
            ("temp_store", str(self._cfg.temp_store).upper()),
            ("cache_size", str(self._cfg.cache_size_kb)),
            ("foreign_keys", "ON" if self._cfg.foreign_keys else "OFF"),
        ]
        for name, value in pragmas:
            try:
                conn.execute(f"PRAGMA {name}={value}")
            except sqlite3.Error as exc:
                logger.warning("记忆胶囊: PRAGMA %s=%s 设置失败: %s", name, value, exc)

    @staticmethod
    def _pragma_scalar(conn: sqlite3.Connection, stmt: str) -> str:
        row = conn.execute(f"PRAGMA {stmt}").fetchone()
        if row is None or row[0] is None:
            return ""
        return str(row[0]).strip().lower()

    def _resolve_journal_mode(self) -> None:
        """WAL 探测：只信读回值，不信「没报错」。失败回退 DELETE（规则 3）。"""
        assert self._writer is not None
        if self._is_memory:
            self._journal_mode = "memory"
            return

        target = str(self._cfg.journal_mode_target).upper()
        actual = ""
        try:
            actual = self._pragma_scalar(self._writer, f"journal_mode={target}")
        except sqlite3.Error as exc:
            logger.warning(
                "记忆胶囊: journal_mode=%s 设置异常（%s），准备回退 DELETE", target, exc
            )
            actual = ""

        if actual != target.lower():
            try:
                fallback = self._pragma_scalar(self._writer, "journal_mode=DELETE")
            except sqlite3.Error as exc:
                raise DatabaseError(
                    f"journal_mode 设置失败（requested={target}，回退 DELETE 也失败）: {exc}"
                ) from exc
            self._journal_mode = fallback or "delete"
            self._degraded_wal = True
            logger.warning(
                "记忆胶囊: journal_mode 降级 requested=%s actual=%s —— 并发模型切换为全局互斥",
                target,
                self._journal_mode,
            )
            if self._cfg.require_wal:
                raise DatabaseError(
                    f"require_wal=True 但 WAL 不可用（requested={target} actual={self._journal_mode}）"
                )
            return

        self._journal_mode = actual
        if actual == "wal":
            try:
                self._writer.execute(
                    f"PRAGMA wal_autocheckpoint={int(self._cfg.wal_autocheckpoint)}"
                )
            except sqlite3.Error as exc:
                logger.warning("记忆胶囊: wal_autocheckpoint 设置失败: %s", exc)

    def _build_sync(self) -> None:
        use_global = (
            self._journal_mode != "wal" or self._cfg.read_pool_size <= 0
        )
        self._sync = GlobalLock() if use_global else WritePriorityRWLock()
        self._sync_mode = "global" if use_global else "rw"

    # ------------------------------------------------------------ schema

    def _ensure_schema(self) -> None:
        assert self._writer is not None
        version = int(self._pragma_scalar(self._writer, "user_version") or 0)
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"库 user_version={version} 高于本库支持的 {SCHEMA_VERSION}，拒绝操作"
            )
        if version == 0:
            sql = self._read_schema_sql()
            try:
                self._writer.executescript(sql)
            except sqlite3.Error as exc:
                raise DatabaseError(f"建表失败: {exc}") from exc
            self._ensure_fts(self._writer)
            with self._write_tx() as conn:
                self._journal(
                    conn,
                    "schema_migrate",
                    detail={"from": 0, "to": SCHEMA_VERSION},
                )
            logger.info("记忆胶囊: schema 从 v0 迁移到 v%d", SCHEMA_VERSION)
            return
        if version < SCHEMA_VERSION:
            self._migrate(version)
        # 已是最新版本：仍要保证 FTS 表存在（幂等）
        self._ensure_fts(self._writer)

    def _migrate(self, from_version: int) -> None:
        """逐版本升级。当前只处理 v1 -> v2（atoms 增加 embedding）。"""
        assert self._writer is not None
        if from_version < 2:
            columns = {
                str(row[1]) for row in self._writer.execute("PRAGMA table_info(atoms)").fetchall()
            }
            if "embedding" not in columns:
                try:
                    self._writer.execute("ALTER TABLE atoms ADD COLUMN embedding BLOB")
                except sqlite3.Error as exc:
                    raise DatabaseError(f"迁移 v1->v2 失败: {exc}") from exc
            self._writer.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            with self._write_tx() as conn:
                self._journal(
                    conn,
                    "schema_migrate",
                    detail={"from": from_version, "to": SCHEMA_VERSION},
                )
            logger.info(
                "记忆胶囊: schema 从 v%d 迁移到 v%d（新增 atoms.embedding）",
                from_version,
                SCHEMA_VERSION,
            )

    @staticmethod
    def _read_schema_sql() -> str:
        try:
            return (
                importlib.resources.files(__package__)
                .joinpath("schema.sql")
                .read_text(encoding="utf-8")
            )
        except (FileNotFoundError, ModuleNotFoundError, AttributeError):
            local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
            with open(local, "r", encoding="utf-8") as fh:
                return fh.read()

    def _ensure_fts(self, conn: sqlite3.Connection) -> None:
        """建 atoms_fts：trigram 优先，不可用则 unicode61 + 查询侧 bigram 展开。"""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='atoms_fts'"
        ).fetchone()
        if row is not None and row[0]:
            ddl = str(row[0]).lower()
            self._fts_tokenizer = _FTS_TRIGRAM if "trigram" in ddl else _FTS_UNICODE
            return

        last_error: Exception | None = None
        for tokenizer, label in (
            (_FTS_TRIGRAM, _FTS_TRIGRAM),
            ("unicode61 remove_diacritics 2", _FTS_UNICODE),
        ):
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE atoms_fts USING fts5("
                    "content, content='atoms', content_rowid='id', "
                    f"tokenize='{tokenizer}')"
                )
            except sqlite3.Error as exc:
                last_error = exc
                logger.warning(
                    "记忆胶囊: FTS5 tokenizer=%s 不可用（%s），尝试下一个", tokenizer, exc
                )
                continue
            self._fts_tokenizer = label
            logger.info("记忆胶囊: atoms_fts 已建立 tokenizer=%s", label)
            try:  # 已有数据时补齐索引
                conn.execute("INSERT INTO atoms_fts(atoms_fts) VALUES('rebuild')")
            except sqlite3.Error:
                pass
            return

        raise DatabaseError(f"FTS5 完全不可用，无法建全文索引: {last_error}")

    # ------------------------------------------------------------ 向量 / LLM 探测

    def _probe_vector(self) -> None:
        """选向量后端：sqlite-vec -> numpy -> pure -> none，逐级降级不抛异常。"""
        assert self._writer is not None
        store = VectorStore(self._cfg, resolve_embedder(self._cfg))
        self._vectors = store
        try:
            backend = store.probe(self._writer)
        except Exception as exc:  # 双保险：探测本身出事也按 none 处理
            backend = "none"
            store.last_error = "探测异常: " + str(exc)
            logger.warning("记忆胶囊: 向量探测异常，按 none 处理: %s", exc)
        self._vector_backend = backend

        info = store.describe()
        if backend == "none":
            logger.warning(
                "记忆胶囊: 向量后端 = none（%s），检索走纯 FTS5/LIKE",
                info.get("last_error") or "未启用",
            )
            if self._cfg.require_vector:
                raise ExtensionError(
                    "require_vector=True 但向量后端不可用: " + str(info.get("last_error"))
                )
            try:
                with self._write_tx() as conn:
                    self._journal(conn, "degrade_vec", detail=info)
            except Exception:
                pass
            return

        logger.info(
            "记忆胶囊: 向量后端 = %s dim=%s embedder=%s",
            backend,
            info.get("dim"),
            info.get("embedder"),
        )
        try:
            with self._write_tx() as conn:
                self._journal(conn, "vector_ready", detail=info)
        except Exception:
            pass

    def _probe_llm(self) -> None:
        cfg = self._cfg
        if cfg.enable_llm_gate and cfg.llm_endpoint and cfg.llm_model:
            self._llm_mode = "on"
        else:
            self._llm_mode = "off"

    def mark_llm_degraded(self, reason: str = "") -> None:
        """T4 门控在 LLM 调用失败后调用；不抛异常。"""
        was = self._llm_mode
        self._llm_mode = "degraded"
        logger.warning("记忆胶囊: LLM 降级为规则门控（%s）", reason or "unknown")
        # 只在「从正常掉到降级」的那一次留痕，否则熔断期内每条消息都写一条 journal
        if was != "degraded":
            try:
                with self._write_tx() as conn:
                    self._journal(conn, "gate_llm_fallback",
                                  detail={"reason": reason or "unknown"})
            except Exception:
                pass

    def mark_llm_ok(self) -> None:
        if self._llm_mode == "degraded" and self._cfg.enable_llm_gate:
            self._llm_mode = "on"

    # ------------------------------------------------------------ 并发入口

    def _ensure_open(self) -> None:
        if self._closing or self._closed:
            raise ClosedError(f"MemoryDB 已关闭: {self._path}")

    def _begin_op(self, allow_closing: bool = False) -> None:
        with self._state_lock:
            if self._closed or (self._closing and not allow_closing):
                raise ClosedError(f"MemoryDB 已关闭: {self._path}")
            self._in_flight += 1

    def _end_op(self) -> None:
        with self._state_cond:
            self._in_flight -= 1
            if self._in_flight <= 0:
                self._in_flight = 0
                self._state_cond.notify_all()

    def _lock_timeout(self) -> float:
        return self._cfg.busy_timeout_ms / 1000.0

    @contextmanager
    def _write_tx(self, allow_closing: bool = False) -> Iterator[sqlite3.Connection]:
        """写事务：单写连接 + 写锁 + BEGIN IMMEDIATE / COMMIT / ROLLBACK。"""
        assert self._sync is not None and self._writer is not None
        self._begin_op(allow_closing=allow_closing)
        try:
            with self._sync.write(self._lock_timeout()):
                conn = self._writer
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.Error as exc:
                    raise DatabaseError(f"开启事务失败: {exc}") from exc
                try:
                    yield conn
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
                else:
                    try:
                        conn.execute("COMMIT")
                    except sqlite3.Error as exc:
                        try:
                            conn.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                        raise DatabaseError(f"提交事务失败: {exc}") from exc
        finally:
            self._end_op()

    @contextmanager
    def _read_conn(self) -> Iterator[sqlite3.Connection]:
        assert self._sync is not None
        self._begin_op()
        try:
            with self._sync.read(self._lock_timeout()):
                conn, temporary = self._acquire_reader()
                try:
                    yield conn
                finally:
                    self._release_reader(conn, temporary)
        finally:
            self._end_op()

    def _acquire_reader(self) -> tuple[sqlite3.Connection, bool]:
        try:
            return self._pool.get_nowait(), False
        except queue.Empty:
            if self._writer is None:
                raise ClosedError("writer 连接已关闭")
            if self._cfg.read_pool_size <= 0 or self._is_memory:
                return self._writer, False          # 由全局锁串行保护
            return self._connect(), True             # 临时连接，用完即关

    def _release_reader(self, conn: sqlite3.Connection, temporary: bool) -> None:
        if temporary:
            self._safe_close_conn(conn)
        else:
            try:
                self._pool.put_nowait(conn)
            except queue.Full:
                self._safe_close_conn(conn)

    @staticmethod
    def _safe_close_conn(conn: sqlite3.Connection) -> None:
        try:
            conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 访问计数 flusher

    def note_access(self, atom_id: int, times: int = 1) -> None:
        """recall 命中时记一次曝光。零写盘，只进内存。"""
        self._ensure_open()
        if times <= 0:
            return
        with self._access_lock:
            self._access_delta[atom_id] = self._access_delta.get(atom_id, 0) + times
            pending = len(self._access_delta)
        if pending >= self._cfg.access_flush_batch:
            self._flush_wakeup.set()

    def pending_access(self) -> int:
        with self._access_lock:
            return len(self._access_delta)

    def flush_access(self) -> int:
        self._ensure_open()
        return self._flush_access_internal()

    def _flush_access_internal(self, allow_closing: bool = False) -> int:
        with self._access_lock:
            if not self._access_delta:
                return 0
            delta = self._access_delta
            self._access_delta = {}
        now = utc_now()
        rows = [(int(times), now, int(atom_id)) for atom_id, times in delta.items()]
        try:
            with self._write_tx(allow_closing=allow_closing) as conn:
                conn.executemany(
                    "UPDATE atoms SET access_count = access_count + ?, "
                    "last_accessed_at = ? WHERE id = ?",
                    rows,
                )
        except Exception:
            with self._access_lock:  # 刷盘失败就把增量放回去，不丢语义
                for atom_id, times in delta.items():
                    self._access_delta[atom_id] = self._access_delta.get(atom_id, 0) + times
            raise
        return len(rows)

    def _start_flusher(self) -> None:
        interval = max(0.05, float(self._cfg.access_flush_interval_s))

        def loop() -> None:
            while not self._stop.is_set():
                self._flush_wakeup.wait(interval)
                self._flush_wakeup.clear()
                if self._stop.is_set():
                    break
                try:
                    self._flush_access_internal()
                except Exception as exc:
                    logger.warning("记忆胶囊: 访问计数刷盘失败（已回滚进内存）: %s", exc)

        self._flusher = threading.Thread(
            target=loop, name="brain_memory-flusher", daemon=True
        )
        self._flusher.start()

    # ------------------------------------------------------------ 日志 / journal

    def _log_ready(self) -> None:
        logger.info(
            "记忆胶囊: open path=%s journal_mode=%s sync=%s mmap=%s busy=%s "
            "sqlite=%s vec=%s llm=%s fts=%s pool=%d lock=%s",
            self._path,
            self._journal_mode,
            str(self._cfg.synchronous).upper(),
            self._cfg.mmap_size,
            self._cfg.busy_timeout_ms,
            sqlite3.sqlite_version,
            self._vector_backend,
            self._llm_mode,
            self._fts_tokenizer,
            self._cfg.read_pool_size if not self._is_memory else 0,
            self._sync_mode,
        )

    def _journal(
        self,
        conn: sqlite3.Connection,
        action: str,
        target_table: str | None = None,
        target_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO journal(action, target_table, target_id, detail_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                action,
                target_table,
                target_id,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                utc_now(),
            ),
        )

    # ------------------------------------------------------------ 业务 API（T4-T7）

    def ingest(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        meta: dict[str, Any] | None = None,
        created_at: str | None = None,
        promote: bool = True,
    ) -> int:
        """写入 L0 原始事件；门控通过则落 L1，promote=True 时再尝试晋升 L2。

        门控拒收**不抛异常**，raw_events 照常落盘并返回 event_id。
        """
        self._ensure_open()
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id 必须是非空字符串")
        if not isinstance(content, str):
            raise ValueError("content 必须是字符串")
        role_norm = role if role in ROLES else DEFAULT_ROLE

        meta_json = None
        if meta is not None:
            try:
                meta_json = json.dumps(meta, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("meta 必须可 JSON 序列化") from exc

        ts = created_at or utc_now()
        # 门控放在事务外：LLM 可能慢，绝不能占着写锁
        gate = self._gate.evaluate(content, role=role_norm, db=self)

        with self._write_tx() as conn:
            cur = conn.execute(
                "INSERT INTO raw_events(session_id, role, content, meta_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (session_id.strip(), role_norm, content, meta_json, ts),
            )
            event_id = int(cur.lastrowid)
            self._journal(
                conn,
                "ingest",
                "raw_events",
                event_id,
                {"role": role_norm, "accepted": gate.accepted, "gate": gate.reason[:80]},
            )
            if role_norm != role:
                self._journal(
                    conn, "role_unknown", "raw_events", event_id, {"raw_role": str(role)[:32]}
                )
            if not gate.accepted:
                self._journal(
                    conn,
                    "gate_reject",
                    "raw_events",
                    event_id,
                    {"reason": gate.reason, "confidence": gate.confidence},
                )
                return event_id

            conn.execute(
                "INSERT OR IGNORE INTO candidates(raw_event_id, fact_type, content, status, "
                "confidence, entity_hint, created_at) VALUES (?,?,?,'pending',?,?,?)",
                (
                    event_id,
                    gate.fact_type,
                    gate.content,
                    gate.confidence,
                    gate.entity_hint,
                    utc_now(),
                ),
            )
            row = conn.execute(
                "SELECT id FROM candidates WHERE raw_event_id = ? AND fact_type = ? AND content = ?",
                (event_id, gate.fact_type, gate.content),
            ).fetchone()
            if row is None:
                return event_id

            cand = {
                "id": int(row[0]),
                "raw_event_id": event_id,
                "fact_type": gate.fact_type,
                "content": gate.content,
                "confidence": gate.confidence,
                "entity_hint": gate.entity_hint,
            }
            if not (promote and self._cfg.enable_promote):
                return event_id

            status, atom_id = promote_candidate(conn, self._cfg, cand)
            if status == "promoted" and atom_id is not None and self._vectors is not None:
                # 同一事务内写向量：atoms.embedding + 可选 vec0
                self._vectors.index_atom(conn, atom_id, cand["content"])
            now = utc_now()
            if status == "rejected":
                conn.execute(
                    "UPDATE candidates SET status = 'rejected', resolved_at = ? WHERE id = ?",
                    (now, cand["id"]),
                )
            elif status == "merged":
                conn.execute(
                    "UPDATE candidates SET status = 'merged', resolved_at = ? WHERE id = ?",
                    (now, cand["id"]),
                )
                self._journal(conn, "merge", "atoms", atom_id, {"candidate_id": cand["id"]})
            else:
                conn.execute(
                    "UPDATE candidates SET status = 'promoted', resolved_at = ? WHERE id = ?",
                    (now, cand["id"]),
                )
                self._journal(conn, "promote", "atoms", atom_id, {"candidate_id": cand["id"]})
            return event_id

    def recall(
        self,
        query: str,
        top_k: int = 5,
        budget_tokens: int = 2000,
        *,
        min_quality: float | None = None,
        fact_types: list[str] | None = None,
        session_id: str | None = None,
        layer: str = "L2",
        escalate: bool | None = None,
    ) -> list[Memory]:
        """FTS5 + 向量召回 + RRF 融合 + 预算截断。全程零写。

        escalate：None=用配置；True/False 只覆盖本次调用。
        """
        self._ensure_open()
        return recall_search(
            self,
            query,
            top_k,
            budget_tokens,
            min_quality=min_quality,
            fact_types=fact_types,
            session_id=session_id,
            layer=layer,
            escalate=escalate,
        )

    def facts(
        self,
        *,
        session_id: str | None = None,
        fact_types: list[str] | tuple[str, ...] | None = None,
        min_quality: float = 0.0,
        limit: int = 50,
    ) -> list[Memory]:
        """按类型 / 会话**直接列举**活跃事实，不做语义检索。

        用途：把「这个用户的所有偏好与约束」整批注入 system prompt。
        这类需求不能靠关键词检索 —— 用户问「今天心情不好」时，
        「别给我发语音」不会因为字面相似被召回，但它必须被注入。
        """
        self._ensure_open()
        sql = (
            "SELECT a.id, a.content, a.source_event_id, a.confidence, a.quality, "
            "a.fact_type, a.entity_id, a.access_count, a.feedback, "
            "a.created_at, a.last_accessed_at "
            "FROM atoms a LEFT JOIN raw_events e ON e.id = a.source_event_id "
            "WHERE a.state = 'active'"
        )
        # min_quality 不放进 SQL：存储的 quality 是写入快照，现算值才代表「现在值多少」。
        params: list[Any] = []
        types = [t for t in (fact_types or []) if t]
        if types:
            sql += " AND a.fact_type IN (" + ",".join("?" * len(types)) + ")"
            params.extend(types)
        if session_id:
            sql += " AND e.session_id = ?"
            params.append(session_id)
        sql += " ORDER BY a.id ASC LIMIT ?"
        # 2026-10-27：id 从 DESC 改 ASC —— 同质量时取最早的，早期建的是身份信息，
        # 越往后越是口水话（「爱玩梗」「复读机」），不能把核心事实挤出预算。
        # ⚠ 2026-10-29：不能按 a.quality 排序预筛 —— 那是写入快照，会先砍掉
        # 「存储值偏低但现算偏高」的条目。排序与过滤都在下面 Python 侧按 live_quality 做。
        # 窗口给宽：facts 是「列举」不是「检索」，候选不够会导致过滤后空手而归。
        params.append(max(1000, max(1, int(limit)) * 20))

        out: list[Memory] = []
        try:
            with self._read_conn() as conn:
                rows = conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"facts 查询失败: {exc}") from exc
        ranked: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            ranked.append((live_quality(self._cfg, item), item))
        ranked.sort(key=lambda it: (-it[0], int(it[1]["id"])))

        floor = float(min_quality)
        cap = max(1, int(limit))
        for quality, row in ranked:
            if quality < floor:
                continue
            if len(out) >= cap:
                break
            out.append(
                Memory(
                    atom_id=int(row["id"]),
                    content=str(row["content"]),
                    source_event_id=row["source_event_id"],
                    confidence=float(row["confidence"] or 0.0),
                    quality=quality,
                    fact_type=str(row["fact_type"] or "fact"),
                    entity_id=row["entity_id"],
                    access_count=int(row["access_count"] or 0),
                    created_at=row["created_at"],
                    last_accessed_at=row["last_accessed_at"],
                    score=quality,
                    layer="L2",
                    record_id=int(row["id"]),
                )
            )
        return out

    def remember(
        self,
        session_id: str,
        content: str,
        *,
        fact_type: str = "fact",
        confidence: float = 0.8,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """应用层已确认的事实：**跳过门控**直接进 L1 -> L2。

        和 ingest 的区别：ingest 要靠门控判断「这句话值不值得记」，
        remember 是调用方已经知道这是事实（比如模型压缩出来的摘要条目
        「在杭州」「有只猫叫豆豆」），不该再被「太短/像闲聊」挡掉。
        raw_events 照常落盘，溯源链不断。
        """
        self._ensure_open()
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id 必须是非空字符串")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content 必须是非空字符串")
        if fact_type not in FACT_TYPES:
            fact_type = "fact"
        text = content.strip()
        ts = utc_now()
        meta_json = None
        if meta is not None:
            try:
                meta_json = json.dumps(meta, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("meta 必须可 JSON 序列化") from exc

        with self._write_tx() as conn:
            cur = conn.execute(
                "INSERT INTO raw_events(session_id, role, content, meta_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (session_id.strip(), "user", text, meta_json, ts),
            )
            event_id = int(cur.lastrowid)
            conn.execute(
                "INSERT OR IGNORE INTO candidates(raw_event_id, fact_type, content, status, "
                "confidence, created_at) VALUES (?,?,?,'pending',?,?)",
                (event_id, fact_type, text, confidence, ts),
            )
            row = conn.execute(
                "SELECT id FROM candidates WHERE raw_event_id = ? AND fact_type = ? AND content = ?",
                (event_id, fact_type, text),
            ).fetchone()
            if row is None:
                return event_id

            cand = {
                "id": int(row[0]),
                "raw_event_id": event_id,
                "fact_type": fact_type,
                "content": text,
                "confidence": confidence,
                "entity_hint": None,
            }
            status, atom_id = promote_candidate(conn, self._cfg, cand)
            now = utc_now()
            if status == "rejected":
                conn.execute(
                    "UPDATE candidates SET status = 'rejected', resolved_at = ? WHERE id = ?",
                    (now, cand["id"]),
                )
            elif status == "merged":
                conn.execute(
                    "UPDATE candidates SET status = 'merged', resolved_at = ? WHERE id = ?",
                    (now, cand["id"]),
                )
            else:
                conn.execute(
                    "UPDATE candidates SET status = 'promoted', resolved_at = ? WHERE id = ?",
                    (now, cand["id"]),
                )
                if atom_id is not None and self._vectors is not None:
                    self._vectors.index_atom(conn, atom_id, text)
                self._journal(conn, "promote", "atoms", atom_id, {"candidate_id": cand["id"], "via": "remember"})
            return event_id

    def forget(
        self,
        dry_run: bool = True,
        *,
        hard: bool = False,
        confirm: bool = False,
        limit: int | None = None,
        older_than_days: float | None = None,
    ) -> list[dict[str, Any]]:
        """默认只读：返回将被静默/归档的条目，库内一行不改。"""
        self._ensure_open()
        if dry_run and (hard or confirm):
            logger.warning("记忆胶囊: dry_run=True 时忽略 hard/confirm")
        decisions = forget_scan(self, limit=limit, older_than_days=older_than_days)
        if dry_run:
            return [d.as_dict() for d in decisions]
        if hard:
            if not confirm:
                raise ValueError("hard 删除必须同时传 confirm=True")
            forget_unlink(self, decisions, confirm=confirm)
        else:
            forget_apply(self, decisions)
        return [d.as_dict() for d in decisions]

    def reindex_vectors(self, limit: int | None = None) -> int:
        """按当前 embedder 重算活跃原子的向量，返回成功条数。

        换 embedding 模型（维度或语义变了）之后必须调一次 —— 否则新旧向量混在一起，
        余弦相似度没有可比性，检索结果会莫名其妙地偏。

        内部按批处理（每批 500 条一个事务），大库不会开一个超长写事务。
        limit 只作总量上限，None 表示全量重建。
        """
        self._ensure_open()
        store = self._vectors
        if store is None or store.backend == "none":
            return 0
        batch = 500
        done = 0
        last_id = 0
        while True:
            with self._write_tx() as conn:
                rows = conn.execute(
                    "SELECT id, content FROM atoms WHERE state = 'active' AND id > ? "
                    "ORDER BY id LIMIT ?",
                    (last_id, batch),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    if store.index_atom(conn, int(row[0]), str(row[1])):
                        done += 1
                last_id = int(rows[-1][0])
            if limit is not None and done >= int(limit):
                break
        if done:
            logger.info("记忆胶囊: 向量重建 %d 条（backend=%s）", done, store.backend)
        return done

    def dream(self, *, persist: bool = True, max_atoms: int | None = None) -> str:
        """执行一次巩固：聚类高质量原子 -> L3 实体页 + DREAMS.md，返回 Markdown 文本。"""
        self._ensure_open()
        return dream_impl(self, persist=persist, max_atoms=max_atoms)

    def feedback(self, atom_ids: int | list[int], signal: str) -> int:
        """回写反馈：used(1.0) / useful(0.8) / noise(0.0)。"""
        self._ensure_open()
        if signal not in SIGNALS:
            raise ValueError(f"signal 只能是 {list(SIGNALS)}，收到 {signal!r}")
        ids = [atom_ids] if isinstance(atom_ids, int) else list(atom_ids)
        if not ids:
            return 0
        value = {
            "used": self._cfg.feedback_used,
            "useful": self._cfg.feedback_useful,
            "noise": self._cfg.feedback_noise,
        }[signal]
        changed = 0
        with self._write_tx() as conn:
            for atom_id in ids:
                cur = conn.execute(
                    "UPDATE atoms SET feedback = ? WHERE id = ?", (value, int(atom_id))
                )
                changed += int(cur.rowcount)
        return changed

    # ------------------------------------------------------------ 兼容层（零开销）

    def init_db(self) -> "MemoryDB":
        """兼容旧写法：构造时已完成建库，这里只做存在性检查后返回 self。"""
        self._ensure_open()
        return self

    @property
    def conn(self) -> sqlite3.Connection:
        """仅调试：直接暴露写连接。

        业务代码禁止使用 —— 绕过 _write_tx 就等于绕过锁与事务保证。
        在 python -O 下访问会抛 AttributeError。
        """
        if not __debug__:
            raise AttributeError("conn 仅在调试模式可用（请勿用 python -O 运行）")
        self._ensure_open()
        assert self._writer is not None
        return self._writer

    # ------------------------------------------------------------ 观测与维护

    def stats(self) -> Stats:
        """各层计数 + 平均质量 + 降级状态快照。只读，不加写锁。"""
        self._ensure_open()
        counts: dict[str, int] = {
            "l0_events": 0,
            "l1_pending": 0,
            "l1_promoted": 0,
            "l1_rejected": 0,
            "l1_merged": 0,
            "l2_active": 0,
            "l2_silent": 0,
            "l2_archived": 0,
            "l2_superseded": 0,
            "l3_pages": 0,
            "sessions": 0,
        }
        avg_quality = 0.0
        avg_confidence = 0.0
        try:
            with self._read_conn() as conn:
                counts["l0_events"] = int(
                    conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
                )
                for row in conn.execute(
                    "SELECT status, COUNT(*) FROM candidates GROUP BY status"
                ):
                    key = f"l1_{row[0]}"
                    if key in counts:
                        counts[key] = int(row[1])
                for row in conn.execute(
                    "SELECT state, COUNT(*) FROM atoms GROUP BY state"
                ):
                    key = f"l2_{row[0]}"
                    if key in counts:
                        counts[key] = int(row[1])
                counts["l3_pages"] = int(
                    conn.execute("SELECT COUNT(*) FROM entity_pages").fetchone()[0]
                )
                counts["sessions"] = int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT session_id) FROM raw_events"
                    ).fetchone()[0]
                )
                row = conn.execute(
                    "SELECT AVG(quality), AVG(confidence) FROM atoms WHERE state = 'active'"
                ).fetchone()
                if row is not None:
                    avg_quality = float(row[0] or 0.0)
                    avg_confidence = float(row[1] or 0.0)
        except sqlite3.Error as exc:
            raise DatabaseError(f"stats 查询失败: {exc}") from exc

        db_bytes = 0
        if not self._is_memory:
            try:
                db_bytes = os.path.getsize(self._path)
            except OSError:
                db_bytes = 0

        return Stats(
            counts=counts,
            avg_quality=round(avg_quality, 6),
            avg_confidence=round(avg_confidence, 6),
            silent_count=counts["l2_silent"],
            archived_count=counts["l2_archived"],
            journal_mode=self._journal_mode,
            vector_backend=self._vector_backend,
            llm_mode=self._llm_mode,
            fts_tokenizer=self._fts_tokenizer,
            db_bytes=db_bytes,
            sqlite_version=sqlite3.sqlite_version,
            pending_access_flush=self.pending_access(),
            sync_mode=self._sync_mode,
            closed=self._closed,
        )

    def maintenance(self, level: str = "normal") -> dict[str, Any]:
        """light = optimize；normal = + wal_checkpoint(TRUNCATE)；full = + VACUUM。

        禁止在热路径调用。VACUUM 在 DELETE 模式下可能耗时数秒。
        """
        if level not in ("light", "normal", "full"):
            raise ValueError(f"level 只能是 light/normal/full，收到 {level!r}")
        self._ensure_open()
        assert self._sync is not None and self._writer is not None
        started = time.monotonic()
        result: dict[str, Any] = {
            "level": level,
            "journal_mode": self._journal_mode,
            "optimized": False,
            "checkpointed_pages": 0,
            "vacuumed": False,
            "duration_ms": 0,
        }
        self._begin_op()
        try:
            with self._sync.write(self._lock_timeout()):
                conn = self._writer
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    conn.execute("PRAGMA optimize")
                    result["optimized"] = True
                except sqlite3.Error as exc:
                    logger.warning("记忆胶囊: PRAGMA optimize 失败: %s", exc)

                if level in ("normal", "full") and self._journal_mode == "wal":
                    try:
                        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                        if row is not None and len(row) >= 2:
                            # PRAGMA wal_checkpoint 返回 (busy, log_frames, checkpointed_frames)
                            result["checkpointed_pages"] = int(row[2]) if len(row) > 2 else int(row[1])
                    except sqlite3.Error as exc:
                        logger.warning("记忆胶囊: wal_checkpoint 失败: %s", exc)

                if level == "full":
                    try:
                        conn.execute("VACUUM")
                        result["vacuumed"] = True
                    except sqlite3.Error as exc:
                        logger.warning("记忆胶囊: VACUUM 失败: %s", exc)
        finally:
            self._end_op()

        result["duration_ms"] = round((time.monotonic() - started) * 1000.0, 2)
        try:
            with self._write_tx() as conn:
                self._journal(conn, "maintenance", detail=result)
        except Exception:
            pass
        return result

    # ------------------------------------------------------------ 关闭

    def close(self, timeout: float = 5.0) -> None:
        """幂等。时序：置位 → 等在飞 → 停 flusher → flush → checkpoint → 关连接。"""
        if not self._close_lock.acquire(timeout=max(0.1, timeout)):
            logger.warning("记忆胶囊: 另一个线程正在关闭，本次 close 直接返回")
            return
        try:
            if self._closed:
                return
            with self._state_lock:
                self._closing = True

            deadline = time.monotonic() + max(0.0, timeout)
            with self._state_cond:
                while self._in_flight > 0:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning(
                            "记忆胶囊: close 等待在飞操作超时（in_flight=%d），继续关闭",
                            self._in_flight,
                        )
                        break
                    self._state_cond.wait(remaining)

            self._stop.set()
            self._flush_wakeup.set()
            if self._flusher is not None and self._flusher.is_alive():
                self._flusher.join(timeout=max(0.1, timeout))

            try:
                flushed = self._flush_access_internal(allow_closing=True)
                if flushed:
                    logger.info("记忆胶囊: close 前刷入 %d 条访问计数", flushed)
            except Exception as exc:
                logger.warning("记忆胶囊: close 时访问计数刷盘失败: %s", exc)

            if self._journal_mode == "wal" and self._writer is not None:
                try:
                    self._writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error as exc:
                    logger.warning("记忆胶囊: close 时 checkpoint 失败: %s", exc)

            while True:
                try:
                    conn = self._pool.get_nowait()
                except queue.Empty:
                    break
                self._safe_close_conn(conn)
            if self._writer is not None:
                self._safe_close_conn(self._writer)
                self._writer = None

            with self._state_lock:
                self._closed = True
            logger.info("记忆胶囊: closed path=%s", self._path)
        finally:
            self._close_lock.release()

    # ------------------------------------------------------------ 杂项

    def __enter__(self) -> "MemoryDB":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            if not getattr(self, "_closed", True):
                self.close(timeout=1.0)
        except Exception:
            pass

    def __repr__(self) -> str:
        return (
            f"<MemoryDB path={self._path!r} journal_mode={self._journal_mode} "
            f"vec={self._vector_backend} fts={self._fts_tokenizer} "
            f"lock={self._sync_mode} closed={self._closed}>"
        )

    # ---- 只读属性 ----

    @property
    def config(self) -> MemoryConfig:
        return self._cfg

    @property
    def path(self) -> str:
        return self._path

    @property
    def journal_mode(self) -> str:
        return self._journal_mode

    @property
    def vector_backend(self) -> str:
        return self._vector_backend

    @property
    def vector_store(self) -> VectorStore | None:
        """向量后端（探测结果与最近一次错误都在这里）。"""
        return self._vectors

    @property
    def llm_mode(self) -> str:
        return self._llm_mode

    @property
    def fts_tokenizer(self) -> str:
        return self._fts_tokenizer

    @property
    def sync_mode(self) -> str:
        return self._sync_mode

    @property
    def is_closed(self) -> bool:
        return self._closed
