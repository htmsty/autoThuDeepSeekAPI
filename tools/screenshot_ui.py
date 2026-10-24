"""离屏渲染主界面与设置对话框并截图，用于人工检查版式。

用法：
    python tools/screenshot_ui.py                 # 主界面 → ui-screenshot.png
    python tools/screenshot_ui.py out.png         # 指定输出名
    python tools/screenshot_ui.py --settings      # 设置对话框 → ui-screenshot-settings.png
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from auto_thu_deepseek_api import paths, startup  # noqa: E402
from auto_thu_deepseek_api.logging_setup import close_file_handlers, setup_logging  # noqa: E402
from auto_thu_deepseek_api.settings import AppSettings  # noqa: E402
from auto_thu_deepseek_api.state_store import AppState  # noqa: E402
from auto_thu_deepseek_api.ui.main_window import MainWindow  # noqa: E402
from auto_thu_deepseek_api.ui.settings_dialog import SettingsDialog  # noqa: E402


def main() -> int:
    arguments = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    wants_settings = "--settings" in sys.argv
    default_name = "ui-screenshot-settings.png" if wants_settings else "ui-screenshot.png"
    output = Path(arguments[0]) if arguments else Path(default_name)
    with tempfile.TemporaryDirectory() as tmp:
        original_data_root = paths.data_root
        original_startup_enabled = startup.is_enabled
        paths.data_root = lambda: Path(tmp) / "private"  # type: ignore[assignment]
        # 截图不能因为开发机恰好开了开机启动就去改写注册表路径。
        startup.is_enabled = lambda: False  # type: ignore[assignment]
        try:
            paths.ensure_runtime_dirs()
            setup_logging()

            application = QApplication(sys.argv[:1])
            settings = AppSettings()
            state = AppState()
            window = MainWindow(settings, state)
            if wants_settings:
                dialog = SettingsDialog(settings, window)
                dialog.show()
                application.processEvents()
                dialog.grab().save(str(output))
                dialog.close()
            else:
                window.resize(900, 660)
                window.show()
                application.processEvents()
                window.grab().save(str(output))
            print(f"已截图：{output}")
            window.quit_app()
        finally:
            close_file_handlers()
            startup.is_enabled = original_startup_enabled  # type: ignore[assignment]
            paths.data_root = original_data_root  # type: ignore[assignment]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

