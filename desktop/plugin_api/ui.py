"""辅助页面复用宿主样式与生命周期，不提供主聊天发送入口。"""
from ..ui.contracts import CloseBlock, UiContribution, UiPageContext, UiPageDefinition, UiServices
from ..ui.common.buttons import MotionButton
from ..ui.common.scrolling import SmoothScrollArea
from ..ui.common.surfaces import CardSurface, WhiteSurface
from ..ui.common.switch import SwitchToggle
from ..ui.common.auto_save import FormAutoSave
from ..ui.common.disclosure import AnimatedDisclosure
from ..ui.common.popup import CardPopup
from ..ui.common.dialogs import SurfaceDialog
from ..ui.common.widgets import ClickComboBox, ElidedLabel
from ..ui.shell import theme
from ..ui.chat.projection import DisplayRow

__all__ = ["CloseBlock", "UiContribution", "UiPageContext", "UiPageDefinition", "UiServices",
           "MotionButton", "SmoothScrollArea", "CardSurface", "WhiteSurface", "SwitchToggle", "FormAutoSave",
           "AnimatedDisclosure", "CardPopup", "SurfaceDialog", "ClickComboBox", "ElidedLabel", "DisplayRow", "theme"]
