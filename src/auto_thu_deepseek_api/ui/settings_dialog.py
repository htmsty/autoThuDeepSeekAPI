"""设置对话框：路径、开关、CC Switch 策略、备份还原、手动粘贴 Token。"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..backup import BackupManager
from ..settings import CCSWITCH_POLICY_DIRECT, CCSWITCH_POLICY_PROMPT, AppSettings
from ..token.jwt_utils import looks_like_jwt

logger = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.manual_token = ""
        self.setWindowTitle("设置")
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)

        # —— 目标 ——
        targets_box = QGroupBox("写入目标（检测到哪个写哪个，可分别关闭）")
        targets_form = QFormLayout(targets_box)

        self.dsh_enabled = QCheckBox("启用 DSH 写入（凭据 + provider）")
        self.dsh_enabled.setChecked(settings.enable_dsh)
        targets_form.addRow(self.dsh_enabled)

        self.dsh_dir_edit = QLineEdit(settings.dsh_dir)
        self.dsh_dir_edit.setPlaceholderText(str(paths.default_dsh_dir()))
        targets_form.addRow("DSH 目录", self._path_row(self.dsh_dir_edit, self._browse_dsh))

        self.unify_profiles = QCheckBox("统一所有 profile 到 madmodel（不勾选时只写已配置 madmodel 的 profile）")
        self.unify_profiles.setChecked(settings.unify_all_profiles)
        self.unify_profiles.setToolTip(
            "勾选：所有 profile 都写，并把默认模型切到 madmodel / DeepSeek-V4.1-Flash。\n"
            "不勾：只更新已配置 madmodel 的 profile，默认模型保持你在 DSH 里选的那个。"
        )
        targets_form.addRow(self.unify_profiles)

        self.ccswitch_enabled = QCheckBox("启用 CC Switch 写入（供应商卡片 + 当前路由）")
        self.ccswitch_enabled.setChecked(settings.enable_ccswitch)
        targets_form.addRow(self.ccswitch_enabled)

        self.ccswitch_dir_edit = QLineEdit(settings.ccswitch_dir)
        self.ccswitch_dir_edit.setPlaceholderText(str(paths.default_ccswitch_dir()))
        targets_form.addRow("CC Switch 目录", self._path_row(self.ccswitch_dir_edit, self._browse_ccswitch))

        layout.addWidget(targets_box)

        # —— CC Switch 运行中 ——
        policy_box = QGroupBox("CC Switch 正在运行时")
        policy_layout = QVBoxLayout(policy_box)
        self.policy_prompt = QRadioButton("提示我先关闭，再由我决定")
        self.policy_direct = QRadioButton("不提示，直接改数据库")
        if settings.ccswitch_running_policy == CCSWITCH_POLICY_DIRECT:
            self.policy_direct.setChecked(True)
        else:
            self.policy_prompt.setChecked(True)
        policy_layout.addWidget(self.policy_prompt)
        policy_layout.addWidget(self.policy_direct)
        self.suppress_warning = QCheckBox("不再显示关闭提醒（等同直接改）")
        self.suppress_warning.setChecked(settings.suppress_ccswitch_running_warning)
        policy_layout.addWidget(self.suppress_warning)
        layout.addWidget(policy_box)

        # —— 备份 ——
        backup_box = QGroupBox("备份与还原")
        backup_layout = QHBoxLayout(backup_box)
        self.keep_spin = QSpinBox()
        self.keep_spin.setRange(1, 200)
        self.keep_spin.setValue(settings.backup_keep)
        backup_layout.addWidget(QLabel("每个文件保留备份份数"))
        backup_layout.addWidget(self.keep_spin)
        backup_layout.addStretch(1)
        self.restore_button = QPushButton("还原到首次修改前")
        self.restore_button.clicked.connect(self._restore)
        backup_layout.addWidget(self.restore_button)
        self.paste_button = QPushButton("手动粘贴 Token…")
        self.paste_button.clicked.connect(self._paste_token)
        backup_layout.addWidget(self.paste_button)
        layout.addWidget(backup_box)

        note = QLabel(
            f"运行数据目录：{paths.data_root()}\n"
            "所有 Token、日志、备份与浏览器会话都保存在这里，默认不进入 Git。"
        )
        note.setWordWrap(True)
        note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # —— 小工具 ——

    def _path_row(self, edit: QLineEdit, browse) -> QWidget:  # noqa: ANN001
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        button = QPushButton("浏览…")
        button.clicked.connect(browse)
        row.addWidget(button)
        return container

    def _browse_dsh(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择 DSH 目录", self._start_dir(self.dsh_dir_edit, paths.default_dsh_dir()))
        if chosen:
            self.dsh_dir_edit.setText(chosen)

    def _browse_ccswitch(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择 CC Switch 目录", self._start_dir(self.ccswitch_dir_edit, paths.default_ccswitch_dir())
        )
        if chosen:
            self.ccswitch_dir_edit.setText(chosen)

    @staticmethod
    def _start_dir(edit: QLineEdit, fallback: Path) -> str:
        current = edit.text().strip()
        if current and Path(current).is_dir():
            return current
        return str(fallback)

    def accept(self) -> None:  # noqa: D102 - QDialog 约定
        problems: list[str] = []
        for label, edit in (("DSH", self.dsh_dir_edit), ("CC Switch", self.ccswitch_dir_edit)):
            value = edit.text().strip()
            if value and not Path(value).is_dir():
                problems.append(f"{label}：{value}")
        if problems:
            answer = QMessageBox.question(
                self,
                "目录不存在",
                "这些自定义目录现在打不开，仍要保存吗？\n\n" + "\n".join(problems),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        super().accept()

    # —— 动作 ——

    def _restore(self) -> None:
        manager = BackupManager(paths.backups_dir(), keep=self.settings.backup_keep)
        entries = manager.original_entries()
        if not entries:
            QMessageBox.information(self, "还原", "还没有任何备份，不需要还原。")
            return
        detail = "\n".join(f"· {Path(source).name} ← {Path(entry.backup).name}" for source, entry in entries.items())
        confirm = QMessageBox.question(
            self,
            "还原到首次修改前",
            "将用最早的备份覆盖以下文件（当前状态会先被备份）：\n\n" + detail,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        messages = manager.restore(entries)
        QMessageBox.information(self, "还原结果", "\n".join(messages) or "没有可还原的内容。")

    def _paste_token(self) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self,
            "手动粘贴 Token",
            "把站点「API 使用指南 → 复制 token」得到的内容粘贴到这里：",
            "",
        )
        if not ok:
            return
        candidate = text.strip().strip('"').strip()
        if not looks_like_jwt(candidate):
            QMessageBox.warning(self, "手动粘贴 Token", "这看起来不是一个 Token（JWT）。请检查后重试。")
            return
        self.manual_token = candidate
        QMessageBox.information(self, "手动粘贴 Token", "已记录该 Token，关闭设置后会用它同步。")

    # —— 结果 ——

    def apply_to(self, settings: AppSettings) -> None:
        settings.enable_dsh = self.dsh_enabled.isChecked()
        settings.enable_ccswitch = self.ccswitch_enabled.isChecked()
        settings.dsh_dir = self.dsh_dir_edit.text().strip()
        settings.ccswitch_dir = self.ccswitch_dir_edit.text().strip()
        settings.unify_all_profiles = self.unify_profiles.isChecked()
        settings.ccswitch_running_policy = (
            CCSWITCH_POLICY_DIRECT if (self.policy_direct.isChecked() or self.suppress_warning.isChecked())
            else CCSWITCH_POLICY_PROMPT
        )
        settings.suppress_ccswitch_running_warning = self.suppress_warning.isChecked()
        settings.backup_keep = self.keep_spin.value()
        settings.normalize()

