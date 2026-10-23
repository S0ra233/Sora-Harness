"""共用模型与强度卡片；已有会话的选项通过控制器用于下一轮。"""
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QDialogButtonBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QSizePolicy, QStyle, QStyleOptionViewItem, QToolButton, QVBoxLayout,
)
from ..common.motion import HoverMotion
from ..common.scrolling import ScrollMotion
from ..common.switch import SwitchToggle

from ...llm.model_catalog import ModelCatalog, ModelEntry, ModelTarget
from ...llm.models import ModelConfig
from ...llm.reasoning import available_efforts, is_disabled_effort, resolve_effort
from ..common.popup import CardPopup
from ...llm.validation import resolve_model_config
from ...settings import load_model_catalog
from ...runtime.conversations import ConversationSettings
from ..common.dialogs import SurfaceDialog
from ..common.icons import icon
from ..common.widgets import ChoiceDelegate, ElidedLabel, IconGlyph
from ..shell import theme
from .effort_slider import EffortSlider


EFFORT_LABELS = {"none": "关闭思考", "minimal": "轻量", "low": "低", "medium": "中", "high": "高",
                 "xhigh": "极高", "max": "最高", "ultra": "超高"}


def effort_label(value: str | None) -> str:
    return EFFORT_LABELS.get(value, value) if value is not None else "关闭"


_GROUP_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_EXPANDED_ROLE = _GROUP_ROLE + 1


class _ModelDelegate(ChoiceDelegate):
    """提供商是小标题抽屉；模型行沿用右侧勾号，缩小行高与灰色高亮。"""

    def paint(self, painter, option, index) -> None:
        if index.data(_GROUP_ROLE) is None:
            super().paint(painter, option, index)
            return
        prepared = QStyleOptionViewItem(option)
        self.initStyleOption(prepared, index)
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = option.rect.adjusted(4, 1, -4, -1)
            if option.state & (QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Selected):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(theme.HOVER))
                painter.drawRoundedRect(rect, 7, 7)
            painter.setFont(prepared.font)
            painter.setPen(QColor(theme.MUTED))
            text_rect = rect.adjusted(12, 0, -24, 0)
            text = prepared.fontMetrics.elidedText(prepared.text, Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, text)
            glyph = "chevron" if index.data(_EXPANDED_ROLE) else "right"
            icon(glyph, theme.MUTED, 11).paint(painter, QRect(rect.right() - 19, rect.center().y() - 5, 11, 11))
        finally:
            painter.restore()

    def sizeHint(self, option, index) -> QSize:
        return QSize(220, 26 if index.data(_GROUP_ROLE) is not None else 32)


@dataclass(frozen=True)
class ModelSelection:
    config: ModelConfig
    effort: str | None
    model_id: str | None = None
    connection_id: str | None = None
    label: str | None = None
    connection_label: str | None = None

    @property
    def target(self) -> ModelTarget:
        return ModelTarget(self.config, self.model_id, self.connection_id, self.label, self.connection_label)


_Popup = CardPopup


