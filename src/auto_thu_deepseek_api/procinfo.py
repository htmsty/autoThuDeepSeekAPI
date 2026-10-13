"""查一下有哪些进程在跑。

只用来判断"某个程序到底装没装"这类问题：装了但还没生成配置文件时，进程名是一份有用的线索。
查一次缓存一次，避免启动时反复开进程。非 Windows 上直接返回空集合。
"""

from __future__ import annotations

import csv
import io
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE: set[str] | None = None


def running_process_names(*, refresh: bool = False) -> set[str]:
    """当前在跑的进程名（全小写，含 .exe）。查不到就返回空集合。"""

    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE

    names: set[str] = set()
    if sys.platform == "win32":
        try:
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for row in csv.reader(io.StringIO(completed.stdout or "")):
                if row and row[0].strip():
                    names.add(Path(row[0].strip()).name.lower())
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - 依赖环境
            logger.info("查询进程列表失败（按「没在跑」处理）：%s", exc)

    _CACHE = names
    return names


def any_running(*keywords: str) -> str | None:
    """进程名里只要包含任一关键词就算命中，返回命中的进程名。"""

    lowered = [word.lower() for word in keywords if word]
    for name in running_process_names():
        if any(word in name for word in lowered):
            return name
    return None


def reset_cache() -> None:
    """仅供测试。"""

    global _CACHE
    _CACHE = None
