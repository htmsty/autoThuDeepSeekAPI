"""Archive versioned build outputs and write their SHA256 manifest.

Usage:
    python tools/archive_release.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auto_thu_deepseek_api import APP_VERSION  # noqa: E402


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest().upper()


def main() -> int:
    dist = ROOT / "dist"
    folder = dist / "autoThuDeepSeekAPI"
    portable = dist / f"autoThuDeepSeekAPI-{APP_VERSION}-portable.zip"
    executable = dist / f"autoThuDeepSeekAPI-{APP_VERSION}.exe"
    required = (folder, portable, executable)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print("Missing build output:\n" + "\n".join(missing))
        return 1

    files = [path for path in folder.rglob("*") if path.is_file()]
    folder_bytes = sum(path.stat().st_size for path in files)
    zip_hash = sha256(portable)
    exe_hash = sha256(executable)
    commit = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    target = ROOT / "releases" / APP_VERSION
    target.mkdir(parents=True, exist_ok=True)
    archived_zip = target / portable.name
    archived_exe = target / executable.name
    shutil.copy2(portable, archived_zip)
    shutil.copy2(executable, archived_exe)

    manifest = (
        f"Auto THU DeepSeek API {APP_VERSION}\n"
        f"Commit: {commit}\n"
        f"Generated: {generated}\n\n"
        f"{zip_hash}  {portable.name}\n"
        f"{exe_hash}  {executable.name}\n"
    )
    (target / "SHA256.txt").write_text(manifest, encoding="utf-8", newline="\r\n")

    if sha256(archived_zip) != zip_hash or sha256(archived_exe) != exe_hash:
        print("Archive verification failed: copied file hash differs")
        return 1

    print(
        json.dumps(
            {
                "version": APP_VERSION,
                "commit": commit,
                "release_dir": str(target),
                "folder_files": len(files),
                "folder_bytes": folder_bytes,
                "folder_mib": round(folder_bytes / 1024 / 1024, 2),
                "zip_bytes": portable.stat().st_size,
                "zip_mib": round(portable.stat().st_size / 1024 / 1024, 2),
                "zip_sha256": zip_hash,
                "exe_bytes": executable.stat().st_size,
                "exe_mib": round(executable.stat().st_size / 1024 / 1024, 2),
                "exe_sha256": exe_hash,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
