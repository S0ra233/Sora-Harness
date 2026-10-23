"""主应用的语义配色；插件业务界面仍可维护自己的布局和样式。"""

BACKGROUND = "#ffffff"
NAVIGATION = "#eff4f9"
NAVIGATION_SELECTED = "#e5e9ee"
SIDEBAR = "#fafbfd"
TEXT = "#1a1c1f"
TEXT_SECONDARY = TEXT
MUTED = "#737373"
FAINT = "#8e8e8e"
BORDER = "#e5e5e5"
HOVER = "#f0f0f0"
PRESSED = "#e2e2e2"
SELECTED = "#ececec"
SIDEBAR_SELECTED = "#edeff1"
USER_MESSAGE = "#e8f3ff"
SEARCH_MATCH = "#fff0b3"
TEXT_SELECTION = "#bfd9ff"
ACCENT = "#3a83f7"
ACTION = "#0d0d0d"
CONTROL = "#f4f4f4"
CONTROL_BORDER = "#d8dce2"
CONTROL_FOCUS = ACCENT
CONTENT_WIDTH = 880
SIDEBAR_WIDTH = 260
RAIL_WIDTH = 48
SEND_COLOR = ACCENT
SEND_DISABLED = "#c7d8f8"
BODY_FONT_SIZE = 13
# 字号缩小后保留截图约22px的阅读行距，不能连行距一起压紧。
BODY_LINE_HEIGHT = 170
LINK_COLOR = "#0066cc"
DARK_CARD_BG = "#1f2328"
SUCCESS = "#16a34a"
DIFF_ADDED = "#00a240"
DIFF_REMOVED = "#ba2623"
USER_COLLAPSE_LINES = 12
CODE_MAX_HEIGHT = 280
ERROR = "#b43b42"
TOOL_TEXT = "#737373"
TOOL_BACKGROUND = "#f9f9f9"
TOOL_BORDER = BORDER
TOOL_RADIUS = 12
TOOL_FONT_SIZE = 13
TOOL_LIST_MAX_HEIGHT = 360
TOOL_OUTPUT_MAX_HEIGHT = 240
TOOL_ANIMATION_FRAME_MS = 40
TOOL_SWEEP_DURATION_MS = 1600
DISCLOSURE_DURATION_MS = 150
SIDEBAR_GROUP_DURATION_MS = 80
SIDEBAR_WIDTH_DURATION_MS = 110
HOVER_DURATION_MS = 80
BUTTON_PRESS_DURATION_MS = 80
BUTTON_RELEASE_DURATION_MS = 120
# 控件按用途共用尺度；插件可复用控件，不必复制这些绘制常量。
BUTTON_RADIUS = 10
CARD_RADIUS = 16
CARD_PADDING = 12
SETTINGS_CONTENT_WIDTH = 880
SETTINGS_PAGE_MARGINS = (40, 40, 40, 32)
SETTINGS_NAV_SPACING = 4
POPUP_RADIUS = 14
DIALOG_RADIUS = 22
POPUP_ENTER_DURATION_MS = 100
POPUP_EXIT_DURATION_MS = 80
PAGE_FADE_DURATION_MS = 150
PANEL_ENTER_DURATION_MS = 120
PANEL_EXIT_DURATION_MS = 100
SWITCH_DURATION_MS = 120
CONTROL_SNAP_DURATION_MS = 120
FEEDBACK_APPEAR_DURATION_MS = 120
TOOLTIP_DURATION_MS = 80
TOOLTIP_DELAY_MS = 120
TOOLTIP_EXIT_DELAY_MS = 100
PANEL_MIN_CHAT_WIDTH = 330
PANEL_MIN_WIDTH = 300
PANEL_SPLIT_MIN_WIDTH = 750
PANEL_DEFAULT_RATIO = .45
PANEL_KEY_STEP = 24
COMPOSER_COMPACT_WIDTH = 440
COMPOSER_NARROW_WIDTH = 340
EFFORT_TITLE = ACCENT
EFFORT_FILL = ACCENT
EFFORT_TRACK = "#e5e5e5"
EFFORT_BORDER = "#e5e5e5"
EFFORT_DOT = "#9ca3af"
EFFORT_PASSED_DOT = "#ffffff"
TEXT_FAMILIES = ["Microsoft YaHei", "Microsoft YaHei UI", "Segoe UI", "PingFang SC"]


