"""直接绘制卡片和淡入薄层，不把可编辑控件放进离屏效果。"""
from PySide6.QtCore import Property, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from ..shell import theme


def paint_card(painter: QPainter, rect, *, radius: float = theme.POPUP_RADIUS, shadow: int = 5,
               background: str = theme.BACKGROUND) -> None:
    """调用方持有绘制器；投影只画外侧，不重新绘制子控件。"""
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for spread in range(shadow, 0, -1):
            painter.setBrush(QColor(30, 40, 52, 4))
            painter.drawRoundedRect(rect.adjusted(-spread, -spread + 2, spread, spread + 2),
                                    radius + spread, radius + spread)
        painter.setBrush(QColor(background))
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawRoundedRect(rect, radius, radius)
    finally:
        painter.restore()


class FadeVeil(QWidget):
    """白色或侧栏底色薄层消退；原控件仍按 Qt 正常绘制。"""

    def __init__(self, parent, *, color: str = theme.BACKGROUND) -> None:
        super().__init__(parent)
        self._color, self._opacity = color, 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    @Property(float)
    def opacity(self) -> float:
        return self._opacity

    @opacity.setter
    def opacity(self, value: float) -> None:
        self._opacity = value
        self.update()

    def paintEvent(self, event) -> None:
        color = QColor(self._color)
        color.setAlphaF(self._opacity)
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), color)
        finally:
            painter.end()


class WhiteSurface(QWidget):
    """子类页面也明确画白底，避免继承窗口的冰蓝灰背景。"""

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(theme.BACKGROUND))
        finally:
            painter.end()


def settings_column(page, *, spacing=20):
    """设置与插件共用的居中内容列；各页面仍拥有表单、滚动和保存职责。"""
    outer = QHBoxLayout(page)
    outer.setContentsMargins(*theme.SETTINGS_PAGE_MARGINS)
    outer.setSpacing(0)
    column = WhiteSurface(page)
    column.setMaximumWidth(theme.SETTINGS_CONTENT_WIDTH)
    body = QVBoxLayout(column)
    body.setContentsMargins(0, 0, 0, 0)
    body.setSpacing(spacing)
    outer.addStretch(1)
    outer.addWidget(column, 100)
    outer.addStretch(1)
    return body


class CardSurface(QWidget):
    """列表外壳留出圆角区域；内部 viewport 不再覆盖整个外框。"""

    def __init__(self, content: QWidget, parent=None, *, radius: int = theme.CARD_RADIUS,
                 padding: int = theme.CARD_PADDING) -> None:
        super().__init__(parent)
        self._radius = radius
        layout = QVBoxLayout(self)
        layout.setContentsMargins(padding, padding, padding, padding)
        layout.addWidget(content)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            paint_card(painter, QRectF(self.rect()).adjusted(1, 1, -1, -1), radius=self._radius, shadow=0)
        finally:
            painter.end()


class _SurfaceCorner(QWidget):
    """只覆盖外壳角落的几像素，子控件照常绘制，不捕获整页或添加离屏效果。"""

    def __init__(self, parent, right: bool, bottom: bool) -> None:
        super().__init__(parent)
        self._right, self._bottom = right, bottom
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

    def paintEvent(self, event) -> None:
        radius = self.width()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.translate(radius if self._right else 0, radius if self._bottom else 0)
            painter.scale(-1 if self._right else 1, -1 if self._bottom else 1)
            square, circle = QPainterPath(), QPainterPath()
            square.addRect(0, 0, radius, radius)
            circle.addEllipse(0, 0, radius * 2, radius * 2)
            painter.fillPath(square.subtracted(circle), QColor(theme.NAVIGATION))
        finally:
            painter.end()


class RoundedSurface(QWidget):
    """主页面共用外圆角；内部三色和分栏直线仍归各页面。"""

    def __init__(self, content: QWidget, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(content)
        self._corners = tuple(_SurfaceCorner(self, right, bottom)
                              for bottom in (False, True) for right in (False, True))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        radius = min(16, self.width() // 2, self.height() // 2)
        for corner in self._corners:
            corner.setGeometry(self.width() - radius if corner._right else 0,
                               self.height() - radius if corner._bottom else 0, radius, radius)
            corner.raise_()
