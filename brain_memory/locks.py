"""锁实现。

两种锁对外接口一致（read/timeout + write/timeout 上下文管理器）：

- WritePriorityRWLock：WAL 模式下用，读者可并发、写者独占且有写优先，避免写饥饿。
- GlobalLock：DELETE 模式（或读连接池为 0）下用，读写共用一把锁，吞吐降级换绝对稳定。

两种锁都支持超时，超时抛 LockTimeoutError —— 有上限就不会变成真死锁。
若同一线程在持有读锁的情况下申请写锁，立刻抛 LockOrderError（自锁检测）。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator

from .errors import LockOrderError, LockTimeoutError


class _LockBase:
    name = "lock"

    def _timeout_error(self, timeout: float) -> LockTimeoutError:
        return LockTimeoutError(f"{self.name}: 等待超过 {timeout:.2f}s 仍未获得锁")

    @contextmanager
    def read(self, timeout: float = 5.0) -> Iterator[None]:
        raise NotImplementedError

    @contextmanager
    def write(self, timeout: float = 5.0) -> Iterator[None]:
        raise NotImplementedError


class GlobalLock(_LockBase):
    """读写共用一把锁。DELETE 模式下的唯一选择。"""

    name = "global"

    def __init__(self) -> None:
        # ⚠ 2026-10-29：原来用 threading.Lock，同线程嵌套读会自己等死（等满 timeout
        # 再抛 LockTimeoutError，看起来像"别人占着锁"）。改 RLock 允许读重入；
        # 写重入单独检测 —— 那会把外层事务回滚掉。
        self._lock = threading.RLock()
        self._write_owner: int | None = None

    @contextmanager
    def read(self, timeout: float = 5.0) -> Iterator[None]:
        acquired = self._lock.acquire(timeout=timeout)
        if not acquired:
            raise self._timeout_error(timeout)
        try:
            yield
        finally:
            self._lock.release()

    @contextmanager
    def write(self, timeout: float = 5.0) -> Iterator[None]:
        tid = threading.get_ident()
        if self._write_owner == tid:
            raise LockOrderError(
                "锁序错误：同一线程在写锁内又申请写锁（会让外层事务被回滚），已拒绝"
            )
        with self.read(timeout):
            self._write_owner = tid
            try:
                yield
            finally:
                self._write_owner = None


class WritePriorityRWLock(_LockBase):
    """写优先读写锁，纯 threading.Condition 实现，无第三方依赖。"""

    name = "rw"

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer_active = False
        self._writers_waiting = 0
        self._writer_tid: int | None = None
        self._reader_threads: dict[int, int] = {}

    # -- 内部 --

    def _wait(self, deadline: float, timeout: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._timeout_error(timeout)
        self._cond.wait(remaining)

    def _release_read(self, tid: int) -> None:
        with self._cond:
            self._readers -= 1
            held = self._reader_threads.get(tid, 0) - 1
            if held <= 0:
                self._reader_threads.pop(tid, None)
            else:
                self._reader_threads[tid] = held
            if self._readers == 0:
                self._cond.notify_all()

    # -- 对外 --

    @contextmanager
    def read(self, timeout: float = 5.0) -> Iterator[None]:
        tid = threading.get_ident()
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if self._reader_threads.get(tid, 0) > 0:
                    self._readers += 1
                    self._reader_threads[tid] += 1
                    break
                if not self._writer_active and self._writers_waiting == 0:
                    self._readers += 1
                    self._reader_threads[tid] = 1
                    break
                self._wait(deadline, timeout)
        try:
            yield
        finally:
            self._release_read(tid)

    @contextmanager
    def write(self, timeout: float = 5.0) -> Iterator[None]:
        tid = threading.get_ident()
        deadline = time.monotonic() + timeout
        with self._cond:
            if self._reader_threads.get(tid, 0) > 0:
                raise LockOrderError(
                    "锁序错误：当前线程持有读锁时又申请写锁（必然自锁），已拒绝"
                )
            if self._writer_tid == tid:
                raise LockOrderError(
                    "锁序错误：同一线程在写锁内又申请写锁（会让外层事务被回滚），已拒绝"
                )
            self._writers_waiting += 1
            try:
                while self._writer_active or self._readers > 0:
                    self._wait(deadline, timeout)
                self._writer_active = True
                self._writer_tid = tid
            finally:
                self._writers_waiting -= 1
                self._cond.notify_all()
        try:
            yield
        finally:
            with self._cond:
                self._writer_active = False
                self._writer_tid = None
                self._cond.notify_all()

    # -- 观测 --

    def snapshot(self) -> dict[str, int | bool]:
        with self._cond:
            return {
                "readers": self._readers,
                "writer_active": self._writer_active,
                "writers_waiting": self._writers_waiting,
            }
