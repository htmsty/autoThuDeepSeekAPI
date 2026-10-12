"""离屏渲染主界面并截图，用于人工检查版式。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from auto_thu_deepseek_api import paths  # noqa: E402
from auto_thu_deepseek_api.logging_setup import setup_logging  # noqa: E402
from auto_thu_deepseek_api.settings import AppSettings  # noqa: E402
from auto_thu_deepseek_api.state_store import AppState  # noqa: E402
from auto_thu_deepseek_api.ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ui-screenshot.png")
    paths.ensure_runtime_dirs()
    setup_logging()

    application = QApplication(sys.argv[:1])
    settings = AppSettings()
    state = AppState()
    window = MainWindow(settings, state)
    window.resize(900, 660)
    window.show()
    application.processEvents()
    window.grab().save(str(output))
    print(f"已截图：{output}")
    window.quit_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

