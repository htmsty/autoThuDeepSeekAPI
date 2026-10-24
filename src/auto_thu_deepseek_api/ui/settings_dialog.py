"""设置对话框：路径、开关、CC Switch 策略、备份还原、手动粘贴 Token。"""

from __future__ import annotations

import copy
import logging
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import backup_ops, credentials, data_location, paths, startup, uninstall
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

DEFAULT_DIALOG_WIDTH = 700
DEFAULT_DIALOG_HEIGHT = 540
MIN_DIALOG_WIDTH = 640
MIN_DIALOG_HEIGHT = 480


class SettingsDialog(QDialog):
    settings_applied = Signal()
    window_size_saved = Signal(int, int)

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
        *,
        saved_size: Sequence[int] | None = None,
    ) -> None:
        super().__init__(parent)
        # 使用草稿副本：取消时不能把已经点过的控件值写回主窗口。
        self.settings = copy.deepcopy(settings)
        self.manual_token = ""
        self._saved_username = ""
        try:
            stored = credentials.load_login()
        except OSError as exc:
            logger.warning("读取已保存登录用户名失败：%s", exc)
            stored = None
        if stored is not None:
            self._saved_username = stored.username
        self.setWindowTitle("设置")
        self.setMinimumSize(MIN_DIALOG_WIDTH, MIN_DIALOG_HEIGHT)
        self.resize(DEFAULT_DIALOG_WIDTH, DEFAULT_DIALOG_HEIGHT)

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
        tabs = QTabWidget()
        self.tabs = tabs

        sync_page = QWidget()
        sync_layout = QVBoxLayout(sync_page)
        targets_page = QWidget()
        targets_layout = QVBoxLayout(targets_page)
        security_page = QWidget()
        security_layout = QVBoxLayout(security_page)
        maintenance_page = QWidget()
        maintenance_page_layout = QVBoxLayout(maintenance_page)
        advanced_page = QWidget()
        advanced_layout = QVBoxLayout(advanced_page)
        tabs.addTab(sync_page, "同步")
        tabs.addTab(targets_page, "写入目标")
        tabs.addTab(security_page, "登录与安全")
        tabs.addTab(maintenance_page, "备份与维护")
        tabs.addTab(advanced_page, "高级")
        content_layout.addWidget(tabs)

        # —— 同步 ——
        sync_box = QGroupBox("自动同步")
        sync_form = QFormLayout(sync_box)
        self.auto_sync_check = QCheckBox("启用自动同步")
        self.auto_sync_check.setChecked(settings.auto_sync_enabled)
        sync_form.addRow(self.auto_sync_check)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 1440)
        self.interval_spin.setSuffix(" 分钟")
        self.interval_spin.setValue(settings.sync_interval_minutes)
        self.interval_spin.setToolTip("实际会在基准值前后随机浮动 10 分钟")
        sync_form.addRow("同步间隔", self.interval_spin)
        self.sync_on_start_check = QCheckBox("启动时立即同步")
        self.sync_on_start_check.setChecked(settings.sync_on_start)
        sync_form.addRow(self.sync_on_start_check)
        self.minimize_after_check = QCheckBox("同步完成后最小化到托盘")
        self.minimize_after_check.setChecked(settings.minimize_after_sync)
        sync_form.addRow(self.minimize_after_check)
        sync_layout.addWidget(sync_box)
        sync_layout.addStretch(1)

        # —— 目标 ——
        targets_box = QGroupBox("写入目标（检测到哪个写哪个，可分别关闭）")
        targets_form = QFormLayout(targets_box)

        self.dsh_enabled = QCheckBox("启用 DSH 写入")
        self.dsh_enabled.setChecked(settings.enable_dsh)
        targets_form.addRow(self.dsh_enabled)

        self.dsh_credentials_check = QCheckBox("更新凭据 MADMODEL_API_KEY")
        self.dsh_credentials_check.setChecked(settings.enable_dsh_credentials)
        targets_form.addRow(self.dsh_credentials_check)

        self.dsh_provider_check = QCheckBox("更新 llm-pi-ai Provider 与模型清单")
        self.dsh_provider_check.setChecked(settings.enable_dsh_provider)
        targets_form.addRow(self.dsh_provider_check)

        self.dsh_dir_edit = QLineEdit(settings.dsh_dir)
        self.dsh_dir_edit.setPlaceholderText(str(paths.default_dsh_dir()))
        targets_form.addRow("DSH 目录", self._path_row(self.dsh_dir_edit, self._browse_dsh))

        self.dsh_profile_edit = QLineEdit(settings.dsh_profile)
        self.dsh_profile_edit.setPlaceholderText("留空自动选择；找不到时只创建到 desktop 或第一个 profile")
        targets_form.addRow("受管 profile", self.dsh_profile_edit)

        self.ccswitch_enabled = QCheckBox("启用 CC Switch 写入")
        self.ccswitch_enabled.setChecked(settings.enable_ccswitch)
        targets_form.addRow(self.ccswitch_enabled)

        self.ccswitch_card_check = QCheckBox("更新 DeepSeek (THU) 供应商卡片")
        self.ccswitch_card_check.setChecked(settings.enable_ccswitch_card)
        targets_form.addRow(self.ccswitch_card_check)

        self.ccswitch_activate_check = QCheckBox("把 DeepSeek (THU) 设为当前供应商和代理路由")
        self.ccswitch_activate_check.setChecked(settings.enable_ccswitch_activate)
        self.ccswitch_activate_check.setToolTip(
            "会改变 CC Switch 当前供应商，并清理其它卡片同一 app_type 的 is_current；默认关闭。"
        )
        targets_form.addRow(self.ccswitch_activate_check)

        self.ccswitch_dir_edit = QLineEdit(settings.ccswitch_dir)
        self.ccswitch_dir_edit.setPlaceholderText(str(paths.default_ccswitch_dir()))
        targets_form.addRow("CC Switch 目录", self._path_row(self.ccswitch_dir_edit, self._browse_ccswitch))

        self.refresh_siblings_check = QCheckBox("顺带刷新同站点旧卡片的令牌（会改动别的卡片，默认关）")
        self.refresh_siblings_check.setChecked(settings.ccswitch_refresh_sibling_cards)
        self.refresh_siblings_check.setToolTip(
            "打开后：非本程序名下、但配置里出现清华站点的卡片，其令牌字段也会被换成新 Token。\n"
            "默认关闭——程序只维护自己那张卡片（DeepSeek (THU)），不动别人的配置。"
        )
        targets_form.addRow(self.refresh_siblings_check)

        targets_layout.addWidget(targets_box)

        # —— CC Switch 运行中 ——
        policy_box = QGroupBox("CC Switch 正在运行时")
        policy_layout = QVBoxLayout(policy_box)
        self.policy_prompt = QRadioButton("先提示我，由我决定（关闭好了继续 / 直接改 / 取消）")
        self.policy_prompt.setToolTip(
            "每次同步前检测到 CC Switch 正在运行就先问一次，不会自己动它的数据库"
        )
        self.policy_direct = QRadioButton("不提示，直接改数据库（更快，可能被正在运行的 CC Switch 覆盖）")
        self.policy_direct.setToolTip(
            "不弹提醒直接写；CC Switch 之后可能用内存里的旧数据把这次修改覆盖回去"
        )
        if settings.ccswitch_running_policy == CCSWITCH_POLICY_DIRECT:
            self.policy_direct.setChecked(True)
        else:
            self.policy_prompt.setChecked(True)
        policy_layout.addWidget(self.policy_prompt)
        policy_layout.addWidget(self.policy_direct)
        targets_layout.addWidget(policy_box)
        targets_layout.addStretch(1)

        # —— 登录与安全 ——
        login_box = QGroupBox("自动登录（默认关闭）")
        login_form = QFormLayout(login_box)
        self.auto_login_check = QCheckBox("允许在清华域名下自动填充并提交登录信息")
        self.auto_login_check.setChecked(settings.auto_login_enabled)
        self.auto_login_check.setToolTip(
            "遇到验证码、二次验证、页面结构变化时不会绕过，程序会暂停并打开登录窗口提醒你接手。"
        )
        login_form.addRow(self.auto_login_check)
        self.login_username_edit = QLineEdit(self._saved_username)
        self.login_username_edit.setPlaceholderText("学号或统一身份认证用户名")
        login_form.addRow("用户名", self.login_username_edit)
        self.login_password_edit = QLineEdit()
        self.login_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_password_edit.setPlaceholderText(
            "留空表示保留 Credential Manager 里已保存的密码"
        )
        login_form.addRow("密码", self.login_password_edit)
        self.clear_login_button = QPushButton("删除已保存凭据")
        self.clear_login_button.clicked.connect(self._clear_saved_login)
        login_form.addRow(self.clear_login_button)
        security_layout.addWidget(login_box)

        verify_box = QGroupBox("Token 校验")
        verify_layout = QVBoxLayout(verify_box)
        self.chat_verify_check = QCheckBox("允许发送最小对话请求做兜底校验（可能消耗少量额度）")
        self.chat_verify_check.setChecked(settings.chat_verification_enabled)
        verify_layout.addWidget(self.chat_verify_check)
        security_layout.addWidget(verify_box)
        security_layout.addStretch(1)

        # —— 备份 ——
        backup_box = QGroupBox("备份与还原")
        backup_layout = QVBoxLayout(backup_box)
        keep_row = QHBoxLayout()
        self.keep_spin = QSpinBox()
        self.keep_spin.setRange(1, 200)
        self.keep_spin.setValue(settings.backup_keep)
        keep_row.addWidget(QLabel("每个文件保留备份份数"))
        keep_row.addWidget(self.keep_spin)
        keep_row.addStretch(1)
        backup_layout.addLayout(keep_row)
        tool_row = QHBoxLayout()
        self.manual_backup_button = QPushButton("立即备份")
        self.manual_backup_button.clicked.connect(self._manual_backup)
        self.preflight_button = QPushButton("校验写入内容")
        self.preflight_button.clicked.connect(self._run_preflight)
        self.log_button = QPushButton("打开日志")
        self.log_button.clicked.connect(self._open_log)
        self.restore_button = QPushButton("还原到首次修改前")
        self.restore_button.clicked.connect(self._restore)
        for button in (
            self.manual_backup_button,
            self.preflight_button,
            self.log_button,
            self.restore_button,
        ):
            tool_row.addWidget(button)
        tool_row.addStretch(1)
        backup_layout.addLayout(tool_row)
        self.paste_button = QPushButton("手动粘贴 Token…")
        self.paste_button.clicked.connect(self._paste_token)
        token_row = QHBoxLayout()
        token_row.addWidget(self.paste_button)
        token_row.addStretch(1)
        backup_layout.addLayout(token_row)
        maintenance_page_layout.addWidget(backup_box)

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
        maintenance_page_layout.addWidget(maintenance_box)
        maintenance_page_layout.addStretch(1)

        # —— 更多 ——
        more_box = QGroupBox("更多")
        more_form = QFormLayout(more_box)

        self.startup_check = QCheckBox("开机启动（最小化到托盘）")
        self.startup_check.setChecked(settings.launch_at_startup)
        more_form.addRow(self.startup_check)

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
            "自动创建 provider 时，默认模型优先用哪个 id（要能在站点清单里找到；留空=用第一个）"
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

        utility_row = QHBoxLayout()
        self.data_dir_button = QPushButton("打开数据目录")
        self.data_dir_button.clicked.connect(self._open_data_dir)
        self.about_button = QPushButton("关于…")
        self.about_button.clicked.connect(self._show_about)
        utility_row.addWidget(self.data_dir_button)
        utility_row.addWidget(self.about_button)
        utility_row.addStretch(1)
        more_form.addRow("工具", utility_row)

        advanced_layout.addWidget(more_box)

        note = QLabel(
            f"运行数据目录：{paths.data_root()}\n"
            "Token、日志、备份与浏览器会话都只保存在本机这个目录里，不会上传到任何服务器。"
        )
        note.setWordWrap(True)
        note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        advanced_layout.addWidget(note)
        advanced_layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        buttons.rejected.connect(self.reject)
        # 按钮固定在窗口底部，不跟着内容一起滚
        self.outer.addWidget(buttons)
        self._restore_window_size(saved_size)

    # —— 小工具 ——

    def _restore_window_size(self, saved_size: Sequence[int] | None) -> None:
        width, height = DEFAULT_DIALOG_WIDTH, DEFAULT_DIALOG_HEIGHT
        if saved_size is not None and len(saved_size) == 2:
            try:
                saved_width, saved_height = int(saved_size[0]), int(saved_size[1])
            except (TypeError, ValueError):
                pass
            else:
                if saved_width > 0 and saved_height > 0:
                    width, height = saved_width, saved_height
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, max(MIN_DIALOG_WIDTH, available.width() - 40))
            height = min(height, max(MIN_DIALOG_HEIGHT, available.height() - 40))
        self.resize(max(MIN_DIALOG_WIDTH, width), max(MIN_DIALOG_HEIGHT, height))

    def _emit_window_size(self) -> None:
        self.window_size_saved.emit(self.width(), self.height())

    def done(self, result: int) -> None:  # noqa: D102 - QDialog 约定
        self._emit_window_size()
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        self._emit_window_size()
        super().closeEvent(event)

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

    def _validate(self) -> bool:
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
                return False
        return True

    def _store_login_if_needed(self) -> bool:
        if not self.auto_login_check.isChecked():
            return True
        username = self.login_username_edit.text().strip()
        password = self.login_password_edit.text()
        if not username:
            QMessageBox.warning(self, "自动登录", "启用自动登录时必须填写用户名。")
            return False
        try:
            existing = credentials.load_login()
        except OSError as exc:
            QMessageBox.warning(self, "自动登录", f"无法读取 Windows 凭据：{exc}")
            return False
        if not password and (
            existing is None or existing.username.strip() != username
        ):
            QMessageBox.warning(
                self,
                "自动登录",
                "没有找到这个用户名对应的已保存密码，请填写密码后再应用。",
            )
            return False
        if password:
            try:
                credentials.save_login(username, password)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "自动登录", f"保存到 Windows Credential Manager 失败：{exc}")
                return False
            self.login_password_edit.clear()
            self._saved_username = username
        return True

    def apply(self) -> bool:
        """Apply without closing; returns False when validation failed."""

        if not self._validate() or not self._store_login_if_needed():
            return False
        self.apply_to(self.settings)
        parent = self.parent()
        if parent is not None and hasattr(parent, "apply_settings_from_dialog"):
            if not parent.apply_settings_from_dialog(self.settings):
                return False
        self.settings_applied.emit()
        return True

    def accept(self) -> None:  # noqa: D102 - QDialog 约定
        if self.apply():
            super().accept()

    # —— 动作 ——

    def _clear_saved_login(self) -> None:
        try:
            deleted = credentials.delete_login()
        except OSError as exc:
            QMessageBox.warning(self, "自动登录", f"删除 Windows 凭据失败：{exc}")
            return
        self.login_password_edit.clear()
        self._saved_username = ""
        self.auto_login_check.setChecked(False)
        QMessageBox.information(
            self,
            "自动登录",
            "已删除保存的凭据。" if deleted else "Windows 凭据库里没有保存过登录信息。",
        )

    def _call_parent(self, method: str) -> bool:
        parent = self.parent()
        callback = getattr(parent, method, None) if parent is not None else None
        if callback is None:
            return False
        callback()
        return True

    def _manual_backup(self) -> None:
        self._call_parent("manual_backup")

    def _run_preflight(self) -> None:
        self._call_parent("check_preflight")

    def _open_log(self) -> None:
        self._call_parent("open_log")

    def _open_data_dir(self) -> None:
        self._call_parent("_open_data_dir")

    def _show_about(self) -> None:
        self._call_parent("show_about")

    def _restore(self) -> None:
        manager = BackupManager(paths.backups_dir(), keep=self.settings.backup_keep)
        entries = manager.original_entries()
        field_records = backup_ops.has_field_records()
        if not entries and not field_records:
            QMessageBox.information(self, "还原", "还没有任何备份，不需要还原。")
            return
        allow_legacy = False
        if not field_records:
            detail = "\n".join(
                f"· {Path(source).name} ← {Path(entry.backup).name}"
                for source, entry in entries.items()
            )
            confirm = QMessageBox.warning(
                self,
                "旧版整文件还原",
                "这次备份没有字段级变更日志，只能用旧版整文件覆盖：\n\n"
                + detail
                + "\n\n风险：程序写入后，你后来对同一文件做的其它修改也可能被覆盖。"
                "\n当前状态会先再备份一份。确定执行高风险还原吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
            allow_legacy = True
        else:
            confirm = QMessageBox.question(
                self,
                "按字段还原",
                "将只撤销本程序管理、且当前值仍等于本程序最后写入值的字段。\n"
                "你后来修改过的字段会保留，并在结果里报告冲突。继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        try:
            messages = backup_ops.restore_original(
                self.settings, allow_legacy_full_restore=allow_legacy
            )
        except OSError as exc:
            logger.error("还原失败：%s", exc)
            QMessageBox.warning(self, "还原结果", f"还原失败：{exc}")
            return
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

        defaults = AppSettings()  # 文案里的默认值直接从默认设置取，改了默认值这里跟着变
        if QMessageBox.question(
            self,
            "恢复默认设置",
            "将把设置恢复成默认值：\n"
            f"· 自动同步：{'开' if defaults.auto_sync_enabled else '关'}，"
            f"间隔 {defaults.sync_interval_minutes} 分钟，"
            f"启动时立即同步：{'开' if defaults.sync_on_start else '关'}\n"
            f"· 自动登录：{'开' if defaults.auto_login_enabled else '关'}；"
            f"DSH 凭据 / Provider：{'开' if defaults.enable_dsh_credentials else '关'} / "
            f"{'开' if defaults.enable_dsh_provider else '关'}\n"
            "· CC Switch 卡片 / 当前路由："
            f"{'开' if defaults.enable_ccswitch_card else '关'} / "
            f"{'开' if defaults.enable_ccswitch_activate else '关'}\n"
            f"· 同步前预检：{'开' if defaults.preflight_enabled else '关'}；"
            "CC Switch 运行中：先提示\n"
            f"· 备份保留 {defaults.backup_keep} 份；静默取 Token 超时 "
            f"{defaults.silent_capture_timeout_seconds} 秒；日志级别 {defaults.log_level}；"
            f"关窗{'最小化到托盘' if defaults.close_to_tray else '直接退出'}\n\n"
            "开机启动项（注册表）保持现状，不会被动。\n确定恢复吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        reset_to_defaults(self.settings)
        self._reset_widgets_to_defaults()
        if not self.apply():
            return
        logger.info("设置已恢复为默认值")
        QMessageBox.information(self, "恢复默认设置", "已恢复并应用默认设置。")
        self._offer_reset_private_dir()

    def _reset_widgets_to_defaults(self) -> None:
        """把对话框里的控件刷成当前 settings 的值（恢复默认后立刻能看到）。"""

        settings = self.settings
        self.auto_sync_check.setChecked(settings.auto_sync_enabled)
        self.interval_spin.setValue(settings.sync_interval_minutes)
        self.sync_on_start_check.setChecked(settings.sync_on_start)
        self.minimize_after_check.setChecked(settings.minimize_after_sync)
        self.startup_check.setChecked(settings.launch_at_startup)
        self.dsh_enabled.setChecked(settings.enable_dsh)
        self.dsh_credentials_check.setChecked(settings.enable_dsh_credentials)
        self.dsh_provider_check.setChecked(settings.enable_dsh_provider)
        self.dsh_profile_edit.setText(settings.dsh_profile)
        self.dsh_dir_edit.setText(settings.dsh_dir)
        self.ccswitch_enabled.setChecked(settings.enable_ccswitch)
        self.ccswitch_card_check.setChecked(settings.enable_ccswitch_card)
        self.ccswitch_activate_check.setChecked(settings.enable_ccswitch_activate)
        self.ccswitch_dir_edit.setText(settings.ccswitch_dir)
        # 别忘了这个：漏掉它的话，"恢复默认设置"把配置改成 False，
        # 对话框里却还勾着，点确定时又会写回 True，等于没恢复。
        self.refresh_siblings_check.setChecked(settings.ccswitch_refresh_sibling_cards)
        if settings.ccswitch_running_policy == CCSWITCH_POLICY_DIRECT:
            self.policy_direct.setChecked(True)
        else:
            self.policy_prompt.setChecked(True)
        self.keep_spin.setValue(settings.backup_keep)
        self.auto_login_check.setChecked(settings.auto_login_enabled)
        self.chat_verify_check.setChecked(settings.chat_verification_enabled)
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
        parent = self.parent()
        if parent is not None and hasattr(parent, "_busy") and parent._busy():
            QMessageBox.information(
                self,
                "恢复默认设置",
                "设置已经恢复默认值了；正在同步或正在取 Token，private 位置等它结束再改。",
            )
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

        # 走和「更改位置…」同一条流程：先放开浏览器档案、日志句柄、单实例锁，再搬
        # （不然 browser-profile / logs 会被占着，跟用户之前遇到的问题一样）
        prepare = getattr(parent, "_prepare_for_data_move", None)
        after = getattr(parent, "_after_data_move", None)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, message = data_location.move_private_dir(
                self,
                paths.default_private_dir(),
                prepare=prepare,
                after=after,
            )
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            return  # move_private_dir 里已经提示过原因
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
        settings.sync_interval_minutes = self.interval_spin.value()
        settings.auto_sync_enabled = self.auto_sync_check.isChecked()
        settings.sync_on_start = self.sync_on_start_check.isChecked()
        settings.minimize_after_sync = self.minimize_after_check.isChecked()
        settings.launch_at_startup = self.startup_check.isChecked()
        settings.enable_dsh = self.dsh_enabled.isChecked()
        settings.enable_dsh_credentials = self.dsh_credentials_check.isChecked()
        settings.enable_dsh_provider = self.dsh_provider_check.isChecked()
        settings.dsh_profile = self.dsh_profile_edit.text().strip()
        settings.enable_ccswitch = self.ccswitch_enabled.isChecked()
        settings.enable_ccswitch_card = self.ccswitch_card_check.isChecked()
        settings.enable_ccswitch_activate = self.ccswitch_activate_check.isChecked()
        settings.dsh_dir = self.dsh_dir_edit.text().strip()
        settings.ccswitch_dir = self.ccswitch_dir_edit.text().strip()
        settings.ccswitch_refresh_sibling_cards = self.refresh_siblings_check.isChecked()
        settings.ccswitch_running_policy = (
            CCSWITCH_POLICY_DIRECT if self.policy_direct.isChecked() else CCSWITCH_POLICY_PROMPT
        )
        settings.backup_keep = self.keep_spin.value()
        settings.auto_login_enabled = self.auto_login_check.isChecked()
        settings.chat_verification_enabled = self.chat_verify_check.isChecked()
        settings.preflight_enabled = self.preflight_check.isChecked()
        settings.close_to_tray = self.close_to_tray_check.isChecked()
        settings.silent_capture_timeout_seconds = self.silent_timeout_spin.value()
        settings.log_level = self.log_level_combo.currentText()
        text = self.default_model_combo.currentText().strip()
        settings.default_model_id = "" if text == str(DEFAULT_MODELS[0]["id"]) else text
        settings.normalize()

