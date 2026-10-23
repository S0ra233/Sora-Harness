"""按参考截图绘制轻量矢量图标；所有图标在 GUI 线程创建。"""
from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


DRAWINGS = {
    "question": '<path d="M21 11a9 9 0 0 1-9 9H8l-5 2 1-5a9 9 0 1 1 17-6Z"/><path d="M9 8a3 3 0 0 1 6 0c0 2-3 2-3 4m0 3h.01"/>',
    "left": '<path d="m15 6-6 6 6 6"/>',
    "undo": '<path d="M9 5 3 11l6 6M3 11h11a6 6 0 0 1 6 6"/>',
    "branch": '<circle cx="6" cy="5" r="2"/><circle cx="18" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10m0-4c8 0 12-2 12-6"/>',
    "edit": '<path d="m16 3 5 5-12 12H4v-5Z"/>',
    "think": '<path d="M9 4a4 4 0 0 0-6 4 4 4 0 0 0-1 7 4 4 0 0 0 7 5m6-16a4 4 0 0 1 6 4 4 4 0 0 1 1 7 4 4 0 0 1-7 5M9 4v16m6-16v16M5 9h4m6 0h4M5 15h4m6 0h4"/>',
    "home": '<path d="M3 10.5 12 3l9 7.5V21h-6v-7H9v7H3z"/>',
    "home-filled": '<path d="M3 10.5 12 3l9 7.5V21h-6v-7H9v7H3z" fill="currentColor"/>',
    "plugin": '<path d="M9 3h5v5a3 3 0 1 1 5 0h2v6h-5a3 3 0 1 1 0 5v2h-6v-5a3 3 0 1 1-5 0H3v-6h5a3 3 0 0 1 1-6Z"/>',
    "settings": '<path d="m9 3-.7 2.4-2.1 1.2-2.4-.6-2 3.4 1.7 1.8v2.4l-1.7 1.8 2 3.4 2.4-.6 2.1 1.2L9 22h4l.7-2.4 2.1-1.2 2.4.6 2-3.4-1.7-1.8v-2.4l1.7-1.8-2-3.4-2.4.6-2.1-1.2L13 3Z"/><circle cx="11" cy="12.5" r="3.3"/>',
    "sidebar": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    "new": '<path d="M20 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h6m4-1 6 6-9 9H6v-6Z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "close": '<path d="m6 6 12 12M18 6 6 18"/>',
    "folder": '<path d="M3 7V5h6l3 3h9v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Zm0 3h18"/>',
    "chat": '<path d="M21 14a3 3 0 0 1-3 3H8l-5 4V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3Z"/><path d="M7 8h10M7 12h6"/>',
    "chevron": '<path d="m7 10 5 5 5-5"/>',
    "right": '<path d="m9 6 6 6-6 6"/>',
    "pin": '<path d="m14 3 7 7-4 1-4 5-5-5 5-4Zm-4 11-7 7"/>',
    "pin-filled": '<path d="m14 3 7 7-4 1-4 5-5-5 5-4Z" fill="currentColor"/><path d="m10 14-7 7"/>',
    "more": '<circle cx="5" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.3" fill="currentColor" stroke="none"/>',
    "expand": '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
    "bolt": '<path d="m13 2-9 12h7l-1 8 10-13h-7Z"/>',
    "arrow": '<path d="M12 20V4m-6 6 6-6 6 6"/>',
    "stop": '<rect x="6.5" y="6.5" width="11" height="11" rx="1" fill="currentColor" stroke="none"/>',
    "paperclip": '<path d="m8 14 8-8a3 3 0 0 1 4 4L9 21a5 5 0 0 1-7-7L13 3a4 4 0 0 1 6 6L8 20"/>',
    "up": '<path d="m6 15 6-6 6 6"/>',
    "down": '<path d="m6 9 6 6 6-6"/>',
    "arrow-down": '<path d="M12 4v16m-6-6 6 6 6-6"/>',
    "copy": '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "refresh": '<path d="M4 8a8 8 0 1 1-1 7M4 3v5h5"/>',
    "terminal": '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 9 3 3-3 3m6 0h4"/>',
    "file": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Zm0 0v6h6"/>',
    "archive": '<rect x="3" y="3" width="18" height="5" rx="1.5"/><path d="M5 8v12h14V8m-10 5h6"/>',
    "trash": '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
    "compact": '<path d="M8 3H5v6l-2 3 2 3v6h3m8-18h3v6l2 3-2 3v6h-3M9 9h6m-6 4h6m-6 4h4"/>',
}


@lru_cache(maxsize=128)
def icon(name: str, color: str = "#737d88", size: int = 18) -> QIcon:
    """复用绘制结果，避免侧栏每次 repaint 都解析 SVG；准备1倍和2倍图。"""
    drawing = DRAWINGS[name].replace("currentColor", color)
    content = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
               f'fill="none" stroke="{color}" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">'
               + drawing + "</svg>")
    renderer = QSvgRenderer(QByteArray(content.encode("utf-8")))
    result = QIcon()
    for scale in (1, 2):
        pixmap = QPixmap(size * scale, size * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            renderer.render(painter, QRectF(0, 0, pixmap.width(), pixmap.height()))
        finally:
            painter.end()
        pixmap.setDevicePixelRatio(scale)
        result.addPixmap(pixmap)
    return result
