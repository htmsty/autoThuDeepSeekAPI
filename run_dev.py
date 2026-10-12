"""源码方式启动（开机启动项在源码模式下指向这里）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from auto_thu_deepseek_api.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

