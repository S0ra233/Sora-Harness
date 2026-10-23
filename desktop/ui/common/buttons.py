"""按钮外观的状态变换；Qt仍负责点击、键盘、信号与启用状态。"""
from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton

from .motion import HoverMotion
from ..shell import theme


# 旧插件按objectName设置外观仍然兼容；新调用方直接传variant。
_LEGACY_VARIANTS = {
    **{name: "ghost" for name in ("settingsNav", "advancedToggle", "messageMore", "pluginNav",
                                  "cancelButton", "sidebarMore", "newChatButton", "questionOption")},
    "welcomeMode": "segment", "primaryButton": "primary", "questionSend": "primary",
    "chatSend": "accent", "dangerButton": "danger",
}
_VARIANTS = {"primary", "accent", "secondary", "outline", "ghost", "danger", "segment"}
_SIZES = {"small": (28, 10), "medium": (32, 12), "large": (40, 16), "icon": (32, 0)}


class MotionButton(QPushButton):
    """variant只决定外观，size可选；点击、权限与业务状态仍由调用方负责。"""

    def __init__(self, *args, variant: str | None = None, size: str | None = None,
                 shape: str | None = None, icon_position: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if variant is not None and variant not in _VARIANTS:
            raise ValueError("按钮外观类型不正确。")
        if size is not None and size not in _SIZES:
            raise ValueError("按钮尺寸类型不正确。")
        if shape not in {None, "rounded", "pill"} or icon_position not in {None, "left", "right"}:
            raise ValueError("按钮形状或图标位置不正确。")
        self._variant, self._size = variant, size
        self._shape, self._icon_position = shape, icon_position
        if size is not None:
            self.setMinimumHeight(_SIZES[size][0])
        self._motion = HoverMotion(self)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_variant(self, variant: str) -> None:
        """同一个确认按钮可改为危险操作，不必靠改名改变绘制。"""
        if variant not in _VARIANTS:
            raise ValueError("按钮外观类型不正确。")
        self._variant = variant
        self.update()

    def sizeHint(self) -> QSize:
        original = super().sizeHint()
        size = getattr(self, "_size", None)
        if size is None:
            return original
        height, padding = _SIZES[size]
        if size == "icon":
            return QSize(height, height)
        width = self.fontMetrics().horizontalAdvance(self.text()) + padding * 2
        if not self.icon().isNull():
            width += self.iconSize().width() + (6 if self.text() else 0)
        # 原Qt样式还可能带QSS内边距或平台最小宽度，不能被尺寸预设截掉。
        return QSize(max(width, original.width()), max(height, original.height(), self.fontMetrics().height() + 8))

    def paintEvent(self, event) -> None:
        name = self.objectName()
        variant = self._variant or _LEGACY_VARIANTS.get(name, "outline")
        ghost = variant in {"ghost", "segment"}
        primary, send, danger = variant == "primary", variant == "accent", variant == "danger"
        foreground = QColor("white" if primary or send else "#b91c1c" if danger else theme.TEXT)
        base = QColor(theme.SEND_COLOR if send else theme.ACTION if primary else "#fee2e2" if danger
                      else theme.CONTROL if variant == "secondary" else theme.BACKGROUND)
        hover = QColor("#1d4ed8" if send else "#333333" if primary else "#fecaca" if danger else theme.HOVER)
        t = self._motion.progress
        color = QColor(round(base.red() + (hover.red() - base.red()) * t),
                       round(base.green() + (hover.green() - base.green()) * t),
                       round(base.blue() + (hover.blue() - base.blue()) * t))
        if ghost:
            color = QColor(theme.BACKGROUND if variant == "segment" and self.isChecked() else theme.SELECTED if self.isChecked() else theme.HOVER)
            if not self.isChecked():
                color.setAlphaF(t)
        if self.isDown():
            color = QColor("#1e40af" if send else "#1f1f1f" if primary else theme.PRESSED)
        if not self.isEnabled():
            color = QColor(theme.SEND_DISABLED if send else "#a4a4a4" if primary else theme.CONTROL)
            foreground = QColor("white" if send or primary else theme.FAINT)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(color)
            border = variant == "outline"
            selected_mode = variant == "segment" and self.isChecked()
            painter.setPen(QPen(QColor(theme.BORDER if selected_mode else theme.CONTROL_BORDER), 1)
                           if border or selected_mode else Qt.PenStyle.NoPen)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            pill = self._shape == "pill" or self._shape is None and name in {"chatSend", "questionSend", "questionSkip", "questionOption", "welcomeMode"}
            radius = self.height() / 2 if pill else theme.BUTTON_RADIUS
            painter.drawRoundedRect(rect, radius, radius)
            option = QStyleOptionButton()
            self.initStyleOption(option)
            option.state &= ~QStyle.StateFlag.State_HasFocus
            option.palette.setColor(QPalette.ColorRole.ButtonText, foreground)
            # 只移动绘制内容；按压和回弹从当前偏移接续，不让布局几何发生跳动。
            option.state &= ~QStyle.StateFlag.State_Sunken
            painter.translate(0, self._motion.press_offset)
            trailing_icon = self._icon_position == "right" or self._icon_position is None and name == "messageMore"
            if trailing_icon:
                painter.setFont(self.font())
                painter.setPen(foreground)
                painter.drawText(rect.toRect().adjusted(2, 0, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text())
                x = round(rect.left()) + 2 + self.fontMetrics().horizontalAdvance(self.text()) + 6
                self.icon().paint(painter, QRect(x, (self.height() - 14) // 2, 14, 14))
            else:
                # 与Qt的CE_PushButton路线一致，先求内容区域，使QSS内边距用于标签和图标。
                option.rect = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
                self.style().drawControl(QStyle.ControlElement.CE_PushButtonLabel, option, painter, self)
            painter.translate(0, -self._motion.press_offset)
            if self._motion.keyboard_focus:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(theme.CONTROL_FOCUS), 1))
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 9, 9)
        finally:
            painter.end()