class ModelPicker(QToolButton):
    """两层小面板；拖动只改变面板标题，底部触发按钮在关闭后更新。"""

    selection_changed = Signal()

    def __init__(self, catalog: ModelCatalog, has_tools: bool, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("modelButton")
        self._compact = 0
        self._motion = HoverMotion(self)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(138)
        self.setMaximumWidth(220)
        self.setFixedHeight(30)
        button_layout = QHBoxLayout(self)
        button_layout.setContentsMargins(10, 0, 9, 0)
        button_layout.setSpacing(5)
        self._caption = ElidedLabel()
        self._caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._effort_caption = QLabel()
        self._effort_caption.setTextFormat(Qt.TextFormat.PlainText)
        self._effort_caption.setStyleSheet(f"color: {theme.MUTED}; font-size: 12px;")
        self._effort_caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        button_layout.addStretch(1)
        button_layout.addWidget(self._caption)
        button_layout.addWidget(self._effort_caption)
        button_layout.addWidget(IconGlyph("chevron", size=13, color=theme.FAINT))
        button_layout.addStretch(1)
        self._catalog, self._has_tools = catalog, has_tools
        self.model_id = catalog.active_model
        self._selection_entry = catalog.models.get(self.model_id)
        self.effort: str | None = None
        self._choices: tuple[str, ...] = ()
        self._last_effort: str | None = None
        self._popup = _Popup(self, "modelPopup")
        self._popup.bind_toggle(self._open)
        self.destroyed.connect(self._popup.deleteLater)
        self._popup.setFixedSize(260, 116)
        layout = QVBoxLayout(self._popup)
        layout.setContentsMargins(18, 12, 18, 14)
        layout.setSpacing(5)
        self._strength = QLabel()
        self._strength.setObjectName("effortTitle")
        self._strength.setFixedHeight(24)
        self._strength.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._effort_enabled = SwitchToggle("开启强度设置；关闭时不发送effort")
        strength_row = QHBoxLayout()
        strength_row.addSpacing(self._effort_enabled.width())
        strength_row.addWidget(self._strength, 1)
        strength_row.addWidget(self._effort_enabled)
        self._model_link = QToolButton()
        self._model_link.setObjectName("effortModelLink")
        self._model_link.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._model_link.setFixedHeight(18)
        self._model_link.setMinimumWidth(0)
        self._model_link.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._model_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self._slider = EffortSlider()
        self._effort_note = QLabel()
        self._effort_note.setObjectName("effortNote")
        self._effort_note.setFixedHeight(30)
        self._effort_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._effort_note.setWordWrap(True)
        layout.addLayout(strength_row)
        layout.addWidget(self._model_link)
        layout.addWidget(self._slider)
        layout.addWidget(self._effort_note)
        self._models = _Popup(self, "modelListPopup")
        self._models.bind_toggle(self._open)
        self.destroyed.connect(self._models.deleteLater)
        self._models.resize(260, 100)
        model_layout = QVBoxLayout(self._models)
        model_layout.setContentsMargins(12, 14, 12, 12)
        model_layout.setSpacing(6)
        model_title = QLabel("选择模型")
        model_title.setFixedHeight(20)
        model_title.setStyleSheet(f"color: {theme.TEXT}; font-size: 12px; padding-left: 12px;")
        model_layout.addWidget(model_title)
        self._snapshot_note = QLabel()
        self._snapshot_note.setWordWrap(True)
        self._snapshot_note.setStyleSheet(f"color: {theme.MUTED}; font-size: 11px; padding: 2px 12px;")
        self._snapshot_note.hide()
        model_layout.addWidget(self._snapshot_note)
        self._list = QListWidget()
        self._list.motion = ScrollMotion(self._list)
        self._list.setMouseTracking(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._items: dict[str, QListWidgetItem] = {}
        self._list.setItemDelegate(_ModelDelegate(self._selected_model_row, self._list, check_right=True))
        model_layout.addWidget(self._list)
        self._groups: dict[str, tuple[QListWidgetItem, list[QListWidgetItem]]] = {}
        self._model_groups: dict[str, str] = {}
        self._rebuild_models()
        self.clicked.connect(self._open)
        self._model_link.clicked.connect(self._open_models)
        self._slider.valueChanged.connect(self._choose_effort)
        self._effort_enabled.toggled.connect(self._toggle_effort)
        self._list.itemClicked.connect(self._choose_model)
        self._list.installEventFilter(self)
        self._popup.closed.connect(self._update_label)
        self._models.closed.connect(self._update_label)
        self._reset_choices()

    def set_compact(self, level: int) -> None:
        """只调整标签密度，不改模型、强度和已有连接快照。"""
        if level == self._compact:
            return
        self._compact = level
        self.setMinimumWidth((138, 96, 72)[level])
        self.setMaximumWidth((220, 180, 140)[level])
        self._effort_caption.setVisible(level == 0 and self._selection_entry is not None and not self._popup.is_open)
        self.updateGeometry()

    def set_catalog(self, catalog: ModelCatalog) -> None:
        """欢迎页跟随最新候选；已有会话覆盖此方法以保留连接快照。"""
        previous = self.effort
        self._catalog = catalog
        if self.model_id not in catalog.models:
            self.model_id = catalog.active_model
        self._selection_entry = catalog.models.get(self.model_id)
        self._rebuild_models()
        self._reset_choices()
        self.restore_effort(previous if previous in self._choices else None)

    def _rebuild_models(self) -> None:
        expanded = {key: bool(header.data(_EXPANDED_ROLE)) for key, (header, _) in self._groups.items()}
        self._models.dismiss()
        self._list.clear()
        self._items.clear()
        self._groups.clear()
        self._model_groups.clear()
        groups = {}
        for key, entry in self._catalog.models.items():
            group = entry.connection_id or entry.config.provider_kind
            groups.setdefault(group, []).append((key, entry))
        for group, entries in groups.items():
            header = QListWidgetItem(entries[0][1].connection_label or entries[0][1].config.provider_kind)
            header.setData(_GROUP_ROLE, group)
            font = QFont(self.font())
            font.setPixelSize(11)
            font.setBold(False)
            header.setFont(font)
            self._list.addItem(header)
            items = []
            for key, entry in entries:
                item = QListWidgetItem(entry.label)
                item.setData(Qt.ItemDataRole.UserRole, key)
                item.setToolTip(f"{entry.config.provider_kind} · {entry.config.model}")
                if key == self.model_id and self._selection_entry is not None and entry.config != self._selection_entry.config:
                    item.setToolTip("当前会话保留旧连接；重新选择此项采用新版。")
                model_font = QFont(self.font())
                model_font.setPixelSize(12)
                item.setFont(model_font)
                self._list.addItem(item)
                self._items[key] = item
                self._model_groups[key] = group
                items.append(item)
            self._groups[group] = (header, items)
        self._reset_groups()
        for key, value in expanded.items():
            if key in self._groups:
                header, items = self._groups[key]
                header.setData(_EXPANDED_ROLE, value)
                for item in items:
                    item.setHidden(not value)
        self._fit_model_popup()

    def _selected_model_row(self) -> int:
        # 键盘焦点只移动候选；确认选择前，勾号仍表示当前真正选用的模型。
        return self._list.row(self._items[self.model_id]) if self.model_id in self._items else -1

    def _reset_groups(self) -> None:
        current = self._model_groups.get(self.model_id)
        if current is None and self._selection_entry is not None:
            current = self._selection_entry.connection_id or self._selection_entry.config.provider_kind
        for group, (header, items) in self._groups.items():
            header.setData(_EXPANDED_ROLE, group == current)
            for item in items:
                item.setHidden(group != current)
        self._fit_model_popup()

    def _fit_model_popup(self) -> None:
        height = sum(26 + (32 * len(items) if header.data(_EXPANDED_ROLE) else 0)
                     for header, items in self._groups.values())
        # 少量模型不留固定空白；很多模型保留列表内部滚动，不撑出窗口。
        extra = 44 if not self._snapshot_note.isHidden() else 0
        self._models.setFixedSize(260, min(360, max(84, height + 52 + extra)))

    def _reset_choices(self) -> None:
        entry = self._selection_entry
        profile = entry.config.reasoning if entry else None
        choices = tuple(level for level in available_efforts(profile, has_tools=self._has_tools)
                        if level is not None and not is_disabled_effort(profile, level))
        self._choices, self.effort = choices, None
        self._last_effort = "medium" if "medium" in choices else choices[0] if choices else None
        self._slider.blockSignals(True)
        self._slider.setRange(0, max(0, len(choices) - 1))
        self._slider.setValue(0)
        self._slider.blockSignals(False)
        self._slider.setVisible(len(choices) > 1)
        self._effort_note.setVisible(len(choices) <= 1)
        self._effort_note.setText("不发送强度参数" if not choices else "当前接口只有一个可用档位")
        self._effort_note.setToolTip("关闭强度设置不等于关闭模型思考；请求仍经过模型与工具组合校验。")
        self._sync_effort_controls()
        name = entry.label if entry else "没有已保存模型"
        self._model_link.ensurePolished()
        caption = self._model_link.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, self._popup.width() - 58)
        self._model_link.setText(caption + (" ›" if self._model_link.isEnabled() else ""))
        self._model_link.setToolTip(name + (" · 点击选择模型" if self._model_link.isEnabled() else ""))
        self._model_link.setAccessibleName("选择模型" if self._model_link.isEnabled() else "本会话模型")
        self.setEnabled(entry is not None)
        self._update_label()

    def _position(self, popup: _Popup) -> None:
        point = self.mapToGlobal(QPoint(self.width() - popup.width(), -popup.height() - 8))
        screen = self.screen().availableGeometry()
        area = screen.intersected(self.window().frameGeometry())
        if area.width() < popup.width() or area.height() < popup.height():
            area = screen
        x = min(max(point.x(), area.left()), area.right() - popup.width())
        y = point.y() if point.y() >= area.top() else self.mapToGlobal(QPoint(0, self.height() + 8)).y()
        popup.move(x, min(y, area.bottom() - popup.height()))
        popup.present()

    def _open(self) -> None:
        if self._popup.is_open or self._models.is_open:
            self._popup.dismiss(restore_focus=True)
            self._models.dismiss(restore_focus=True)
            return
        # 关闭另一层仍在淡出的卡片，再打开当前层，避免两层同时接收原按钮事件。
        self._models.hide()
        self.setText("选择强度")
        self._caption.setText("选择强度")
        self._effort_caption.hide()
        self._position(self._popup)

    def _open_models(self) -> None:
        self._popup.hide()
        if self.model_id in self._items:
            self._list.setCurrentItem(self._items[self.model_id])
            self._list.scrollToItem(self._items[self.model_id])
        self._position(self._models)
        self._list.setFocus(Qt.FocusReason.PopupFocusReason)

    def _choose_effort(self, index: int) -> None:
        if not self._effort_enabled.isChecked() or not self._choices:
            return
        previous = self.effort
        self.effort = self._choices[index]
        self._last_effort = self.effort
        self._sync_effort_controls()
        if previous != self.effort:
            self.selection_changed.emit()

    def _toggle_effort(self, enabled: bool) -> None:
        self.effort = self._last_effort if enabled else None
        self._sync_effort_controls()
        self.selection_changed.emit()

    def _sync_effort_controls(self) -> None:
        enabled = self.effort is not None
        if self._effort_enabled.isChecked() != enabled:
            self._effort_enabled.blockSignals(True)
            self._effort_enabled.setChecked(enabled)
            self._effort_enabled.blockSignals(False)
        self._effort_enabled.setEnabled(bool(self._choices))
        self._slider.setEnabled(self.effort is not None and len(self._choices) > 1)
        self._strength.setText(effort_label(self.effort))
        self._strength.setStyleSheet(f"color: {theme.ACCENT if self.effort is not None else theme.MUTED};")
        self._slider.setToolTip(effort_label(self.effort))
        if self._last_effort in self._choices:
            target = self._choices.index(self._last_effort)
            if self._slider.value() != target:
                self._slider.blockSignals(True)
                self._slider.setValue(target)
                self._slider.blockSignals(False)

    def _choose_model(self, item: QListWidgetItem) -> None:
        if item is None:
            return
        group = item.data(_GROUP_ROLE)
        if group is not None:
            expanded = not item.data(_EXPANDED_ROLE)
            item.setData(_EXPANDED_ROLE, expanded)
            for model in self._groups[group][1]:
                model.setHidden(not expanded)
            self._fit_model_popup()
            self._position(self._models)
            return
        model_id = item.data(Qt.ItemDataRole.UserRole)
        if model_id is None:
            return
        previous = self.effort
        self.model_id = model_id
        self._selection_entry = self._catalog.models[model_id]
        self._snapshot_note.hide()
        self._reset_choices()
        self.restore_effort(previous if previous in self._choices else None)
        self._reset_groups()
        self.selection_changed.emit()
        self._models.dismiss(restore_focus=True)

    def eventFilter(self, watched, event) -> bool:
        if watched is self._list and event.type() == QEvent.Type.KeyPress:
            item = self._list.currentItem()
            if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
                self._choose_model(item)
                return True
            if event.key() in {Qt.Key.Key_Left, Qt.Key.Key_Right} and item is not None:
                group = item.data(_GROUP_ROLE) or self._model_groups.get(item.data(Qt.ItemDataRole.UserRole))
                if group is not None:
                    header = self._groups[group][0]
                    desired = event.key() == Qt.Key.Key_Right
                    if bool(header.data(_EXPANDED_ROLE)) != desired:
                        self._choose_model(header)
                    if not desired:
                        self._list.setCurrentItem(header)
                return True
        return super().eventFilter(watched, event)

    def restore_selection(self, model_id: str | None, effort: str | None) -> None:
        if model_id not in self._catalog.models:
            return
        self.model_id = model_id
        self._selection_entry = self._catalog.models[model_id]
        self._reset_choices()
        self._reset_groups()
        self.restore_effort(effort)
        self._update_label()

    def _update_label(self) -> None:
        entry = self._selection_entry
        label = effort_label(self.effort)
        text = f"{entry.label} · {label}" if entry else "请先保存模型"
        if self.text() == text and self._caption.full_text == (entry.label if entry else "请先保存模型"):
            return
        self.setText(text)
        self._caption.setText(entry.label if entry else "请先保存模型")
        self._effort_caption.setText(label if entry else "")
        self._effort_caption.setVisible(entry is not None and self._compact == 0)
        self.setToolTip(self.text())
        self.setAccessibleName(self.text())
        self.updateGeometry()

    def restore_effort(self, effort: str | None) -> None:
        """控制器读回选择时不重新发出信号，也不打断正在操作的卡片。"""
        entry = self._selection_entry
        profile = entry.config.reasoning if entry else None
        if is_disabled_effort(profile, effort):
            effort = None
        if effort not in self._choices and profile is not None and effort in profile.effort_map:
            # 旧快照可能保留minimal等别名，滑条显示同一原生值的实际档位。
            native = profile.effort_map[effort]
            effort = next((choice for choice in self._choices if choice is not None
                           and profile.effort_map[choice] == native), effort)
        if (effort is None or effort in self._choices) and effort != self.effort:
            self.effort = effort
            if effort is not None:
                self._last_effort = effort
            self._sync_effort_controls()
        if not self._popup.isVisible():
            self._update_label()

    def sizeHint(self) -> QSize:
        if not hasattr(self, "_effort_caption"):
            return super().sizeHint()
        width = self._caption.sizeHint().width() + 42
        if not self._effort_caption.isHidden():
            width += self._effort_caption.sizeHint().width() + 5
        return QSize(min(self.maximumWidth(), max(self.minimumWidth(), width)), 30)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            base, hover, t = QColor(theme.CONTROL), QColor(theme.HOVER), self._motion.progress
            color = QColor(theme.PRESSED) if self.isDown() else QColor(
                round(base.red() + (hover.red() - base.red()) * t),
                round(base.green() + (hover.green() - base.green()) * t),
                round(base.blue() + (hover.blue() - base.blue()) * t))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(self.rect()), 15, 15)
            if self._motion.keyboard_focus:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QColor(theme.CONTROL_FOCUS))
                painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 15, 15)
        finally:
            painter.end()

    def selection(self) -> ModelSelection:
        entry = self._selection_entry
        if entry is None:
            raise ValueError("请先在设置中保存一个模型。")
        config = resolve_model_config(entry.config)
        resolve_effort(config.reasoning, self.effort, has_tools=self._has_tools)
        return ModelSelection(config, self.effort, self.model_id, entry.connection_id, entry.label, entry.connection_label)

    def hideEvent(self, event) -> None:
        if hasattr(self, "_models"):
            self._popup.hide()
            self._models.hide()
        super().hideEvent(event)


