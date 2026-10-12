"""日志：写 private/logs/，并通过 Qt 信号同步到界面。"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
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

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    ui_handler = QtLogHandler()
    ui_handler.setFormatter(formatter)
    root.addHandler(ui_handler)

    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    logging.getLogger("auto_thu_deepseek_api").info("日志已启动：%s", log_file)
    return log_file

