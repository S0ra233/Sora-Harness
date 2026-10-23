"""真实用量的分层信息卡；悬停预览、点击固定，与压缩命令无连接。"""
from PySide6.QtCore import QEvent, QEasingCurve, QPoint, Property, QPropertyAnimation, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QToolButton, QVBoxLayout

from ..shell import theme
from .usage_sample import cache_percentage


class _UsagePopup(QFrame):
    hovered = Signal(bool)

    def __init__(self, parent) -> None:
        super().__init__(parent.window(), Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(200)
        self.setStyleSheet("QLabel { background: transparent; color: #b7bec8; font-size: 10px; }"
                          "QLabel#usageCaption { color: #b7bec8; font-size: 11px; }"
                          "QLabel#usageValue { color: white; font-size: 15px; font-weight: 600; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)
        self.label = QLabel("上下文窗口")
        self.label.setObjectName("usageCaption")
        self.value = QLabel()
        self.value.setObjectName("usageValue")
        self.metrics = QLabel()
        for label in (self.label, self.value, self.metrics):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            layout.addWidget(label)
        self.metrics.setTextFormat(Qt.TextFormat.RichText)

    def set_sample(self, input_tokens, output_tokens, window, cache_read, cache_write, reasoning, request_seconds,
                   cache_input, settled_requests, running, estimated, cache_scope) -> None:
        prefix = "约 " if estimated else ""
        self.value.setText(prefix + (f"{input_tokens / window:.0%} 已用" if window else f"{input_tokens:,} token"))
        cache = cache_percentage(cache_read, cache_input)
        speed = f"{output_tokens / request_seconds:.1f}" if output_tokens is not None and request_seconds is not None and request_seconds > 0 else "—"
        self.metrics.setText(f"{cache_scope}缓存：<b>{cache}</b>　{speed} TPS")
        basis = "压缩后有效上下文的程序估计，包含预设、工具、环境、摘要和保留原文" if estimated else "最近已完成请求的实际输入"
        self.label.setToolTip(f"{basis}：{input_tokens:,} token；" + (f"配置窗口 {window:,} token。" if window else "未配置窗口容量。"))
        scope = f"{'摘要' if cache_scope == '摘要' else '本轮'}{'已结算' if running else '合计'}{settled_requests}次请求"
        self.metrics.setToolTip(scope + "：缓存读取合计 ÷ 输入合计；缓存写入不算命中，未知不补0。\n"
            "成功压缩后仅统计本轮压缩边界之后的主请求，不含摘要请求。\n"
            "TPS = 最近一次成功请求的输出 token ÷ 该请求耗时，\n包含首次等待、可能包含推理，排除工具执行和失败重试。缺失数据为—。" +
            (f"\n真实缓存读取 {cache_read:,} / 总输入 {cache_input:,} token。" if cache_read is not None and cache_input is not None else ""))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.DARK_CARD_BG))
            painter.drawRoundedRect(QRectF(self.rect()), 12, 12)
        finally:
            painter.end()

    def enterEvent(self, event) -> None:
        self.hovered.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.hovered.emit(False)
        super().leaveEvent(event)