def reading_column_width(width: int) -> int:
    """正文与输入台共用居中阅读列，留白随画布宽度变化。"""
    gutter = min(140, max(24, round(width * 0.12)))
    return max(0, min(CONTENT_WIDTH, width - gutter * 2))


def shell_stylesheet() -> str:
    """只定位主应用控件，不用宽泛选择器覆盖插件的所有按钮。"""
    return f"""
    QMainWindow, QWidget#conversationWorkspace, QWidget#settingsPage, QWidget#pluginsPage {{ background: {BACKGROUND}; color: {TEXT}; }}
    QFrame#applicationRail {{ background: {NAVIGATION}; border: none; }}
    QWidget#applicationSurface {{ background: {NAVIGATION}; }}
    QToolButton#navigationButton {{ border: none; border-radius: 11px; padding: 0; background: transparent; }}
    QToolButton#navigationButton:hover {{ background: {SELECTED}; }}
    QToolButton#navigationButton:checked {{ background: {NAVIGATION_SELECTED}; }}
    QWidget#conversationSidebar {{ background: {SIDEBAR}; border: none; }}
    QWidget#conversationSidebar QTreeView {{ background: transparent; border: none; outline: none; }}
    QWidget#conversationSidebar QLineEdit {{ background: {CONTROL}; color: {TEXT}; border: none; border-radius: 10px; padding: 9px; selection-background-color: {TEXT_SELECTION}; selection-color: {TEXT}; }}
    QWidget#conversationSidebar QPushButton {{ border: none; border-radius: 9px; padding: 9px 11px; text-align: left; color: {TEXT}; }}
    QWidget#conversationSidebar QPushButton:hover, QWidget#conversationSidebar QToolButton:hover {{ background: {HOVER}; }}
    QWidget#conversationSidebar QPushButton:disabled {{ color: {MUTED}; }}
    QLabel#sidebarTitle {{ color: {TEXT}; font-size: 16px; font-weight: 600; }}
    QLabel#workspaceHeading {{ color: {TEXT}; font-size: 14px; font-weight: 500; }}
    QLabel#welcomeTitle {{ color: {TEXT}; font-size: 28px; font-weight: 400; }}
    QLabel#welcomeCaption {{ color: {FAINT}; font-size: 13px; }}
    QLabel#workspaceDetails {{ color: {MUTED}; }}
    QLabel#workspaceNotice {{ color: {ERROR}; }}
    QFrame#workspaceHeader {{ background: {BACKGROUND}; border: none; }}
    QToolButton#iconButton {{ border: none; border-radius: 8px; padding: 0; background: transparent; }}
    QToolButton#iconButton:hover {{ background: {HOVER}; }}
    QToolButton#iconButton:focus {{ background: {HOVER}; border: 1px solid {CONTROL_BORDER}; }}
    QWidget#chatPanel {{ background: {BACKGROUND}; color: {TEXT}; }}
    QWidget#composerDock {{ background: {BACKGROUND}; border: none; }}
    QScrollArea#transcriptScroll, QWidget#transcriptContent, QWidget#transcriptColumn {{ background: {BACKGROUND}; border: none; }}
    QScrollArea#turnNavigation, QWidget#turnMarks {{ background: {BACKGROUND}; border: none; }}
    QWidget#chatPanel QPlainTextEdit {{ border: none; background: transparent; color: {TEXT}; selection-background-color: {TEXT_SELECTION}; selection-color: {TEXT}; }}
    QWidget#chatPanel QLineEdit {{ border: 1px solid {BORDER}; border-radius: 7px; padding: 6px; background: {BACKGROUND}; color: {TEXT}; selection-background-color: {TEXT_SELECTION}; selection-color: {TEXT}; }}
    QFrame#chatComposer {{ border: 1px solid #e2e7eb; border-radius: 22px; background: {BACKGROUND}; }}
    QFrame#chatComposer[focused="true"] {{ border-color: #b4b4b4; }}
    QPushButton#chatSend {{ border: none; border-radius: 17px; color: white; background: {SEND_COLOR}; }}
    QPushButton#chatSend:hover {{ background: #1d4ed8; }}
    QPushButton#chatSend:pressed {{ background: #1e40af; }}
    QPushButton#chatSend:disabled {{ background: {SEND_DISABLED}; }}
    QToolButton#modelButton {{ background: {CONTROL}; color: {TEXT}; border: none; border-radius: 15px; padding: 0 10px; font-size: 12px; }}
    QToolButton#modelButton:hover {{ background: {HOVER}; }}
    QToolButton#modelButton QLabel {{ background: transparent; color: {TEXT}; font-size: 12px; }}
    QFrame#attachmentChip {{ background: #f3f5f7; border: none; border-radius: 8px; }}
    QFrame#attachmentChip QLabel {{ color: {MUTED}; font-size: 12px; background: transparent; }}
    QFrame#userBubble {{ background: {USER_MESSAGE}; border-radius: 18px; border: none; }}
    QFrame#assistantSurface {{ background: transparent; border: none; }}
    QFrame#messageNotice {{ background: #fbfcfd; border: 1px solid {BORDER}; border-radius: 10px; }}
    QLabel#chatStatus {{ color: {MUTED}; font-size: 11px; }}
    QFrame#chatAlert {{ background: {CONTROL}; border: none; border-radius: 10px; }}
    QFrame#chatAlert[error="true"] {{ background: #fef2f2; }}
    QFrame#chatAlert[error="true"] QLabel#chatStatus {{ color: #991b1b; font-size: 12px; }}
    QFrame#toolSurface, QWidget#toolGroup, QScrollArea#toolList, QWidget#toolListContent {{ background: transparent; border: none; }}
    QFrame#toolListCard {{ background: transparent; border: none; }}
    QFrame#reasoningCard {{ background: {TOOL_BACKGROUND}; border: 1px solid {BORDER}; border-radius: 12px; }}
    QToolButton#toolHeader {{ border: none; border-radius: 9px; background: transparent; }}
    QToolButton#toolHeader:hover {{ background: #f6f7f8; }}
    QToolButton#toolHeader:focus {{ background: #f2f4f6; }}
    QToolButton#toolHeader QLabel {{ background: transparent; color: {TOOL_TEXT}; font-size: {TOOL_FONT_SIZE}px; }}
    QFrame#toolDetails {{ background: {TOOL_BACKGROUND}; border: 1px solid {TOOL_BORDER}; border-radius: {TOOL_RADIUS}px; }}
    QFrame#toolDetails QLabel {{ background: transparent; border: none; color: {TOOL_TEXT}; font-size: 12px; }}
    QFrame#toolDetails QLabel#toolDetailsFooter {{ padding-top: 2px; }}
    QWidget#chatPanel QPlainTextEdit#toolOutput {{ border: none; background: transparent; color: #656b73; font-family: Consolas, 'Courier New'; font-size: 13px; selection-background-color: #dce8f4; }}
    QFrame#toolDetails QScrollBar:horizontal {{ height: 6px; background: transparent; margin: 2px 0; }}
    QFrame#toolDetails QScrollBar::handle:horizontal {{ background: rgba(0, 0, 0, 38); border-radius: 3px; min-width: 28px; }}
    QFrame#toolDetails QScrollBar::handle:hover, QFrame#toolDetails QScrollBar::handle:pressed {{ background: rgba(0, 0, 0, 89); }}
    QFrame#toolDetails QScrollBar::add-line:horizontal, QFrame#toolDetails QScrollBar::sub-line:horizontal {{ width: 0; }}
    QFrame#toolDetails QScrollBar::add-page:horizontal, QFrame#toolDetails QScrollBar::sub-page:horizontal {{ background: transparent; }}
    QLabel#chatStatus[error="true"] {{ color: {ERROR}; }}
    QWidget#settingsSidebar {{ background: {SIDEBAR}; border: none; }}
    QScrollArea#pluginNavScroll, QWidget#pluginNavViewport, QWidget#pluginNavEntries {{ background: {SIDEBAR}; border: none; }}
    QWidget#settingsContent {{ background: {BACKGROUND}; }}
    QLabel#settingsTitle {{ font-size: 20px; font-weight: 600; color: {TEXT}; }}
    QLabel#settingsHeading {{ font-size: 28px; font-weight: 500; color: {TEXT}; }}
    QLabel#settingsDescription {{ color: {MUTED}; font-size: 13px; }}
    QPushButton#settingsNav, QPushButton#pluginNav {{ border: none; border-radius: 10px; background: transparent; color: {TEXT}; text-align: left; padding: 11px 13px; }}
    QPushButton#settingsNav:hover {{ background: {HOVER}; }}
    QPushButton#settingsNav:checked {{ background: {SELECTED}; }}
    QWidget#archivePage {{ background: {BACKGROUND}; }}
    QTreeView#archiveList {{ border: none; background: {BACKGROUND}; outline: none; }}
    QFrame#modelCard {{ background: {BACKGROUND}; border: 1px solid {BORDER}; border-radius: 14px; }}
    QFrame#connectionCard {{ background: {BACKGROUND}; border: 1px solid {BORDER}; border-radius: 14px; }}
    QFrame#welcomeModes {{ background: {CONTROL}; border: none; border-radius: 22px; }}
    QFrame#welcomeScope {{ background: {CONTROL}; border: none; border-radius: 16px; }}
    QPushButton#welcomeMode {{ border: none; border-radius: 19px; padding: 0; font-size: 14px; }}
    QLineEdit#pluginSearch {{ background: {CONTROL}; border: none; border-radius: 10px; padding: 9px 12px; }}
    QFrame#connectionCard[active="true"] {{ border-color: #aebccb; }}
    QFrame#modelListSurface {{ background: {BACKGROUND}; border: 1px solid {BORDER}; border-radius: 18px; }}
    QFrame#settingsCard {{ background: {BACKGROUND}; border: 1px solid {BORDER}; border-radius: 14px; }}
    QTableWidget#connectionModels {{ background: transparent; color: {TEXT}; border: none; outline: none; selection-background-color: transparent; selection-color: {TEXT}; }}
    QTableWidget#connectionModels QHeaderView::section {{ background: {BACKGROUND}; color: {MUTED}; border: none; padding: 9px 12px; }}
    QTableWidget#connectionModels::item {{ padding: 6px 10px; border: none; }}
    QWidget#settingsPage QTableWidget#connectionModels QLineEdit {{ background: {BACKGROUND}; border: none; border-radius: 6px; padding: 3px 6px; min-height: 0; }}
    QLabel#formSection {{ font-size: 14px; font-weight: 600; color: {TEXT}; }}
    QPushButton#advancedToggle {{ border: none; background: transparent; color: {MUTED}; text-align: left; padding: 8px 0; }}
    QPushButton#messageMore {{ border: none; background: transparent; color: {MUTED}; text-align: left; padding: 4px 0; }}
    QPushButton#messageMore:hover {{ color: {TEXT}; }}
    QFrame#codeCard {{ background: {CONTROL}; border: 1px solid {BORDER}; border-radius: 14px; }}
    QFrame#codeCard QLabel {{ background: transparent; border: none; color: {MUTED}; font-size: 12px; }}
    QFrame#codeCard QPlainTextEdit, QFrame#codeCard QTextEdit {{ background: transparent; border: none; color: {TEXT}; }}
    QWidget#chatPanel QTextEdit#toolOutput {{ border: none; background: transparent; color: #656b73; font-family: Consolas, 'Courier New'; font-size: 13px; selection-background-color: #dce8f4; }}
    QWidget#settingsPage QComboBox, QWidget#settingsPage QLineEdit {{ background: {CONTROL}; color: {TEXT}; border: none; border-radius: 10px; padding: 9px 12px; min-height: 18px; selection-background-color: {TEXT_SELECTION}; selection-color: {TEXT}; }}
    QWidget#settingsPage QComboBox::drop-down {{ border: none; width: 28px; }}
    QWidget#settingsPage QComboBox QAbstractItemView {{ background: {BACKGROUND}; color: {TEXT}; border: 1px solid {BORDER}; selection-background-color: {SELECTED}; selection-color: {TEXT}; }}
    QLabel#formLabel, QLabel#settingsNote {{ color: {MUTED}; }}
    QWidget#settingsPage QPushButton, QPushButton#retryButton {{ background: {BACKGROUND}; color: {TEXT}; border: 1px solid {CONTROL_BORDER}; border-radius: 9px; padding: 8px 14px; }}
    QWidget#settingsPage QPushButton:hover, QPushButton#retryButton:hover {{ background: {HOVER}; }}
    QWidget#settingsPage QPushButton#primaryButton {{ background: {ACTION}; color: white; border-color: {ACTION}; }}
    QWidget#settingsPage QPushButton:disabled {{ color: {FAINT}; background: {CONTROL}; }}
    QWidget#conversationWorkspace QScrollBar:vertical, QWidget#settingsPage QScrollBar:vertical {{ width: 6px; background: transparent; margin: 2px 0; }}
    QWidget#conversationWorkspace QScrollBar::handle:vertical, QWidget#settingsPage QScrollBar::handle:vertical {{ background: rgba(0, 0, 0, 38); border-radius: 3px; min-height: 28px; }}
    QWidget#conversationWorkspace QScrollBar::handle:hover, QWidget#settingsPage QScrollBar::handle:hover,
    QWidget#conversationWorkspace QScrollBar::handle:pressed, QWidget#settingsPage QScrollBar::handle:pressed {{ background: rgba(0, 0, 0, 89); }}
    QWidget#conversationWorkspace QScrollBar::add-line:vertical, QWidget#conversationWorkspace QScrollBar::sub-line:vertical,
    QWidget#settingsPage QScrollBar::add-line:vertical, QWidget#settingsPage QScrollBar::sub-line:vertical {{ height: 0; }}
    QWidget#conversationWorkspace QScrollBar::add-page:vertical, QWidget#conversationWorkspace QScrollBar::sub-page:vertical,
    QWidget#settingsPage QScrollBar::add-page:vertical, QWidget#settingsPage QScrollBar::sub-page:vertical {{ background: transparent; }}
    QAbstractScrollArea QScrollBar:vertical {{ width: 6px; background: transparent; margin: 2px 0; }}
    QAbstractScrollArea QScrollBar:horizontal {{ height: 6px; background: transparent; margin: 0 2px; }}
    QAbstractScrollArea QScrollBar::handle {{ background: rgba(0, 0, 0, 38); border-radius: 3px; }}
    QAbstractScrollArea QScrollBar::handle:vertical {{ min-height: 28px; }}
    QAbstractScrollArea QScrollBar::handle:horizontal {{ min-width: 28px; }}
    QAbstractScrollArea QScrollBar::handle:hover, QAbstractScrollArea QScrollBar::handle:pressed {{ background: rgba(0, 0, 0, 89); }}
    QAbstractScrollArea QScrollBar::add-line:vertical, QAbstractScrollArea QScrollBar::sub-line:vertical {{ height: 0; }}
    QAbstractScrollArea QScrollBar::add-line:horizontal, QAbstractScrollArea QScrollBar::sub-line:horizontal {{ width: 0; }}
    QAbstractScrollArea QScrollBar::add-page, QAbstractScrollArea QScrollBar::sub-page {{ background: transparent; }}
    QToolTip {{ background: {DARK_CARD_BG}; color: white; border: none; border-radius: 8px; padding: 8px 11px; font-size: 11px; }}
    QMenu {{ background: {BACKGROUND}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 12px; padding: 5px; }}
    QMenu::item {{ padding: 8px 12px; border-radius: 7px; background: transparent; }}
    QMenu::item:selected {{ background: {HOVER}; }}
    QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
    """


