"""桌面入口：创建应用和窗口，等待用户操作。"""
import sys
import argparse
import os

from PySide6.QtWidgets import QApplication

from .storage.paths import LocalPaths
from .storage.initialization import initialize_data


def main(argv: list[str] | None = None) -> int:
    """源码沿用原目录，安装版默认使用用户目录，也可显式指定数据根。"""
    parser = argparse.ArgumentParser(description="模型工作平台")
    parser.add_argument("--data-dir", help="配置、对话与草稿的数据目录；不会自动搬迁现有文件。")
    options, qt_arguments = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    paths = LocalPaths.default(options.data_dir)
    # 在界面及插件导入前固定进程的数据根，后台进程也继承相同选择。
    os.environ["DESKTOP_HARNESS_DATA_DIR"] = str(paths.root)
    from .ui.main_window import MainWindow
    app = QApplication([sys.argv[0], *qt_arguments])
    initialize_data(paths)
    window = MainWindow(paths.settings)
    window.show()
    # 进入界面事件循环，按钮点击后才会执行已连接的处理函数。
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
