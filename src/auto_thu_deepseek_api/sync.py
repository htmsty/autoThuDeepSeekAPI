"""同步编排：校验 Token → 逐目标备份并写入。"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, procinfo
from .backup import BackupManager
from .preflight import GLOBAL_CHECK_KEYS, PreflightReport, run_preflight
from .settings import AppSettings
from .targets import ApplyResult, CcSwitchTarget, DshTarget
from .targets.base import TargetStatus
from .token import site_models
from .token.jwt_utils import fingerprint
from .token.verifier import VerifyResult, verify_token

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    verified: VerifyResult | None = None
    statuses: list[TargetStatus] = field(default_factory=list)
    results: list[ApplyResult] = field(default_factory=list)
    models: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    preflight: PreflightReport | None = None
    # 用户点了「强行终止并重试」：这次同步在步骤之间安全停下
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        """整体是否算成功。

        站点明确拒绝 Token、或一个目标都没写成（例如都没安装）都不算成功——
        免得界面上弹出"同步完成"让人以为一切正常。
        """

        if self.verified is not None and self.verified.ok is False:
            return False
        if self.cancelled:
            return False
        if not self.results:
            return False
        return all(result.ok for result in self.results)

    def summary(self) -> str:
        parts: list[str] = []
        if self.verified is not None:
            if self.verified.ok is True:
                parts.append("Token 校验通过")
            elif self.verified.ok is False:
                parts.append("Token 被站点拒绝")
            else:
                parts.append("Token 未能校验")
        for result in self.results:
            if not result.ok:
                parts.append(f"{result.label} 失败")
            elif result.changed:
                parts.append(f"{result.label} 已更新")
            else:
                parts.append(f"{result.label} 无变化")
        if self.skipped:
            parts.append("跳过：" + "、".join(self.skipped))
        if self.cancelled:
            parts.insert(0, "已被用户终止（安全停下）")
        return "；".join(parts) if parts else "没有任何目标被写入"


def build_targets(settings: AppSettings) -> list[object]:
    targets: list[object] = []
    if settings.enable_dsh:
        targets.append(
            DshTarget(
                paths.resolve_dsh_paths(settings.dsh_dir),
                settings.unify_all_profiles,
                settings.default_model_id,
            )
        )
    if settings.enable_ccswitch:
        targets.append(
            CcSwitchTarget(
                paths.resolve_ccswitch_paths(settings.ccswitch_dir),
                refresh_sibling_cards=settings.ccswitch_refresh_sibling_cards,
            )
        )
    return targets


def detect_targets(settings: AppSettings) -> list[TargetStatus]:
    return [target.detect() for target in build_targets(settings)]


def _installed_like_dir(keywords: tuple[str, ...]) -> Path | None:
    """在常见安装位置里找一个名字对得上的目录。

    只做浅层扫描（一层），别为了"到底装没装"把启动拖慢。
    """

    roots: list[Path] = []
    for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "APPDATA"):
        value = os.environ.get(variable, "").strip()
        if not value:
            continue
        base = Path(value)
        roots.append(base / "Programs" if variable in ("LOCALAPPDATA", "APPDATA") else base)

    for root in roots:
        if not root.is_dir():
            continue
        try:
            for entry in root.iterdir():
                if entry.is_dir() and any(word in entry.name.lower() for word in keywords):
                    return entry
        except OSError:  # pragma: no cover - 扫不动就当没有
            continue
    return None


def target_traces(settings: AppSettings) -> dict[str, str]:
    """两个目标各自"有没有任何痕迹"，用于启动时判断要不要提醒"先安装"。

    返回 {"dsh": 说明, "ccswitch": 说明}：说明为空串 = 一点痕迹都没有。
    判据尽量放宽（配置目录、进程、常见安装目录任一命中就算有痕迹）——宁可少提醒，
    也别在用户已经装好的时候冤枉人家。
    """

    traces = {"dsh": "", "ccswitch": ""}

    dsh = paths.resolve_dsh_paths(settings.dsh_dir).base_dir
    if dsh.is_dir():
        traces["dsh"] = f"DSH 目录存在：{dsh}"
    else:
        process = procinfo.any_running("deepseek", "dsh")
        if process:
            traces["dsh"] = f"检测到正在运行的进程：{process}"
        else:
            found = _installed_like_dir(("deepseek", "harness"))
            if found is not None:
                traces["dsh"] = f"安装目录存在：{found}"

    ccswitch = paths.resolve_ccswitch_paths(settings.ccswitch_dir).base_dir
    if ccswitch.is_dir():
        traces["ccswitch"] = f"CC Switch 目录存在：{ccswitch}"
    else:
        process = procinfo.any_running("cc-switch", "ccswitch")
        if process:
            traces["ccswitch"] = f"检测到正在运行的进程：{process}"
        else:
            found = _installed_like_dir(("cc-switch", "ccswitch"))
            if found is not None:
                traces["ccswitch"] = f"安装目录存在：{found}"

    return traces


def both_targets_missing(settings: AppSettings) -> tuple[bool, dict[str, str]]:
    """两边是不是都"一点痕迹都没有"。"""

    traces = target_traces(settings)
    return (not traces["dsh"] and not traces["ccswitch"]), traces


def run_sync(
    settings: AppSettings,
    token: str,
    *,
    verify: bool = True,
    fetch_model_list: bool = True,
    cancel_event: threading.Event | None = None,
) -> SyncReport:
    """把 Token 写进所有启用且已安装的目标。

    `cancel_event` 被置位时在**步骤之间**安全停下：正在写的那一份文件会先原子写完
    （失败还有回滚兜底），不会中途丢下写了一半的文件。
    """

    report = SyncReport()

    def stopped() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if stopped():
        report.cancelled = True
        report.skipped.append("已被用户终止")
        return report

    targets = build_targets(settings)

    models: list[dict] = []
    if fetch_model_list:
        # 站点前端包 → /v1/models → 内置名单（详见 token/site_models.py）
        models = site_models.resolve_models(token)
    report.models = models

    if stopped():
        report.cancelled = True
        report.skipped.append("已被用户终止，未写入任何目标")
        return report

    if verify:
        report.verified = verify_token(token, models[0]["id"] if models else None)
        logger.info(
            "Token 校验（向站点发起会话校验请求，指纹 %s）：%s",
            fingerprint(token),
            report.verified.detail,
        )
        if report.verified.ok is False:
            report.skipped.append("Token 被站点拒绝，未写入任何目标")
            return report

    if stopped():
        report.cancelled = True
        report.skipped.append("已被用户终止，未写入任何目标")
        return report

    blocked: set[str] = set()
    if settings.preflight_enabled:
        report.preflight = run_preflight(settings, token, models)
        for line in report.preflight.text().splitlines():
            logger.info("预检 %s", line)
        blocked = report.preflight.blocked_targets()
        if blocked:
            logger.warning("预检未通过，这次跳过写入：%s", "、".join(sorted(blocked)))

    backup = BackupManager(paths.backups_dir(), keep=settings.backup_keep)
    for target in targets:
        if stopped():
            report.cancelled = True
            report.skipped.append("已被用户终止，剩余目标未写入")
            break
        status = target.detect()
        report.statuses.append(status)
        if not status.detected:
            report.skipped.append(f"{status.label}（未安装）")
            continue
        if target.key in blocked:
            # 全局检查（Token 不是 JWT / 模型清单不合法）失败时也会走到这里，
            # 所以除了"这个目标自己的失败项"，还要带上全局失败项的说明
            reasons = "；".join(
                item.detail
                for item in (report.preflight.failed() if report.preflight else [])
                if item.target_key in (target.key, *GLOBAL_CHECK_KEYS)
            )
            result = ApplyResult(target.key, target.label, ok=False)
            result.fail(f"预检未通过，已跳过写入：{reasons}")
            report.results.append(result)
            logger.error("%s：%s", result.label, result.errors[0])
            continue
        logger.info("开始写入 %s", status.label)
        try:
            result = target.apply(token, models, backup)
        except Exception as exc:  # noqa: BLE001 - 单个目标失败不影响其它目标
            result = ApplyResult(target.key, target.label, ok=False)
            result.fail(f"写入过程异常：{exc}")
        report.results.append(result)
        for message in result.messages:
            logger.info("%s：%s", result.label, message)
        for message in result.errors:
            logger.error("%s：%s", result.label, message)

    return report