def dialog_stylesheet() -> str:
    """弹窗有自己的样式范围，避免取决于创建它的页面和系统主题。"""
    return f"""
    QFrame#dialogSurface {{ background: transparent; border: none; }}
    QFrame#dialogSurface QLabel {{ color: {MUTED}; background: transparent; }}
    QFrame#dialogSurface QLabel#dialogTitle {{ color: {TEXT}; font-size: 20px; font-weight: 600; }}
    QFrame#dialogSurface QLineEdit {{ background: white; color: {TEXT}; border: none; border-radius: 12px; padding: 12px 14px; selection-background-color: {TEXT_SELECTION}; selection-color: {TEXT}; }}
    QFrame#folderPanel {{ background: white; border: 1px solid {BORDER}; border-radius: 16px; }}
    QFrame#dialogSurface QPushButton {{ background: transparent; border: none; border-radius: 12px; padding: 9px 18px; color: {MUTED}; }}
    QFrame#dialogSurface QPushButton:hover {{ background: {HOVER}; }}
    QFrame#dialogSurface QPushButton#primaryButton {{ background: {ACTION}; color: white; }}
    QFrame#dialogSurface QPushButton#primaryButton:disabled {{ background: #dce0e5; color: {FAINT}; }}
    QFrame#dialogSurface QPushButton#dangerButton {{ background: #fbecee; color: {ERROR}; }}
    QFrame#dialogSurface QPushButton#dangerButton:hover {{ background: #f7dfe3; }}
    QToolButton#iconButton {{ background: transparent; border: none; border-radius: 8px; }}
    QToolButton#iconButton:hover {{ background: {HOVER}; }}
    QToolButton#modelButton {{ background: #f2f3f5; border: 1px solid {BORDER}; border-radius: 15px; padding: 7px 13px; color: {TEXT}; }}
    """