class NewConversationDialog(SurfaceDialog):
    def __init__(self, settings_path: Path, project_name: str | None, parent=None) -> None:
        super().__init__("新建项目对话" if project_name else "新建聊天", parent, width=420)
        self.selection: ModelSelection | None = None
        caption = QLabel(project_name or "普通聊天")
        caption.setTextFormat(Qt.TextFormat.PlainText)
        self.body.addWidget(caption)
        self._picker = ModelPicker(load_model_catalog(settings_path), project_name is not None)
        self.body.addWidget(self._picker)
        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.setTextFormat(Qt.TextFormat.PlainText)
        self.body.addWidget(self._error)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("创建")
        self.buttons.accepted.connect(self._confirm)

    def _confirm(self) -> None:
        try:
            self.selection = self._picker.selection()
        except ValueError as exc:
            self._error.setText(str(exc))
            return
        self.accept()


class SavedModelButton(ModelPicker):
    """候选目录与会话快照分别保存；主动选择才改变下一独立轮次。"""

    def __init__(self, parent=None) -> None:
        super().__init__(ModelCatalog(), False, parent)
        self._target = None
        self._saved_effort = None
        self._model_link.setEnabled(False)

    def set_catalog(self, catalog: ModelCatalog) -> None:
        if catalog == self._catalog:
            return
        self._catalog = catalog
        self._rebuild_models()
        self._model_link.setEnabled(bool(catalog.models))
        self._show_snapshot_note()
        self._reset_choices()
        self.restore_effort(self._saved_effort)

    def _show_snapshot_note(self) -> None:
        entry = self._catalog.models.get(self.model_id)
        detached = self._selection_entry is not None and (entry is None or entry.config != self._selection_entry.config)
        self._snapshot_note.setText("当前会话快照；重新选择模型后采用目录中的连接。")
        self._snapshot_note.setVisible(detached)
        self._fit_model_popup()

    def set_settings(self, settings: ConversationSettings | None, effort: str | None = None,
                     target: ModelTarget | None = None) -> None:
        target = (target or settings.model_target) if settings is not None else None
        has_tools = bool(settings.tool_names) if settings else False
        different_selection = (target is not None and (self._selection_entry is None
            or self._selection_entry.config != target.config
            or self._selection_entry.connection_id != target.connection_id
            or target.model_id is not None and self.model_id != target.model_id))
        if target != self._target or has_tools != self._has_tools or different_selection:
            self._target, self._has_tools = target, has_tools
            self.model_id = target.model_id if target else None
            if target is not None and self.model_id is None:
                self.model_id = next((key for key, entry in self._catalog.models.items() if entry.config == target.config), None)
            self._selection_entry = ModelEntry(target.label or target.config.model, target.config,
                                               target.connection_id, target.connection_label) if target else None
            self._reset_choices()
            self._reset_groups()
        self._saved_effort = effort
        self.restore_effort(effort)
        self._show_snapshot_note()
