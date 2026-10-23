"""最小可安装页面示例：插件提供内容，宿主负责页面外壳。"""
from PySide6.QtWidgets import QLabel, QVBoxLayout
from desktop.plugin_api.ui import CardSurface, UiContribution, UiPageDefinition, WhiteSurface


def create_ui(settings_path, *, services):
    def create(context):
        body = WhiteSurface()
        layout = QVBoxLayout(body)
        text = QLabel("这是插件创建的辅助内容。正常请求仍在主聊天发送。")
        text.setWordWrap(True)
        layout.addWidget(CardSurface(text))
        layout.addStretch()
        return UiContribution("reference-page-example", body)
    return (UiPageDefinition("reference-page-example", "reference_page_example", "资料页示例",
                             ("资料页示例",), create, presentation="aside"),)
