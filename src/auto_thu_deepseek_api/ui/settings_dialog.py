"""设置对话框：路径、开关、CC Switch 策略、备份还原、手动粘贴 Token。"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
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
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import paths, startup, uninstall
from ..backup import BackupManager
from ..constants import DEFAULT_MODELS
from ..settings import (
    CCSWITCH_POLICY_DIRECT,
    CCSWITCH_POLICY_PROMPT,
    AppSettings,
    reset_to_defaults,
)
from ..token.jwt_utils import looks_like_jwt

logger = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.manual_token = ""
        self.setWindowTitle("设置")
        # 宽度要放得下最长的复选框文案，窄了右边会被截断；高度交给滚动区
        self.setMinimumWidth(900)
        self.resize(920, 760)

        # 只有一个挂在对话框上的布局：滚动区 + 底部按钮。
        # 注意别再造第二个 QVBoxLayout(self)——后建的那个会把先挂的布局顶掉，
        # 结果对话框没有布局、点「打开配置」就什么也看不到（2.4 的真实 bug）。
        self.outer = QVBoxLayout(self)

        # 内容比一屏高，放进滚动区，窗口小的时候也能看到「创建桌面快捷方式」等底部选项
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        # 留住 Python 引用：只靠父控件关系的话，局部变量出栈时控件可能一起被回收
        self._scroll = scroll
        self._content = content
        content_layout = QVBoxLayout(content)
        scroll.setWidget(content)
        self.outer.addWidget(scroll, 1)
        layout = content_layout

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
        self.suppress_warning = QCheckBox("检测到运行时直接改，不再弹提醒（默认勾选）")
        self.suppress_warning.setChecked(settings.suppress_ccswitch_running_warning)
        self.suppress_warning.setToolTip(
            "勾选：CC Switch 正在运行也直接写数据库，不弹提醒。\n"
            "不勾：每次同步前先提示，由你选「关闭好了，继续」「直接改」还是「取消」。"
        )
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

        # —— 运行数据与维护 ——
        maintenance_box = QGroupBox("运行数据与维护")
        maintenance_layout = QVBoxLayout(maintenance_box)

        private_row = QHBoxLayout()
        private_row.addWidget(QLabel("private 数据文件夹"))
        # 用只读输入框而不是 QLabel：长路径不会把整个对话框撑出横向滚动条，也能直接选中复制
        self.private_path_label = QLineEdit(str(paths.data_root()))
        self.private_path_label.setReadOnly(True)
        self.private_path_label.setCursorPosition(0)
        self.private_path_label.setToolTip(
            "Token、设置、状态、日志、备份和浏览器会话都放在这里。\n"
            "默认在程序目录旁；也可以放到别处（比如不放桌面、不放系统盘）。"
        )
        private_row.addWidget(self.private_path_label, 1)
        self.private_button = QPushButton("更改位置…")
        self.private_button.setToolTip("把运行数据搬到别的文件夹（重启后生效）")
        self.private_button.clicked.connect(self._change_private_dir)
        private_row.addWidget(self.private_button)
        maintenance_layout.addLayout(private_row)

        maintain_row = QHBoxLayout()
        self.defaults_button = QPushButton("恢复默认设置")
        self.defaults_button.setToolTip("把所有设置恢复成程序默认值（开机启动项不动）")
        self.defaults_button.clicked.connect(self._restore_defaults)
        self.uninstall_button = QPushButton("干净卸载…")
        self.uninstall_button.setToolTip(
            "可选还原 DSH / CC Switch 到首次备份，然后清掉程序的全部运行数据（不删程序自己）"
        )
        self.uninstall_button.clicked.connect(self._clean_uninstall)
        maintain_row.addWidget(self.defaults_button)
        maintain_row.addWidget(self.uninstall_button)
        maintain_row.addStretch(1)
        maintenance_layout.addLayout(maintain_row)
        layout.addWidget(maintenance_box)

        # —— 更多 ——
        more_box = QGroupBox("更多")
        more_form = QFormLayout(more_box)

        self.preflight_check = QCheckBox("同步前先预检将要写入的内容（不通过就这次不写）")
        self.preflight_check.setChecked(settings.preflight_enabled)
        self.preflight_check.setToolTip(
            "预检把要写出去的 YAML / JSON 先渲染一遍再校验：能不能解析、字段齐不齐、\n"
            "模型 id 与站点清单是否一致、Token 是不是本次拿到的那个。只读，不改文件。"
        )
        more_form.addRow(self.preflight_check)

        self.close_to_tray_check = QCheckBox("点 × 时最小化到托盘（取消勾选则直接退出程序）")
        self.close_to_tray_check.setChecked(settings.close_to_tray)
        more_form.addRow(self.close_to_tray_check)

        self.silent_timeout_spin = QSpinBox()
        self.silent_timeout_spin.setRange(10, 600)
        self.silent_timeout_spin.setSuffix(" 秒")
        self.silent_timeout_spin.setValue(settings.silent_capture_timeout_seconds)
        self.silent_timeout_spin.setToolTip("自动同步时后台静默取 Token 最多等多久；超过就沿用旧 Token 并提示")
        more_form.addRow("静默取 Token 超时", self.silent_timeout_spin)

        self.default_model_combo = QComboBox()
        self.default_model_combo.setEditable(True)
        for model in DEFAULT_MODELS:
            self.default_model_combo.addItem(str(model["id"]))
        self.default_model_combo.setCurrentText(settings.default_model_id or str(DEFAULT_MODELS[0]["id"]))
        self.default_model_combo.setToolTip(
            "勾选「统一所有 profile」时，DSH 的默认模型用哪个 id（要能在站点清单里找到；留空=用第一个）"
        )
        more_form.addRow("DSH 默认模型", self.default_model_combo)

        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["INFO", "DEBUG"])
        self.log_level_combo.setCurrentText(settings.log_level)
        self.log_level_combo.setToolTip("DEBUG 会记录更多细节，排查问题时再开")
        more_form.addRow("日志级别", self.log_level_combo)

        self.shortcut_button = QPushButton("创建桌面快捷方式")
        self.shortcut_button.setToolTip("在桌面生成一个指向本程序的快捷方式，双击即可启动")
        self.shortcut_button.clicked.connect(self._create_shortcut)
        more_form.addRow("快捷方式", self.shortcut_button)

        layout.addWidget(more_box)

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
        # 按钮固定在窗口底部，不跟着内容一起滚
        self.outer.addWidget(buttons)

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

    def _create_shortcut(self) -> None:
        # 主界面已经有这个功能，直接复用它的入口（弹窗、日志口径保持一致）
        parent = self.parent()
        if parent is not None and hasattr(parent, "create_shortcut"):
            parent.create_shortcut()
            return
        ok, message = startup.create_desktop_shortcut()
        logger.info(message)
        if ok:
            QMessageBox.information(self, "创建桌面快捷方式", message)
        else:
            QMessageBox.warning(self, "创建桌面快捷方式", message)

    # —— 运行数据位置 / 维护 ——

    def _change_private_dir(self) -> None:
        """把 private 运行数据换到别的文件夹（重启后生效）。"""

        parent = self.parent()
        if parent is not None and hasattr(parent, "choose_private_dir"):
            parent.choose_private_dir()
        else:  # pragma: no cover - 正常都有主窗口
            QMessageBox.information(
                self, "更改 private 位置", "请从主界面打开设置后再更改 private 位置。"
            )
        self.private_path_label.setText(str(paths.data_root()))

    def _request_restart(self) -> None:
        parent = self.parent()
        if parent is not None and hasattr(parent, "restart_app"):
            parent.restart_app()
        else:  # pragma: no cover - 正常都有主窗口
            QMessageBox.information(self, "重启程序", "请手动关闭再打开程序，新位置才会生效。")

    def _restore_defaults(self) -> None:
        """一键恢复默认设置（开机启动项不动；private 位置会另外问）。"""

        if QMessageBox.question(
            self,
            "恢复默认设置",
            "将把设置恢复成默认值：\n"
            "· 自动同步：开，间隔 180 分钟，启动时立即同步：开\n"
            "· 统一所有 profile 到 madmodel：勾选\n"
            "· 同步前预检：开；CC Switch 运行中：不弹提醒、直接改\n"
            "· 备份保留 10 份；静默取 Token 超时 40 秒；日志级别 INFO；关窗最小化到托盘\n\n"
            "开机启动项（注册表）保持现状，不会被动。\n确定恢复吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        settings = reset_to_defaults(self.settings)
        settings.save()
        logger.info("设置已恢复为默认值")

        self._reset_widgets_to_defaults()
        parent = self.parent()
        if parent is not None and hasattr(parent, "apply_settings_to_ui"):
            parent.apply_settings_to_ui()
        QMessageBox.information(self, "恢复默认设置", "已恢复默认设置。")
        self._offer_reset_private_dir()

    def _reset_widgets_to_defaults(self) -> None:
        """把对话框里的控件刷成当前 settings 的值（恢复默认后立刻能看到）。"""

        settings = self.settings
        self.dsh_enabled.setChecked(settings.enable_dsh)
        self.dsh_dir_edit.setText(settings.dsh_dir)
        self.unify_profiles.setChecked(settings.unify_all_profiles)
        self.ccswitch_enabled.setChecked(settings.enable_ccswitch)
        self.ccswitch_dir_edit.setText(settings.ccswitch_dir)
        if settings.ccswitch_running_policy == CCSWITCH_POLICY_DIRECT:
            self.policy_direct.setChecked(True)
        else:
            self.policy_prompt.setChecked(True)
        self.suppress_warning.setChecked(settings.suppress_ccswitch_running_warning)
        self.keep_spin.setValue(settings.backup_keep)
        self.preflight_check.setChecked(settings.preflight_enabled)
        self.close_to_tray_check.setChecked(settings.close_to_tray)
        self.silent_timeout_spin.setValue(settings.silent_capture_timeout_seconds)
        self.default_model_combo.setCurrentText(
            settings.default_model_id or str(DEFAULT_MODELS[0]["id"])
        )
        self.log_level_combo.setCurrentText(settings.log_level)
        self.private_path_label.setText(str(paths.data_root()))

    def _offer_reset_private_dir(self) -> None:
        """恢复默认设置的联动：问一句要不要把 private 位置也还原。"""

        if paths.read_private_override() is None:
            return
        current = paths.data_root()
        if QMessageBox.question(
            self,
            "恢复默认设置",
            f"private 数据文件夹现在在：\n{current}\n\n"
            "要顺便还原成默认位置（程序目录旁）吗？\n"
            "还原同样要搬数据并重启程序，确定的话现在就开始搬。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, message = paths.switch_private_root(paths.default_private_dir())
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            QMessageBox.warning(self, "恢复默认设置", message)
            return
        logger.info(message)
        self.private_path_label.setText(str(paths.data_root()))
        if QMessageBox.question(
            self,
            "恢复默认设置",
            message + "\n\n现在重启程序吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self._request_restart()

    def _clean_uninstall(self) -> None:
        """干净卸载：二次确认，可选还原 DSH / CC Switch，只删程序自己的东西。"""

        parent = self.parent()
        if parent is not None and hasattr(parent, "_busy") and parent._busy():
            QMessageBox.warning(
                self, "干净卸载", "正在同步或正在取 Token，等它结束（或强行终止）再卸载。"
            )
            return
        if QMessageBox.warning(
            self,
            "干净卸载",
            "将清掉程序的全部运行数据：Token、设置、状态、日志、备份、浏览器会话、模型缓存、提醒状态。\n\n"
            "程序自身的 exe **不会**被删除（需要你自己手动删）。\n"
            "自选目录里属于你自己的文件也不会动，只会删程序管理的那几项。\n\n继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        restore = QMessageBox.question(
            self,
            "干净卸载",
            "要把 DSH / CC Switch 还原到本程序**第一次修改之前**的备份吗？\n\n"
            "风险：会覆盖这两个应用当前的配置，正在用的模型/令牌可能一起被换回旧值；\n"
            "如果它们正在运行，建议先退出。\n\n"
            "选「No」= 只清程序自己的运行数据，不动 DSH / CC Switch。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if QMessageBox.warning(
            self,
            "干净卸载",
            "最后确认：清掉之后无法恢复（备份也会一起删掉）。现在执行吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        # 先放开浏览器档案、日志句柄与单实例锁，不然 browser-profile / logs / app.lock 删不掉
        if parent is not None and hasattr(parent, "prepare_for_uninstall"):
            parent.prepare_for_uninstall()
        ok, lines = uninstall.clean_uninstall(
            self.settings, restore_targets=(restore == QMessageBox.StandardButton.Yes)
        )
        detail = "\n".join(lines) or "没有需要清理的内容。"
        if not ok:
            detail += "\n\n（有几项没删干净，详见上面的说明。）"
        QMessageBox.information(
            self,
            "干净卸载",
            detail
            + "\n\n请手动删除程序文件：\n"
            + uninstall.program_exe_hint()
            + "\n\n程序现在会退出。",
        )

        if parent is not None and hasattr(parent, "quit_after_uninstall"):
            parent.quit_after_uninstall()
        else:  # pragma: no cover - 正常都有主窗口
            self.reject()

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
        settings.preflight_enabled = self.preflight_check.isChecked()
        settings.close_to_tray = self.close_to_tray_check.isChecked()
        settings.silent_capture_timeout_seconds = self.silent_timeout_spin.value()
        settings.log_level = self.log_level_combo.currentText()
        text = self.default_model_combo.currentText().strip()
        settings.default_model_id = "" if text == str(DEFAULT_MODELS[0]["id"]) else text
        settings.normalize()

