"""private 位置与桌面提醒的共用流程。

放在独立模块里，是因为两处都要用：
- 启动早期（主窗口还没建）：先提醒"程序在桌面"，别先在桌面把 private 建出来；
- 设置里「更改位置…」与启动提醒里的「选择 private 位置…」。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from . import paths

logger = logging.getLogger(__name__)


def ask_private_dir(parent: QWidget | None, current: Path | None = None) -> Path | None:
    """让用户选一个目录并校验；不合法就提示并返回 None。"""

    chosen = QFileDialog.getExistingDirectory(
        parent, "选择 private 数据文件夹", str(current or paths.data_root())
    )
    if not chosen:
        return None
    target = Path(chosen)
    ok, message = paths.validate_private_dir(target)
    if not ok:
        QMessageBox.warning(parent, "更改 private 位置", message)
        return None
    return target


def confirm_move(parent: QWidget | None, target: Path) -> bool:
    return (
        QMessageBox.question(
            parent,
            "更改 private 位置",
            "会把现在的运行数据（Token、设置、状态、日志、备份、浏览器会话）复制到：\n"
            f"{target}\n\n"
            "复制后需要重启程序才生效；旧位置里属于程序的文件会移到回收站，"
            "你自己放在那里的文件不会动。\n\n现在开始搬吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        == QMessageBox.StandardButton.Yes
    )


def move_private_dir(
    parent: QWidget | None,
    target: Path,
    *,
    prepare: Callable[[], None] | None = None,
    after: Callable[[], None] | None = None,
) -> tuple[bool, str]:
    """真正搬家。prepare/after 用来临时释放日志、浏览器等占用文件的资源。"""

    if prepare is not None:
        prepare()
    try:
        ok, message = paths.switch_private_root(target)
    finally:
        if after is not None:
            after()
    if not ok:
        QMessageBox.warning(parent, "更改 private 位置", message)
        return False, message
    logger.info(message)
    return True, message


def choose_private_dir(
    parent: QWidget | None,
    *,
    prepare: Callable[[], None] | None = None,
    after: Callable[[], None] | None = None,
    offer_restart: Callable[[str], None] | None = None,
) -> bool:
    """选位置 → 确认 → 搬迁 →（可选）问要不要现在重启。"""

    target = ask_private_dir(parent)
    if target is None:
        return False
    if not confirm_move(parent, target):
        return False
    ok, message = move_private_dir(parent, target, prepare=prepare, after=after)
    if not ok:
        return False
    if offer_restart is not None:
        offer_restart(message)
    return True


def desktop_notice(
    parent: QWidget | None,
    state,  # noqa: ANN001 - AppState
    *,
    prepare: Callable[[], None] | None = None,
    after: Callable[[], None] | None = None,
) -> bool:
    """程序直接放在桌面根目录时的提醒。

    返回 False 表示"用户选择退出程序去换本程序位置"（调用方应该退出启动流程）。
    """

    box = QMessageBox(parent)
    box.setWindowTitle("程序放在桌面上运行")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText("程序现在直接放在桌面上，继续运行会在桌面生成并使用 private 数据文件夹。")
    box.setInformativeText(
        f"private 里放着 Token、设置、备份和浏览器会话（默认位置：{paths.data_root()}）。\n"
        "请不要删除它，删了这些数据就没了。\n"
        "建议把它放到别的地方，例如 D 盘一个固定的文件夹。"
    )
    choose_button = box.addButton("选择 private 位置…", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("仍然继续", QMessageBox.ButtonRole.DestructiveRole)
    leave_button = box.addButton("退出程序去换本程序位置", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    clicked = box.clickedButton()

    if clicked is leave_button:
        logger.info("用户选择退出程序去换本程序位置（不记「已处理」，下次仍会提醒）")
        return False
    if clicked is None:
        # 按 ESC / 点标题栏 ×：用户没做选择。不能偷偷替他记成"已处理"
        # （否则下次启动就再也不提醒了），这次继续运行，下次启动照常提醒。
        logger.info("用户直接关掉了「程序在桌面」提醒：本次继续运行，不记「已处理」")
        return True
    if clicked is choose_button and not choose_private_dir(parent, prepare=prepare, after=after):
        return True  # 没选成：不记状态，下次启动再说

    state.desktop_note_ack = True
    try:
        state.save()
    except OSError as exc:  # pragma: no cover - 写不进去也要让程序继续跑
        logger.warning("记「程序在桌面」的提醒状态失败（下次还会提醒）：%s", exc)
    logger.info("已记住「程序在桌面」这件事，以后不再提醒")
    return True
