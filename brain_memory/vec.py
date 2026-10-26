"""向量检索：embedding 接口 + 三级后端 + 纯 Python 兜底。

三级后端（按可用性自动选择）：

| backend    | 条件                       | 检索方式                     |
|------------|----------------------------|------------------------------|
| sqlite-vec | 扩展加载成功 + vec0 建表成功 | vec0 KNN（MATCH + distance） |
| numpy      | 装了 numpy                  | 矩阵点积暴力余弦             |
| pure       | 纯 stdlib                   | 逐条点积暴力余弦             |
| none       | enable_vector=False 或全挂   | 退化纯 FTS5                  |

关键约定：**任何一级失败都只降级，不抛异常**（除 require_vector=True）。
向量本身存在 atoms.embedding（BLOB, 小端 float32），vec0 只是可选加速索引，
所以后端切换不需要重建数据。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol, Sequence

import logging

from .config import MemoryConfig

logger = logging.getLogger("brain_memory")

_WORD_RE = re.compile(r"[a-z0-9_]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

VEC0_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS atoms_vec USING vec0("
    "atom_id INTEGER PRIMARY KEY, embedding FLOAT[{dim}])"
)


# ---------------------------------------------------------------- Embedding 接口


class Embedder(Protocol):
    """最小接口：给一批文本，还一批等长向量。"""

    dim: int
    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


def _l2_normalize(vec: list[float]) -> list[float]:
    total = math.sqrt(sum(x * x for x in vec))
    if total <= 1e-12:
        return vec
    inv = 1.0 / total
    return [x * inv for x in vec]


class HashEmbedder:
    """纯 Python 哈希向量（默认后端，零依赖、零网络）。

    做法：英文词 + 中文 2/3-gram，blake2b 哈希到固定维度，带符号累加，最后 L2 归一化。
    跨进程稳定（不用内置 hash，避免 PYTHONHASHSEED 影响）。

    能力边界要说清楚：它是**词形重叠**的高维投影，不是语义模型。
    对「换序、跨标点、部分字面重叠」比 FTS5 宽容，但「SQLite ↔ 本地数据库」
    这种真同义要靠 HttpEmbedder 或自备 callable。
    """

    name = "hash"

    def __init__(self, dim: int = 384) -> None:
        self.dim = max(8, int(dim))
        self.dim = int(self.dim)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(str(t)) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for gram, weight in self._grams(text):
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign * weight
        return _l2_normalize(vec)

    @staticmethod
    def _grams(text: str):
        lowered = text.lower()
        for word in _WORD_RE.findall(lowered):
            yield word, 1.0
        for seg in _CJK_RE.findall(text):
            for n, weight in ((2, 0.9), (3, 0.6)):
                if len(seg) < n:
                    continue
                for i in range(len(seg) - n + 1):
                    yield seg[i : i + n], weight


class CallableEmbedder:
    """把用户自己的函数包成 Embedder。

    fn 接受 list[str]，返回 list[list[float]]（或 numpy 二维数组）。
    """

    name = "callable"

    def __init__(self, fn: Callable[[list[str]], Any], dim: int = 0) -> None:
        self._fn = fn
        self.dim = int(dim)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raw = self._fn(list(texts))
        out: list[list[float]] = []
        for row in raw:
            vec = [float(x) for x in row]
            if self.dim == 0:
                self.dim = len(vec)
            out.append(_l2_normalize(vec))
        return out


class HttpEmbedder:
    """OpenAI 兼容 /embeddings 客户端。失败抛异常，由上层捕获降级。"""

    name = "http"

    def __init__(
        self,
        endpoint: str,
        api_key: str | None,
        model: str,
        dim: int = 0,
        timeout: float = 10.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.dim = int(dim)
        self.timeout = float(timeout)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint + "/embeddings",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        rows = payload.get("data") or []
        out: list[list[float]] = []
        for item in sorted(rows, key=lambda r: r.get("index", 0)):
            vec = [float(x) for x in item.get("embedding", [])]
            if self.dim == 0:
                self.dim = len(vec)
            out.append(_l2_normalize(vec))
        if len(out) != len(texts):
            raise ValueError("embedding 返回数量与输入不一致")
        return out


def resolve_embedder(cfg: MemoryConfig) -> Embedder | None:
    """按配置挑一个 Embedder；返回 None 表示不做向量。"""
    if not cfg.enable_vector:
        return None
    backend = str(cfg.embedding_backend or "hash").lower()
    if backend == "none":
        return None
    if backend == "http":
        if not cfg.embedding_endpoint or not cfg.embedding_model:
            return None
        return HttpEmbedder(
            cfg.embedding_endpoint,
            cfg.embedding_api_key,
            cfg.embedding_model,
            dim=cfg.vec_dim,
            timeout=cfg.embedding_timeout_s,
        )
    if callable(cfg.embedding_callable):
        return CallableEmbedder(cfg.embedding_callable, dim=cfg.vec_dim)
    return HashEmbedder(dim=cfg.vec_dim)


# ---------------------------------------------------------------- 序列化与相似度


def pack_vector(vec: Sequence[float]) -> bytes:
    return struct.pack("<%df" % len(vec), *vec)


def unpack_vector(blob: bytes | memoryview | None) -> list[float]:
    if not blob:
        return []
    data = bytes(blob)
    count = len(data) // 4
    if count == 0:
        return []
    return list(struct.unpack("<%df" % count, data[: count * 4]))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return dot / math.sqrt(na * nb)


# ---------------------------------------------------------------- 向量存储


class VectorStore:
    """负责：选后端、建索引、写向量、检索。"""

    def __init__(self, cfg: MemoryConfig, embedder: Embedder | None) -> None:
        self.cfg = cfg
        self.embedder = embedder
        self.backend = "none"
        self.dim = int(cfg.vec_dim)
        self._numpy: Any = None
        self._fail_count = 0
        self.last_error = ""

    # ---- 探测 ----

    def probe(self, conn: Any) -> str:
        cfg = self.cfg
        self.backend = "none"
        self.last_error = ""
        self._fail_count = 0

        if not cfg.enable_vector:
            self.last_error = "enable_vector=False"
            return self.backend
        if self.embedder is None:
            self.last_error = "embedder 未配置"
            return self.backend

        self.dim = int(getattr(self.embedder, "dim", 0) or cfg.vec_dim)

        policy = str(cfg.vector_backend_policy or "auto").lower()

        if policy in ("auto", "sqlite-vec"):
            if self._try_sqlite_vec(conn):
                self.backend = "sqlite-vec"
                return self.backend
            if policy == "sqlite-vec":
                self.last_error = self.last_error or "sqlite-vec 不可用"
                return self.backend

        if policy in ("auto", "numpy", "pure", "sqlite-vec"):
            if policy in ("auto", "numpy"):
                try:
                    import numpy as _np  # type: ignore

                    self._numpy = _np
                    self.backend = "numpy"
                    return self.backend
                except Exception:
                    self._numpy = None
            if cfg.enable_pure_fallback or policy == "pure":
                self.backend = "pure"
                return self.backend

        self.last_error = self.last_error or "没有可用后端"
        return self.backend

    def _try_sqlite_vec(self, conn: Any) -> bool:
        try:
            import sqlite_vec  # type: ignore
        except Exception as exc:
            self.last_error = "import sqlite_vec 失败: " + str(exc)
            return False
        try:
            if not hasattr(conn, "enable_load_extension"):
                self.last_error = "sqlite3 编译时禁用了扩展加载"
                return False
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.execute("SELECT vec_version()").fetchone()
        except Exception as exc:
            self.last_error = "加载 sqlite-vec 失败: " + str(exc)
            return False
        finally:
            try:
                conn.enable_load_extension(False)
            except Exception:
                pass
        try:
            conn.execute(VEC0_DDL.format(dim=self.dim))
        except Exception as exc:
            self.last_error = "vec0 建表失败: " + str(exc)
            return False
        return True

    # ---- 写入 ----

    def index_atom(self, conn: Any, atom_id: int, text: str) -> bool:
        """在调用方事务内写向量。任何失败都只返回 False，不抛。"""
        if self.backend == "none" or self.embedder is None:
            return False
        try:
            vec = self.embedder.embed([text])[0]
        except Exception as exc:
            self._note_failure("embed 失败: " + str(exc))
            return False
        if not vec:
            return False
        try:
            conn.execute(
                "UPDATE atoms SET embedding = ? WHERE id = ?",
                (pack_vector(vec), atom_id),
            )
        except Exception as exc:
            self.last_error = "写 atoms.embedding 失败: " + str(exc)
            return False
        if self.backend == "sqlite-vec":
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO atoms_vec(atom_id, embedding) VALUES (?, ?)",
                    (atom_id, json.dumps(vec)),
                )
            except Exception as exc:
                self.last_error = "写 vec0 失败: " + str(exc)
        return True

    # ---- 检索 ----

    def search(self, conn: Any, query: str, limit: int) -> list[tuple[int, float]]:
        """返回 [(atom_id, similarity)]，similarity 越大越像。"""
        if self.backend == "none" or self.embedder is None or limit <= 0:
            return []
        try:
            qvec = self.embedder.embed([query])[0]
        except Exception as exc:
            self._note_failure("query embed 失败: " + str(exc))
            return []
        if not qvec:
            return []
        if self.backend == "sqlite-vec":
            pairs = self._search_vec0(conn, qvec, limit)
        else:
            pairs = self._search_bruteforce(conn, qvec, limit)
        floor = float(self.cfg.vec_min_similarity)
        return [(atom_id, sim) for atom_id, sim in pairs if sim >= floor]

    def _search_vec0(self, conn: Any, qvec: list[float], limit: int):
        try:
            rows = conn.execute(
                "SELECT atom_id, distance FROM atoms_vec "
                "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (json.dumps(qvec), int(limit)),
            ).fetchall()
        except Exception as exc:
            self.last_error = "vec0 查询失败: " + str(exc)
            return []
        out = []
        for row in rows:
            distance = float(row[1])
            out.append((int(row[0]), 1.0 / (1.0 + max(0.0, distance))))
        return out

    def _search_bruteforce(self, conn: Any, qvec: list[float], limit: int):
        cap = int(self.cfg.pure_fallback_max_atoms)
        try:
            rows = conn.execute(
                "SELECT id, embedding FROM atoms "
                "WHERE embedding IS NOT NULL AND state = 'active' LIMIT ?",
                (cap,),
            ).fetchall()
        except Exception as exc:
            self.last_error = "读取 embedding 失败: " + str(exc)
            return []
        if not rows:
            return []

        scored: list[tuple[int, float]] = []
        if self._numpy is not None:
            try:
                np = self._numpy
                mat = np.zeros((len(rows), len(qvec)), dtype="float32")
                ids: list[int] = []
                for i, row in enumerate(rows):
                    vec = unpack_vector(row[1])
                    if len(vec) != len(qvec):
                        continue
                    mat[i] = np.asarray(vec, dtype="float32")
                    ids.append(int(row[0]))
                if not ids:
                    return []
                q = np.asarray(qvec, dtype="float32")
                sims = mat[: len(ids)] @ q
                order = sims.argsort()[::-1][: int(limit)]
                return [(ids[int(i)], float(sims[int(i)])) for i in order]
            except Exception as exc:
                self.last_error = "numpy 检索失败: " + str(exc)

        for row in rows:
            vec = unpack_vector(row[1])
            if len(vec) != len(qvec):
                continue
            scored.append((int(row[0]), cosine(qvec, vec)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[: int(limit)]

    def _note_failure(self, message: str) -> None:
        """连续失败到阈值就整体停用向量，避免每条消息都等一次超时。"""
        self._fail_count += 1
        self.last_error = message
        if self._fail_count >= self.cfg.vec_fail_limit:
            if self.backend != "none":
                logger.warning(
                    "brain_memory: 向量连续失败 %d 次，停用向量后端（%s），检索退回 FTS5",
                    self._fail_count,
                    message,
                )
            self.backend = "none"

    # ---- 观测 ----

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "dim": self.dim,
            "embedder": getattr(self.embedder, "name", "none"),
            "last_error": self.last_error,
        }
