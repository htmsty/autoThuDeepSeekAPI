"""Create, verify, and publish a GitHub release for the current version.

Authentication uses GH_TOKEN, GITHUB_TOKEN, or the token returned by
``git credential fill`` for github.com. Assets are taken from
``releases/<version>/`` and uploaded while the release is still a draft.

Usage:
    python tools/publish_github_release.py --check
    python tools/publish_github_release.py
    python tools/publish_github_release.py --publish
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auto_thu_deepseek_api import APP_VERSION  # noqa: E402

API_ROOT = "https://api.github.com"
UPLOAD_HOST = "uploads.github.com"
API_VERSION = "2022-11-28"


class ReleaseError(RuntimeError):
    pass


def _run_git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def _repository() -> str:
    remote = _run_git("remote", "get-url", "origin")
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", remote)
    if not match:
        raise ReleaseError(f"Cannot derive GitHub repository from origin: {remote}")
    return f"{match.group(1)}/{match.group(2)}"


def _github_token() -> str:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            cwd=ROOT,
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(f"Git credential lookup failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise ReleaseError(f"No GitHub API token available: {detail}")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    token = fields.get("password", "").strip()
    if not token:
        raise ReleaseError(
            "No GitHub API token available. Set GH_TOKEN or configure a git credential."
        )
    return token


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "autoThuDeepSeekAPI-release-tool",
    }


def _api_json(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    *,
    allow_not_found: bool = False,
) -> dict | list | None:
    data = None
    headers = _headers(token)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=90) as response:
            raw = response.read()
    except HTTPError as exc:
        if exc.code == 404 and allow_not_found:
            return None
        detail = exc.read().decode("utf-8", "replace")
        raise ReleaseError(f"GitHub API {method} {url} failed ({exc.code}): {detail}") from exc
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _release_body(version: str) -> str:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(changelog)
    if not match:
        raise ReleaseError(f"CHANGELOG.md has no section for {version}")
    details = match.group(1).strip()
    return f"## Auto THU DeepSeek API {version}\n\n{details}\n\n" + (
        "ZIP 必须完整解压，保留 `autoThuDeepSeekAPI.exe` 与 `_internal\\` 的目录结构。"
    )


def _release_url(repo: str, tag: str) -> str:
    return f"{API_ROOT}/repos/{repo}/releases/tags/{quote(tag, safe='')}"


def _get_release(repo: str, tag: str, token: str) -> dict | None:
    release = _api_json("GET", _release_url(repo, tag), token, allow_not_found=True)
    if isinstance(release, dict):
        return release

    # The tag endpoint does not reliably return draft releases. Newly created
    # drafts are still available to an authenticated caller through the list.
    releases = _api_json(
        "GET",
        f"{API_ROOT}/repos/{repo}/releases?per_page=100",
        token,
    )
    if isinstance(releases, list):
        for item in releases:
            if isinstance(item, dict) and item.get("tag_name") == tag:
                return item
    return None


def _delete_asset(repo: str, asset_id: int, token: str) -> None:
    _api_json("DELETE", f"{API_ROOT}/repos/{repo}/releases/assets/{asset_id}", token)


def _upload_asset(repo: str, release_id: int, path: Path, token: str) -> dict:
    query = urlencode({"name": path.name})
    target = f"/repos/{repo}/releases/{release_id}/assets?{query}"
    content_type = "application/zip" if path.suffix.lower() == ".zip" else (
        "application/vnd.microsoft.portable-executable"
    )
    connection = http.client.HTTPSConnection(UPLOAD_HOST, timeout=900)
    try:
        connection.putrequest("POST", target)
        headers = _headers(token)
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(path.stat().st_size)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                connection.send(chunk)
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    if response.status not in (200, 201):
        raise ReleaseError(
            f"Upload {path.name} failed ({response.status}): "
            f"{raw.decode('utf-8', 'replace')}"
        )
    return json.loads(raw.decode("utf-8"))


def _verify_assets(release: dict, local_assets: list[Path]) -> None:
    remote = {
        str(asset.get("name")): asset
        for asset in release.get("assets", [])
        if isinstance(asset, dict)
    }
    problems: list[str] = []
    for path in local_assets:
        asset = remote.get(path.name)
        if asset is None:
            problems.append(f"{path.name}: missing")
            continue
        if int(asset.get("size") or -1) != path.stat().st_size:
            problems.append(
                f"{path.name}: size {asset.get('size')} != {path.stat().st_size}"
            )
        expected_digest = f"sha256:{_sha256(path)}"
        actual_digest = str(asset.get("digest") or "")
        if actual_digest and actual_digest != expected_digest:
            problems.append(
                f"{path.name}: digest {actual_digest} != {expected_digest}"
            )
        if asset.get("state") != "uploaded":
            problems.append(f"{path.name}: state is {asset.get('state')}")
    if problems:
        raise ReleaseError("Release asset verification failed:\n" + "\n".join(problems))


def _local_assets(version: str) -> list[Path]:
    directory = ROOT / "releases" / version
    assets = [
        directory / f"autoThuDeepSeekAPI-{version}-portable.zip",
        directory / f"autoThuDeepSeekAPI-{version}.exe",
    ]
    missing = [str(path) for path in assets if not path.is_file()]
    if missing:
        raise ReleaseError("Missing archived assets:\n" + "\n".join(missing))
    return assets


def _wait_for_uploaded(repo: str, tag: str, token: str, assets: list[Path]) -> dict:
    current: dict | None = None
    for attempt in range(6):
        current = _get_release(repo, tag, token)
        if current is None:
            raise ReleaseError("Draft release disappeared while verifying assets")
        try:
            _verify_assets(current, assets)
            return current
        except ReleaseError:
            if attempt == 5:
                raise
            time.sleep(2)
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check auth/release state only")
    parser.add_argument("--publish", action="store_true", help="publish a verified draft")
    args = parser.parse_args()

    version = APP_VERSION
    tag = f"v{version}"
    repo = _repository()
    token = _github_token()
    assets = _local_assets(version)
    print(f"Repository: {repo}")
    print(f"Tag: {tag}")
    print(f"Assets: {', '.join(path.name for path in assets)}")

    release = _get_release(repo, tag, token)
    if args.check:
        if release is None:
            print("Release: missing")
        else:
            print(
                f"Release: id={release.get('id')} draft={release.get('draft')} "
                f"url={release.get('html_url')}"
            )
            for asset in release.get("assets", []):
                print(
                    f"  {asset.get('name')}: state={asset.get('state')} "
                    f"size={asset.get('size')} digest={asset.get('digest')}"
                )
        return 0

    commit = _run_git("rev-parse", "HEAD")
    if release is None:
        print("Creating draft release...")
        release = _api_json(
            "POST",
            f"{API_ROOT}/repos/{repo}/releases",
            token,
            {
                "tag_name": tag,
                "target_commitish": commit,
                "name": f"Auto THU DeepSeek API {version}",
                "body": _release_body(version),
                "draft": True,
                "prerelease": False,
            },
        )
        if not release:
            raise ReleaseError("GitHub did not return the created release")

    release_id = int(release["id"])
    is_draft = bool(release.get("draft"))
    remote_assets = {
        str(asset.get("name")): asset
        for asset in release.get("assets", [])
        if isinstance(asset, dict)
    }
    for path in assets:
        existing = remote_assets.get(path.name)
        if existing is not None:
            same_size = int(existing.get("size") or -1) == path.stat().st_size
            expected = f"sha256:{_sha256(path)}"
            same_digest = not existing.get("digest") or existing.get("digest") == expected
            if same_size and same_digest and existing.get("state") == "uploaded":
                print(f"Skipping verified asset: {path.name}")
                continue
            if not is_draft:
                raise ReleaseError(
                    f"Published release has a conflicting asset: {path.name}"
                )
            print(f"Removing conflicting draft asset: {path.name}")
            _delete_asset(repo, int(existing["id"]), token)
        print(f"Uploading {path.name} ({path.stat().st_size} bytes)...")
        _upload_asset(repo, release_id, path, token)

    release = _wait_for_uploaded(repo, tag, token, assets)
    print("All draft assets verified.")
    if args.publish:
        if release.get("draft"):
            print("Publishing release...")
            release = _api_json(
                "PATCH",
                f"{API_ROOT}/repos/{repo}/releases/{release_id}",
                token,
                {"draft": False, "prerelease": False},
            )
            if not release:
                raise ReleaseError("GitHub did not return the published release")
        release = _wait_for_uploaded(repo, tag, token, assets)
        print(f"Published: {release.get('html_url')}")
    else:
        print(f"Draft ready: {release.get('html_url')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
