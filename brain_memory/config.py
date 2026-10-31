"""配置对象 MemoryConfig。

优先级：显式 config > 环境变量 BRAIN_MEMORY_* > 内置默认值。
默认值一律保守：门控严格、遗忘温和、LLM 与向量默认关闭。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any, Mapping

from .errors import ConfigError

ENV_PREFIX = "BRAIN_MEMORY_"

_VALID_JOURNAL = {"WAL", "DELETE", "MEMORY"}
_VALID_SYNC = {"OFF", "NORMAL", "FULL", "EXTRA"}
_VALID_TEMP_STORE = {"MEMORY", "FILE", "DEFAULT"}
_VALID_LOG_LEVEL = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _to_bool(name: str, raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n", ""}:
        return False
    raise ConfigError(f"配置项 {name} 需要布尔值，收到 {raw!r}")


def _to_int(name: str, raw: Any) -> int:
    if isinstance(raw, bool):
        raise ConfigError(f"配置项 {name} 需要整数，收到布尔值")
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {name} 需要整数，收到 {raw!r}") from exc


def _to_float(name: str, raw: Any) -> float:
    if isinstance(raw, bool):
        raise ConfigError(f"配置项 {name} 需要浮点数，收到布尔值")
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {name} 需要浮点数，收到 {raw!r}") from exc


@dataclass(frozen=True)
class MemoryConfig:
    """全部可配置项的单一真源。"""

    # ---- 存储 / PRAGMA ----
    journal_mode_target: str = "WAL"      # WAL 失败会自动回退 DELETE
    synchronous: str = "NORMAL"
    busy_timeout_ms: int = 5000
    mmap_size: int = 0                    # PRoot 下 mmap 不稳定，强制 0
    temp_store: str = "MEMORY"
    cache_size_kb: int = -2000            # 负数=KB，正值=页数
    foreign_keys: bool = True
    wal_autocheckpoint: int = 1000
    read_pool_size: int = 2               # 读连接池大小；0 = 读也走写连接
    create_dirs: bool = True
    require_wal: bool = False             # True 时 WAL 失败抛 DatabaseError

    # ---- flusher ----
    access_flush_batch: int = 32
    access_flush_interval_s: float = 5.0

    # ---- 编码门控 ----
    enable_llm_gate: bool = False
    gate_min_chars: int = 4
    gate_threshold: float = 0.5
    llm_endpoint: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_s: float = 8.0
    llm_cooldown_s: float = 60.0

    # ---- 抽取专用模型（2026-10-28 主从分离）----
    # 门控判断这类「后台脏活」用便宜的快模型，主模型的额度全留给最终回复。
    # 留空则回落到上面的 llm_*，老配置不改也能跑。
    extract_llm_enabled: bool = True
    extract_llm_endpoint: str | None = None
    extract_llm_api_key: str | None = None
    extract_llm_model: str | None = None
    extract_llm_timeout_s: float = 6.0

    # ---- 晋升 ----
    enable_promote: bool = True
    promote_min_confidence: float = 0.55
    dedup_similarity: float = 0.9
    # 核心事实保护：同 fact_type 下与新事实相似度 ≥ 此值，就直接拒绝新事实。
    #
    # 为什么是 0.6 而不是 0.85：「爸爸的名字是李跃铭」→「爸爸的名字是王小明」
    # 的字符串相似度只有 0.667 —— 只换了一个值，公共部分占比不高。
    # 阈值定太高，最典型的「改名字」反而漏过去。
    # 代价是可能误挡一些「相似但不同」的事实；但核心事实是用户手动标的，
    # 误挡的损失（新事实进不来，看得见）比漏挡（身份被改掉，看不见）小。
    core_similarity: float = 0.6
    max_atom_chars: int = 2000

    # ---- 检索 ----
    w_bm25: float = 0.55
    w_quality: float = 0.35
    w_recency: float = 0.10
    recall_min_quality: float = 0.0
    top_k_max: int = 100
    chars_per_token: float = 1.7
    escalate_threshold: float = 0.35      # L2 最高分低于此值才下探
    enable_escalation: bool = False       # 默认关：开了会多查 L1/L0，变慢
    escalate_max_results: int = 3         # 下探时最多补几条
    escalate_l0_limit: int = 50           # L0 只看最近多少条（防全表扫）
    enable_like_fallback: bool = True   # FTS5 零命中时用 LIKE 兜底（只在 0 结果时触发）

    # ---- 向量（Phase 2）----
    enable_vector: bool = True               # 默认开：纯 Python 哈希向量随时可用
    vector_backend_policy: str = "auto"      # auto / sqlite-vec / numpy / pure / none
    embedding_backend: str = "hash"          # hash / http / callable / none
    embedding_endpoint: str | None = None    # OpenAI 兼容 /embeddings 端点
    embedding_api_key: str | None = None
    embedding_model: str | None = None
    embedding_timeout_s: float = 10.0
    embedding_callable: Any = None           # 自定义 fn(list[str]) -> list[list[float]]
    vec_dim: int = 384
    require_vector: bool = False
    enable_numpy_fallback: bool = True
    enable_pure_fallback: bool = True
    pure_fallback_max_atoms: int = 5000      # 单次暴力余弦最多扫描多少条（分块扫，不只看最老）
    vec_min_similarity: float = 0.10         # 向量召回的相似度下限，低于此值直接丢
    vec_fail_limit: int = 3                  # 连续失败几次就整体停用向量（防 API 挂了拖慢 ingest）
    rrf_k: int = 60                          # RRF 融合常数
    vec_candidate_multiplier: int = 4        # 向量召回候选数 = top_k * N

    # ---- 巩固（Phase 2）----
    dream_min_quality: float = 0.6       # 只巩固 quality 不低于此值的原子
    dream_cluster_similarity: float = 0.55
    dream_max_clusters: int = 20
    dreams_path: str | None = None       # 缺省写到 DB 同目录 DREAMS.md

    # ---- 遗忘 ----
    # 时间压缩（dt_eff）：真实年龄 → 等效天数，再代入衰减。
    # 目的：7 天内的近期记忆按真实衰减（该淡就淡），
    #      越过 7 天后把时间「踩住」，让人感觉还记得；越老踩得越狠。
    age_compress_knee1_d: float = 7.0      # 第一拐点：这之前不压缩
    age_compress_knee2_d: float = 30.0     # 第二拐点：这之后用更狠的系数
    age_compress_factor_mid: float = 0.5   # 7~30 天：超出部分按 50% 计
    age_compress_factor_far: float = 0.15  # 30 天以上：超出部分按 15% 计
    age_compress_cap_d: float = 90.0       # dt_eff 上限，防止极老记忆衰减到 0
    halflife_episodic_d: float = 7.0
    halflife_semantic_d: float = 90.0
    halflife_procedural_d: float = 365.0
    feedback_used: float = 1.0
    feedback_useful: float = 0.8
    feedback_noise: float = 0.0
    silent_quality: float = 0.15
    silent_access_max: int = 3
    archive_quality: float = 0.25
    archive_access_min: int = 3
    allow_hard_delete: bool = True

    # ---- 日志 ----
    log_level: str = "INFO"

    # ------------------------------------------------ 构造与校验

    @classmethod
    def resolve(cls, config: MemoryConfig | Mapping[str, Any] | None) -> MemoryConfig:
        """把 None / dict / MemoryConfig 归一成一个已校验的配置对象。"""
        if isinstance(config, cls):
            return config.validated()
        if config is not None and not isinstance(config, Mapping):
            raise ConfigError(
                f"config 需要 dict 或 MemoryConfig，收到 {type(config).__name__}"
            )

        explicit: dict[str, Any] = dict(config) if config else {}
        merged: dict[str, Any] = {}
        for name, default in _DEFAULTS.items():
            merged[name] = default
        merged.update(cls._from_env())
        merged.update({k: cls._coerce(k, v) for k, v in explicit.items()})
        return cls(**merged).validated()

    @classmethod
    def _coerce(cls, name: str, raw: Any) -> Any:
        if name not in _DEFAULTS:
            raise ConfigError(f"未知配置项: {name}")
        default = _DEFAULTS[name]
        if isinstance(default, bool):
            return _to_bool(name, raw)
        if isinstance(default, int):
            return _to_int(name, raw)
        if isinstance(default, float):
            return _to_float(name, raw)
        if name == "embedding_callable":
            return raw
        if raw is None:
            return None
        text = str(raw)
        return text if text != "" else (None if default is None else text)

    @classmethod
    def _from_env(cls) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for name in _DEFAULTS:
            env_key = ENV_PREFIX + name.upper()
            if env_key in os.environ:
                found[name] = cls._coerce(name, os.environ[env_key])
        return found

    def validated(self) -> MemoryConfig:
        """逐项校验取值范围；非法抛 ConfigError。"""
        journal = str(self.journal_mode_target).upper()
        if journal not in _VALID_JOURNAL:
            raise ConfigError(
                f"journal_mode_target 只能是 {sorted(_VALID_JOURNAL)}，收到 {self.journal_mode_target!r}"
            )
        sync = str(self.synchronous).upper()
        if sync not in _VALID_SYNC:
            raise ConfigError(f"synchronous 只能是 {sorted(_VALID_SYNC)}，收到 {self.synchronous!r}")
        temp_store = str(self.temp_store).upper()
        if temp_store not in _VALID_TEMP_STORE:
            raise ConfigError(f"temp_store 只能是 {sorted(_VALID_TEMP_STORE)}，收到 {self.temp_store!r}")
        log_level = str(self.log_level).upper()
        if log_level not in _VALID_LOG_LEVEL:
            raise ConfigError(f"log_level 只能是 {sorted(_VALID_LOG_LEVEL)}，收到 {self.log_level!r}")

        self._require(self.busy_timeout_ms >= 100 and self.busy_timeout_ms <= 60000,
                      "busy_timeout_ms 需在 100..60000")
        self._require(self.mmap_size >= 0, "mmap_size 不能为负")
        self._require(self.read_pool_size >= 0, "read_pool_size 不能为负")
        self._require(self.read_pool_size <= 3, "read_pool_size 上限 3（PRoot 内存敏感）")
        self._require(self.cache_size_kb != 0, "cache_size_kb 不能为 0")

        self._require(1 <= self.access_flush_batch <= 4096, "access_flush_batch 需在 1..4096")
        self._require(0.05 <= self.access_flush_interval_s <= 300,
                      "access_flush_interval_s 需在 0.05..300")

        self._require(self.gate_min_chars >= 0, "gate_min_chars 不能为负")
        self._require(0.0 <= self.gate_threshold <= 1.0, "gate_threshold 需在 0..1")
        self._require(self.llm_timeout_s > 0, "llm_timeout_s 需大于 0")
        self._require(self.llm_cooldown_s >= 0, "llm_cooldown_s 不能为负")
        self._require(self.extract_llm_timeout_s > 0, "extract_llm_timeout_s 需大于 0")

        self._require(0.0 <= self.promote_min_confidence <= 1.0,
                      "promote_min_confidence 需在 0..1")
        self._require(0.0 <= self.dedup_similarity <= 1.0, "dedup_similarity 需在 0..1")
        self._require(0.0 <= self.core_similarity <= 1.0, "core_similarity 需在 0..1")
        self._require(self.max_atom_chars >= 16, "max_atom_chars 至少 16")

        for name in ("w_bm25", "w_quality", "w_recency"):
            self._require(getattr(self, name) >= 0.0, f"{name} 不能为负")
        self._require((self.w_bm25 + self.w_quality + self.w_recency) > 0,
                      "三个打分权重不能同时为 0")
        self._require(0.0 <= self.recall_min_quality <= 1.0, "recall_min_quality 需在 0..1")
        self._require(self.top_k_max >= 1, "top_k_max 至少 1")
        self._require(self.chars_per_token > 0, "chars_per_token 需大于 0")
        self._require(0.0 <= self.escalate_threshold <= 1.0, "escalate_threshold 需在 0..1")
        self._require(self.escalate_max_results >= 1, "escalate_max_results 至少 1")
        self._require(self.escalate_l0_limit >= 1, "escalate_l0_limit 至少 1")

        self._require(self.vec_dim > 0, "vec_dim 需大于 0")
        self._require(self.pure_fallback_max_atoms >= 0, "pure_fallback_max_atoms 不能为负")
        if str(self.vector_backend_policy).lower() not in {"auto", "sqlite-vec", "numpy", "pure", "none"}:
            raise ConfigError("vector_backend_policy 只能是 auto/sqlite-vec/numpy/pure/none")
        if str(self.embedding_backend).lower() not in {"hash", "http", "callable", "none"}:
            raise ConfigError("embedding_backend 只能是 hash/http/callable/none")
        if self.embedding_callable is not None and not callable(self.embedding_callable):
            raise ConfigError("embedding_callable 必须是可调用对象或 None")
        self._require(0.0 <= self.vec_min_similarity <= 1.0, "vec_min_similarity 需在 0..1")
        self._require(self.vec_fail_limit >= 1, "vec_fail_limit 至少 1")
        self._require(self.rrf_k >= 1, "rrf_k 至少 1")
        self._require(self.vec_candidate_multiplier >= 1, "vec_candidate_multiplier 至少 1")
        self._require(self.embedding_timeout_s > 0, "embedding_timeout_s 需大于 0")

        self._require(self.age_compress_knee1_d >= 0, "age_compress_knee1_d 不能为负")
        self._require(self.age_compress_knee2_d > self.age_compress_knee1_d,
                      "age_compress_knee2_d 必须大于 knee1")
        self._require(0.0 <= self.age_compress_factor_mid <= 1.0,
                      "age_compress_factor_mid 需在 0..1")
        self._require(0.0 <= self.age_compress_factor_far <= 1.0,
                      "age_compress_factor_far 需在 0..1")
        self._require(self.age_compress_cap_d > 0, "age_compress_cap_d 需大于 0")
        for name in ("halflife_episodic_d", "halflife_semantic_d", "halflife_procedural_d"):
            self._require(getattr(self, name) > 0, f"{name} 需大于 0")
        for name in ("feedback_used", "feedback_useful", "feedback_noise"):
            self._require(0.0 <= getattr(self, name) <= 1.0, f"{name} 需在 0..1")
        self._require(0.0 <= self.dream_min_quality <= 1.0, "dream_min_quality 需在 0..1")
        self._require(0.0 <= self.dream_cluster_similarity <= 1.0, "dream_cluster_similarity 需在 0..1")
        self._require(self.dream_max_clusters >= 1, "dream_max_clusters 至少 1")
        self._require(self.silent_quality < self.archive_quality,
                      "silent_quality 必须小于 archive_quality")
        self._require(self.silent_access_max >= 0, "silent_access_max 不能为负")
        self._require(self.archive_access_min >= 0, "archive_access_min 不能为负")
        return self

    @staticmethod
    def _require(ok: bool, message: str) -> None:
        if not ok:
            raise ConfigError(message)

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


_DEFAULTS: dict[str, Any] = {f.name: f.default for f in fields(MemoryConfig)}
