"""单实例锁。

单独一个模块，是为了"重启程序"和"干净卸载"能在合适的时机**主动让出锁**：
否则新进程起来时旧进程还没退干净，就会被判成"程序已经在运行"，第一次启动直接退出。
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QLockFile

from . import paths

logger = logging.getLogger(__name__)

_LOCK: QLockFile | None = None


def _lock_file() -> QLockFile:
    lock = QLockFile(str(paths.data_root() / "app.lock"))
    lock.setStaleLockTime(0)
    return lock


def acquire(timeout_ms: int = 100) -> bool:
    """取锁。成功返回 True；已经被别的实例占着返回 False。"""

    global _LOCK
    if _LOCK is not None:
        return True
    lock = _lock_file()
    if not lock.tryLock(timeout_ms):
        return False
    _LOCK = lock
    return True


def acquire_with_retry(seconds: float = 6.0, step_ms: int = 250) -> bool:
    """反复尝试取锁，给"旧进程正在退出"留点时间。

    典型的重启场景：旧进程点完重启马上拉起新进程，旧进程还要几十到几百毫秒才真正退干净。
    """

    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if acquire(timeout_ms=100):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.05, step_ms / 1000.0))


def release() -> None:
    """主动让出锁（重启/卸载前调用），让新进程马上能起来。"""

    global _LOCK
    if _LOCK is None:
        return
    try:
        _LOCK.unlock()
    except Exception as exc:  # pragma: no cover - 让不出也要继续
        logger.warning("让出单实例锁失败：%s", exc)
    _LOCK = None
