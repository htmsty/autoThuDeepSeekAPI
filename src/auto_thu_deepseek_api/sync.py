"""同步编排：校验 Token → 逐目标备份并写入。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import paths
from .backup import BackupManager
from .settings import AppSettings
from .targets import ApplyResult, CcSwitchTarget, DshTarget
from .targets.base import TargetStatus
from .token.verifier import VerifyResult, fetch_models, verify_token

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    verified: VerifyResult | None = None
    statuses: list[TargetStatus] = field(default_factory=list)
    results: list[ApplyResult] = field(default_factory=list)
    models: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
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
        return "；".join(parts) if parts else "没有任何目标被写入"


def build_targets(settings: AppSettings) -> list[object]:
    targets: list[object] = []
    if settings.enable_dsh:
        targets.append(DshTarget(paths.resolve_dsh_paths(settings.dsh_dir), settings.unify_all_profiles))
    if settings.enable_ccswitch:
        targets.append(CcSwitchTarget(paths.resolve_ccswitch_paths(settings.ccswitch_dir)))
    return targets


def detect_targets(settings: AppSettings) -> list[TargetStatus]:
    return [target.detect() for target in build_targets(settings)]


def run_sync(
    settings: AppSettings,
    token: str,
    *,
    verify: bool = True,
    fetch_model_list: bool = True,
) -> SyncReport:
    """把 Token 写进所有启用且已安装的目标。"""

    report = SyncReport()
    targets = build_targets(settings)

    models: list[dict] = []
    if fetch_model_list:
        models = fetch_models(token) or []
    report.models = models

    if verify:
        report.verified = verify_token(token, models[0]["id"] if models else None)
        logger.info("Token 校验：%s", report.verified.detail)
        if report.verified.ok is False:
            report.skipped.append("Token 被站点拒绝，未写入任何目标")
            return report

    backup = BackupManager(paths.backups_dir(), keep=settings.backup_keep)
    for target in targets:
        status = target.detect()
        report.statuses.append(status)
        if not status.detected:
            report.skipped.append(f"{status.label}（未安装）")
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

