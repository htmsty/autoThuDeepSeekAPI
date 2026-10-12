"""程序图标：优先用 assets 里的自绘图标，缺失时现画一个。"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from .. import paths

BG_COLOR = QColor("#1D4ED8")
ACCENT_COLOR = QColor("#10B981")
FG_COLOR = QColor("#FFFFFF")


def draw_icon(size: int = 256) -> QPixmap:
    """自绘融合图标：代码提示符（Codex）+ 切换箭头（CC Switch）+ 鲸尾（DSH）。"""

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    radius = size * 0.22
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(BG_COLOR)
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    # 切换箭头（右侧，绿）
    pen = QPen(ACCENT_COLOR, size * 0.075, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    arrow_x = size * 0.63
    painter.drawLine(QPointF(arrow_x, size * 0.36), QPointF(size * 0.86, size * 0.36))
    painter.drawLine(QPointF(arrow_x, size * 0.66), QPointF(size * 0.86, size * 0.66))
    painter.setBrush(ACCENT_COLOR)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPolygon(
        [
            QPointF(size * 0.82, size * 0.28),
            QPointF(size * 0.94, size * 0.36),
            QPointF(size * 0.82, size * 0.44),
        ]
    )
    painter.drawPolygon(
        [
            QPointF(size * 0.67, size * 0.58),
            QPointF(size * 0.55, size * 0.66),
            QPointF(size * 0.67, size * 0.74),
        ]
    )

    # 代码提示符 >_（左侧，白）
    pen = QPen(FG_COLOR, size * 0.085, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    prompt = QPainterPath()
    prompt.moveTo(size * 0.17, size * 0.34)
    prompt.lineTo(size * 0.36, size * 0.50)
    prompt.lineTo(size * 0.17, size * 0.66)
    painter.drawPath(prompt)
    painter.drawLine(QPointF(size * 0.40, size * 0.70), QPointF(size * 0.54, size * 0.70))

    # 鲸尾（底部中央，淡蓝），暗示 DeepSeek Harness
    pen = QPen(QColor(255, 255, 255, 190), size * 0.055, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    tail = QPainterPath()
    tail.moveTo(size * 0.50, size * 0.84)
    tail.cubicTo(size * 0.44, size * 0.86, size * 0.36, size * 0.84, size * 0.30, size * 0.94)
    tail.moveTo(size * 0.50, size * 0.84)
    tail.cubicTo(size * 0.56, size * 0.86, size * 0.64, size * 0.84, size * 0.70, size * 0.94)
    painter.drawPath(tail)

    painter.end()
    return pixmap


def app_icon() -> QIcon:
    icon_file = paths.asset("icon.png")
    if icon_file.exists():
        icon = QIcon(str(icon_file))
        if not icon.isNull():
            return icon
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(draw_icon(size))
    return icon