def model_popup_stylesheet() -> str:
    """模型弹层同时用于聊天页和创建弹窗，样式由弹层自身持有。"""
    return f"""
    QFrame#modelPopup, QFrame#modelListPopup, QFrame#projectMenu {{ background: transparent; border: none; }}
    QFrame#modelPopup QLabel#effortTitle {{ color: {EFFORT_TITLE}; font-size: 16px; font-weight: 600; background: transparent; }}
    QFrame#modelPopup QToolButton#effortModelLink {{ color: {MUTED}; font-size: 11px; border: none; padding: 0; background: transparent; }}
    QFrame#modelPopup QToolButton#effortModelLink:hover {{ color: {TEXT}; }}
    QFrame#modelPopup QLabel#effortNote {{ color: {MUTED}; font-size: 11px; background: {CONTROL}; border: none; border-radius: 11px; padding: 2px 6px; }}
    QFrame#modelListPopup QLabel {{ color: {TEXT}; font-size: 12px; background: transparent; }}
    QFrame#modelListPopup QListWidget {{ background: {BACKGROUND}; color: {TEXT}; border: none; outline: none; font-size: 12px; }}
    QFrame#modelListPopup QListWidget::item {{ border: none; padding: 0; background: transparent; }}
    QFrame#modelListPopup QListWidget::item:hover, QFrame#modelListPopup QListWidget::item:selected {{ background: transparent; color: {TEXT}; }}
    """


