"""生成 assets/icon.png 与 assets/icon.ico。

图标 = 三家官方标识的组合：
  · OpenAI / Codex 花瓣（assets/logos/openai.svg）
  · DeepSeek 鲸鱼（assets/logos/deepseek.svg，DeepSeek Harness 所用标识）
  · CC Switch 星芒（assets/logos/ccswitch.png，取自 CC Switch 官方仓库 src-tauri/icons）
商标归各自所有者，仅用于标识本程序对接的对象；详见 assets/ATTRIBUTION.md。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

ASSETS = Path(__file__).resolve().parents[1] / "assets"
LOGOS = ASSETS / "logos"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
MASTER = 1024

BACKGROUND = QColor("#FFFFFF")
BORDER = QColor("#E2E8F0")
SEPARATOR = QColor("#FFFFFF")
CODEX_CIRCLE = QColor("#0F172A")
DSH_CIRCLE = QColor("#4D6BFE")
CCSWITCH_CIRCLE = QColor("#F8FAFC")


def render_svg(path: Path, size: int, tint: QColor | None = None) -> QImage:
    renderer = QSvgRenderer(str(path))
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    if tint is None:
        return image
    tinted = QImage(image.size(), QImage.Format.Format_ARGB32)
    tinted.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tinted)
    painter.drawImage(0, 0, image)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(tinted.rect(), tint)
    painter.end()
    return tinted


def draw_centered(painter: QPainter, image: QImage, box: QRectF) -> None:
    """等比缩放并居中。"""

    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return
    scale = min(box.width() / width, box.height() / height)
    target_width = width * scale
    target_height = height * scale
    x = box.x() + (box.width() - target_width) / 2
    y = box.y() + (box.height() - target_height) / 2
    painter.drawImage(QRectF(x, y, target_width, target_height), image)


def draw_badge(
    painter: QPainter,
    center: tuple[float, float],
    radius: float,
    fill: QColor,
    logo: QImage | None = None,
    logo_scale: float = 0.62,
) -> None:
    painter.setPen(QPen(SEPARATOR, radius * 0.10))
    painter.setBrush(fill)
    painter.drawEllipse(QRectF(center[0] - radius, center[1] - radius, radius * 2, radius * 2))
    if logo is None:
        return
    inner = radius * 2 * logo_scale
    box = QRectF(center[0] - inner / 2, center[1] - inner / 2, inner, inner)
    draw_centered(painter, logo, box)


def build_master() -> QImage:
    canvas = QImage(MASTER, MASTER, QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    # 底板
    border = MASTER * 0.012
    painter.setPen(QPen(BORDER, border))
    painter.setBrush(BACKGROUND)
    painter.drawRoundedRect(
        QRectF(border / 2, border / 2, MASTER - border, MASTER - border),
        MASTER * 0.22,
        MASTER * 0.22,
    )

    codex_logo = render_svg(LOGOS / "openai.svg", 512, QColor("#FFFFFF"))
    dsh_logo = render_svg(LOGOS / "deepseek.svg", 512, QColor("#FFFFFF"))
    ccswitch_logo = QImage(str(LOGOS / "ccswitch.png"))

    radius = MASTER * 0.215
    draw_badge(painter, (MASTER * 0.5, MASTER * 0.295), radius, CODEX_CIRCLE, codex_logo, 0.60)
    draw_badge(painter, (MASTER * 0.295, MASTER * 0.705), radius, DSH_CIRCLE, dsh_logo, 0.68)
    draw_badge(
        painter,
        (MASTER * 0.705, MASTER * 0.705),
        radius,
        CCSWITCH_CIRCLE,
        None if ccswitch_logo.isNull() else ccswitch_logo,
        0.80,
    )

    painter.end()
    return canvas


def png_bytes(image: QImage) -> bytes:
    payload = QByteArray()  # 必须持有引用，否则 QBuffer 指向的内存会被回收
    buffer = QBuffer(payload)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(payload)


def scaled_png(master: QImage, size: int) -> bytes:
    scaled = master.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    return png_bytes(scaled)


def write_ico(payloads: list[tuple[int, bytes]], path: Path) -> None:
    """手写 ICO 容器（Windows 支持内嵌 PNG）。"""

    header = struct.pack("<HHH", 0, 1, len(payloads))
    offset = len(header) + 16 * len(payloads)
    entries = bytearray()
    body = bytearray()
    for size, data in payloads:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        body += data
        offset += len(data)
    path.write_bytes(header + bytes(entries) + bytes(body))


def main() -> int:
    application = QApplication(sys.argv)  # noqa: F841 - QImage/QPainter 需要 QApplication
    ASSETS.mkdir(parents=True, exist_ok=True)

    master = build_master()
    master.save(str(ASSETS / "icon-master.png"), "PNG")
    (ASSETS / "icon.png").write_bytes(scaled_png(master, 256))
    write_ico([(size, scaled_png(master, size)) for size in ICON_SIZES], ASSETS / "icon.ico")
    print(f"已生成 {ASSETS / 'icon.png'}、{ASSETS / 'icon.ico'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

