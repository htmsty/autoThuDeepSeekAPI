"""日志：写 private/logs/，并通过 Qt 信号同步到界面。"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from . import constants, paths

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_FILE_RE = re.compile(r"^app-(\d{8})(?:-\d+)?\.log(?:\.\d+)?$")


class LogBus(QObject):
    """把日志行送到界面（跨线程安全：Qt 信号自动排队）。"""

    message = Signal(str)


log_bus = LogBus()

# 记下我们开过的文件 handler：换配置时摘下来的旧句柄也要能关掉，
# 否则 Windows 上那个日志文件会一直被占着（删不掉、也搬不走）。
_CREATED_FILE_HANDLERS: list[logging.Handler] = []


class QtLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            log_bus.message.emit(self.format(record))
        except Exception:  # pragma: no cover - 日志失败不应影响程序
            pass


def _open_file_handler(log_file: Path, formatter: logging.Formatter) -> logging.Handler | None:
    """开一个按大小轮转的文件 handler；打不开（被占用 / 只读）返回 None。"""

    try:
        handler = RotatingFileHandler(
            log_file,
            maxBytes=constants.LOG_MAX_BYTES,
            backupCount=constants.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        return None
    handler.setFormatter(formatter)
    return handler


def prune_old_logs(
    log_dir: Path,
    keep_days: int = constants.DEFAULT_LOG_KEEP_DAYS,
    *,
    active_path: Path | None = None,
) -> int:
    """删除超过保留天数的程序日志；只认 `app-YYYYMMDD.log*` 命名。

    用户放进日志目录的其它文件一律不动。删除走统一回收站入口，
    回收站不可用时保留原文件并记录警告。
    """

    try:
        keep = max(1, min(int(keep_days), 90))
    except (TypeError, ValueError):
        keep = constants.DEFAULT_LOG_KEEP_DAYS
    cutoff = date.today() - timedelta(days=keep - 1)
    try:
        candidates = list(log_dir.iterdir())
    except OSError:
        return 0

    active_key = str(active_path.resolve()).lower() if active_path is not None else ""
    removed = 0
    for path in candidates:
        try:
            if not path.is_file():
                continue
            if active_key and str(path.resolve()).lower() == active_key:
                continue
        except OSError:
            continue
        match = LOG_FILE_RE.fullmatch(path.name)
        if match is None:
            continue
        try:
            file_date = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if file_date >= cutoff:
            continue
        ok, detail = paths.delete_path(path)
        if ok:
            removed += 1
            logging.getLogger(__name__).info("已清理过期日志 %s（%s）", path.name, detail)
        else:
            logging.getLogger(__name__).warning("清理过期日志失败：%s", detail)
    return removed


def setup_logging(
    level: int = logging.INFO,
    *,
    keep_days: int = constants.DEFAULT_LOG_KEEP_DAYS,
) -> Path:
    """配置根日志。返回本次日志文件路径。"""

    log_dir = paths.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"app-{datetime.now():%Y%m%d}.log"

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        # 只是从 logger 上摘掉并不会关闭文件句柄；这里必须真的关掉
        _close_handler(handler)
        # 关掉的 handler 也从"我们开过的"清单里去掉，别让它一直挂在列表上
        if handler in _CREATED_FILE_HANDLERS:
            _CREATED_FILE_HANDLERS.remove(handler)

    formatter = logging.Formatter(LOG_FORMAT)

    # 日志写不进去不该拖垮程序（只读目录、共享目录断掉都可能发生）
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - 依赖具体环境
        pass
    # 按大小轮转：长期挂着也不会把日志写到天上去
    file_handler = _open_file_handler(log_file, formatter)
    if file_handler is None:
        # 主日志文件打不开（最常见的原因：另一个实例正开着它）时换一个带进程号的备用名。
        # 宁可文件名怪一点，也别留下一个 0 字节的日志让人无从排查。
        fallback = log_dir / f"{log_file.stem}-{os.getpid()}.log"
        file_handler = _open_file_handler(fallback, formatter)
        if file_handler is not None:
            log_file = fallback
    if file_handler is not None:
        root.addHandler(file_handler)
        _CREATED_FILE_HANDLERS.append(file_handler)
    removed = prune_old_logs(log_dir, keep_days, active_path=log_file)
    if removed:
        logging.getLogger(__name__).info("本次启动已清理 %d 个过期日志文件", removed)

    ui_handler = QtLogHandler()
    ui_handler.setFormatter(formatter)
    root.addHandler(ui_handler)

    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    logging.getLogger("auto_thu_deepseek_api").info("日志已启动：%s", log_file)
    return log_file


def close_file_handlers() -> int:
    """关掉所有文件日志句柄，返回关掉几个。

    换 private 位置 / 干净卸载之前必须先关：Windows 上日志文件被占着就删不掉、也搬不走。
    关掉之后日志只会走界面与 stderr，做完再调用一次 `setup_logging()` 就会在新位置重开。
    """

    root = logging.getLogger()
    targets: list[logging.Handler] = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    targets += [h for h in _CREATED_FILE_HANDLERS if h not in targets]

    closed = 0
    for handler in targets:
        if handler in root.handlers:
            root.removeHandler(handler)
        if _close_handler(handler):
            closed += 1
    _CREATED_FILE_HANDLERS.clear()
    return closed


def _close_handler(handler: logging.Handler) -> bool:
    """关掉一个 handler（关不掉也不算错）。"""

    try:
        handler.close()
        return True
    except Exception:  # pragma: no cover - 关不掉就算了，别影响主流程
        return False