def choice_stylesheet(*, compact: bool = False) -> str:
    """共用下拉外壳；输入子控件不再单独画焦点框或分隔线。"""
    radius, padding, height = (7, "3px 8px", 18) if compact else (10, "9px 13px", 18)
    return f"""
    QComboBox {{ background: {BACKGROUND}; color: {TEXT}; border: 1px solid {CONTROL_BORDER};
        border-radius: {radius}px; padding: {padding}; padding-right: 32px; min-height: {height}px; }}
    QComboBox:hover {{ border-color: #bcc3cb; }}
    QComboBox:focus, QComboBox[inputFocus="true"], QComboBox[expanded="true"] {{ border-color: #b4b4b4; }}
    QComboBox[keyboardFocus="true"] {{ border-color: {MUTED}; }}
    QComboBox:disabled {{ background: {CONTROL}; color: {FAINT}; border-color: {BORDER}; }}
    QComboBox::drop-down {{ border: none; background: transparent; width: 30px; }}
    QComboBox::down-arrow {{ image: none; width: 0; height: 0; }}
    QComboBox QLineEdit {{ background: transparent; color: {TEXT}; border: none; border-radius: 0;
        padding: 0; margin: 0; min-height: 0; selection-background-color: {TEXT_SELECTION}; selection-color: {TEXT}; }}
    QComboBox QAbstractItemView {{ background: {BACKGROUND}; color: {TEXT}; border: none; outline: none; }}
    """


def choice_popup_stylesheet() -> str:
    """列表外壳只画浅灰边界；选中行由Delegate画圆角灰底与勾号。"""
    return f"""
    QFrame#choicePopup {{ background: transparent; color: {TEXT}; border: none; }}
    QAbstractItemView {{ background: {BACKGROUND}; color: {TEXT}; border: none; outline: none; }}
    QScrollBar:vertical {{ width: 6px; background: transparent; margin: 3px 0; }}
    QScrollBar::handle:vertical {{ background: rgba(0, 0, 0, 38); border-radius: 3px; min-height: 24px; }}
    QScrollBar::handle:hover, QScrollBar::handle:pressed {{ background: rgba(0, 0, 0, 89); }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    """