class ContextMeter(QToolButton):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(26, 26)
        self.setAccessibleName("查看最近请求用量，点击固定信息卡")
        self._input = self._window = self._sample = None
        self._pinned = False
        self._closing = False
        self._appearance = 0.0
        self._popup = _UsagePopup(self)
        self.destroyed.connect(self._popup.deleteLater)
        self._open_timer = QTimer(self)
        self._open_timer.setSingleShot(True)
        self._open_timer.setInterval(theme.TOOLTIP_DELAY_MS)
        self._open_timer.timeout.connect(self._open)
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.setInterval(theme.TOOLTIP_EXIT_DELAY_MS)
        self._close_timer.timeout.connect(self._close_unpinned)
        self._fade = QPropertyAnimation(self._popup, b"windowOpacity", self)
        self._fade.setDuration(theme.TOOLTIP_DURATION_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._fade.finished.connect(self._finish_close)
        self._appear = QPropertyAnimation(self, b"appearance", self)
        self._appear.setDuration(theme.FEEDBACK_APPEAR_DURATION_MS)
        self._appear.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._popup.hovered.connect(self._hover_popup)
        self.clicked.connect(self._show_details)
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)
        self.hide()

    @Property(float)
    def appearance(self) -> float:
        return self._appearance

    @appearance.setter
    def appearance(self, value: float) -> None:
        self._appearance = value
        self.update()

    def set_usage(self, input_tokens: int | None, output_tokens: int | None, window: int | None,
                  *, cache_read: int | None = None, cache_write: int | None = None,
                  reasoning: int | None = None,
                  request_seconds: float | None = None, cache_input: int | None = None,
                  settled_requests: int = 0, running: bool = False, estimated: bool = False, cache_scope: str = "整轮") -> None:
        sample = (input_tokens, output_tokens, window, cache_read, cache_write, reasoning, request_seconds,
                  cache_input, settled_requests, running, estimated, cache_scope)
        if sample == self._sample:
            return
        was_known = self._input is not None
        self._sample = sample
        self._input, self._window = input_tokens, window
        valid = type(input_tokens) is int and input_tokens >= 0
        self.setVisible(valid)
        if not valid:
            self._input = None
            self._appear.stop()
            self._appearance = 0.0
            self._close(animate=False)
            return
        self._popup.set_sample(*sample)
        # 隐藏后同一绑定仍可能复用已知样本，不能留下不可见的0透明度圆环。
        if was_known and self._appearance == 0.0:
            self._appearance = 1.0
        if not was_known:
            self._appear.setStartValue(0.0)
            self._appear.setEndValue(1.0)
            self._appear.start()
        if self._popup.isVisible():
            self._place_popup()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setOpacity(self._appearance)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            ring = QRectF(5, 5, 16, 16)
            painter.setPen(QPen(QColor(theme.BORDER), 1.5))
            painter.drawEllipse(ring)
            if self._input is not None and self._window:
                painter.setPen(QPen(QColor(theme.MUTED), 1.5))
                painter.drawArc(ring, 90 * 16, round(-360 * 16 * min(1.0, self._input / self._window)))
        finally:
            painter.end()

    def _place_popup(self) -> None:
        self._popup.adjustSize()
        area = self.screen().availableGeometry()
        point = self.mapToGlobal(QPoint(self.width() // 2 - self._popup.width() // 2, -self._popup.height() - 8))
        self._popup.move(max(area.left() + 4, min(point.x(), area.right() - self._popup.width() - 4)),
                         max(area.top() + 4, min(point.y(), area.bottom() - self._popup.height() - 4)))

    def _open(self) -> None:
        if self._input is None or not self.isVisible():
            return
        self._close_timer.stop()
        self._place_popup()
        self._fade.stop()
        self._closing = False
        if not self._popup.isVisible():
            self._popup.setWindowOpacity(0.0)
            self._popup.show()
        self._fade.setStartValue(self._popup.windowOpacity())
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _show_details(self) -> None:
        if self._pinned:
            self._close()
        else:
            self._pinned = True
            self._open_timer.stop()
            self._open()

    def _close(self, *, animate: bool = True) -> None:
        self._pinned = False
        self._open_timer.stop()
        self._close_timer.stop()
        self._fade.stop()
        self._closing = animate and self._popup.isVisible()
        if self._closing:
            self._fade.setStartValue(self._popup.windowOpacity())
            self._fade.setEndValue(0.0)
            self._fade.start()
        else:
            self._popup.hide()

    def _finish_close(self) -> None:
        if self._closing:
            self._closing = False
            self._popup.hide()

    def _close_unpinned(self) -> None:
        if not self._pinned:
            self._close()

    def _hover_popup(self, hovered: bool) -> None:
        if hovered:
            self._close_timer.stop()
        elif not self._pinned:
            self._close_timer.start()

    def enterEvent(self, event) -> None:
        self._close_timer.stop()
        if not self._pinned:
            self._open_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._open_timer.stop()
        if not self._pinned:
            self._close_timer.start()
        super().leaveEvent(event)

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape and self._popup.isVisible():
            self._close()
            return True
        if event.type() == QEvent.Type.MouseButtonPress and self._popup.isVisible():
            point = event.globalPosition().toPoint()
            anchor = QRect(self.mapToGlobal(QPoint()), self.size())
            if not anchor.contains(point) and not self._popup.frameGeometry().contains(point):
                self._close()
        elif event.type() == QEvent.Type.WindowDeactivate and watched is self.window():
            self._close()
        return super().eventFilter(watched, event)

    def hideEvent(self, event) -> None:
        self._appear.stop()
        self._appearance = 1.0 if self._input is not None else 0.0
        self._close(animate=False)
        super().hideEvent(event)
