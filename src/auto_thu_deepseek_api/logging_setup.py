"""日志：写 private/logs/，并通过 Qt 信号同步到界面。"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from . import paths

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class LogBus(QObject):
    """把日志行送到界面（跨线程安全：Qt 信号自动排队）。"""

    message = Signal(str)


log_bus = LogBus()


class QtLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            log_bus.message.emit(self.format(record))
        except Exception:  # pragma: no cover - 日志失败不应影响程序
            pass


def setup_logging(level: int = logging.INFO) -> Path:
    """配置根日志。返回本次日志文件路径。"""

    log_dir = paths.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"app-{datetime.now():%Y%m%d}.log"

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT)

    # 日志写不进去不该拖垮程序（只读目录、共享目录断掉都可能发生）
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # 按大小轮转：长期挂着也不会把日志写到天上去
        file_handler = RotatingFileHandler(
            log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:  # pragma: no cover - 依赖具体环境
        pass

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
    closed = 0
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            try:
                handler.close()
            except Exception:  # pragma: no cover - 关不掉也要把它摘掉
                pass
            root.removeHandler(handler)
            closed += 1
    return closed

