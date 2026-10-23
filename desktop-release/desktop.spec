"""两个入口共享一份依赖目录；资源按清单收集，不复制用户local目录。"""
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

release = Path(SPECPATH)
root = release.parent


def source_modules(package, allowed=None):
    modules = []
    for path in (root / package).rglob("*.py"):
        relative = path.relative_to(root)
        if allowed is not None and len(relative.parts) > 2 and relative.parts[1] not in allowed:
            continue
        if any(part in {"__pycache__", "tests", "test", "local", "docs", "design"} for part in relative.parts):
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if all(part.isidentifier() for part in parts):
            modules.append(".".join(parts))
    return modules


desktop_packages = {
    "application", "attachments", "configuration", "credentials", "environment", "llm",
    "plugins", "plugin_api", "mcp", "prompts", "runtime", "skills", "storage", "tools", "ui",
}
modules = source_modules("desktop", desktop_packages) + source_modules("game_translation") + source_modules("process_support")
modules += collect_submodules("pygments.lexers") + collect_submodules("pygments.styles")
# 只收集宿主SDK；mcp.cli导入时要求CLI额外依赖并退出，Desktop不使用它。
modules += collect_submodules("mcp", filter=lambda name: name != "mcp.__main__" and name != "mcp.cli" and not name.startswith("mcp.cli."))
modules += collect_submodules("mcp_types")
# 外部资料库插件使用标准库SQLite；动态加载的插件不参与静态导入发现。
modules += ["sqlite3", "run"]
resources = [
    "desktop/configuration/adapters.json", "desktop/llm/providers.json",
    "desktop/prompts/default.json", "desktop/prompts/summary.txt",
    "game_translation/engine_processes.json", "game_translation/formats/RPGMTL-LICENSE.txt",
    "tools/renpy/read_rpyc.py", "tools/godot/gdre_bridge.gd", "LICENSE",
    "tools/tyrano_asar.md", "tools/unity_fonts.md",
]
resources += [str(path.relative_to(root)).replace("\\", "/")
              for path in (root / "skills" / "game-translation").rglob("*.md")]
resources += [str(path.relative_to(root)).replace("\\", "/") for path in (root / "tools").rglob("README.md")
              if not any(part in {"vendor", "deps", "bin", "obj"} for part in path.relative_to(root).parts)]
datas = [(str(root / name), str(Path(name).parent)) for name in resources]
datas += copy_metadata("PySide6") + copy_metadata("shiboken6")
datas += copy_metadata("mcp") + copy_metadata("mcp-types")


def analyze(entry):
    analysis = Analysis([str(release / entry)], pathex=[str(root)], binaries=[], datas=datas,
                        hiddenimports=sorted(set(modules)), hookspath=[], runtime_hooks=[],
                        excludes=["PyQt5", "PyQt6", "PySide2", "tkinter", "IPython", "jupyter"], noarchive=False)
    # Qt使用Windows的未改名ICU接口；PATH中的Poppler同名库会缺少这些导出。
    # 系统ICU由支持的Windows提供，不能把构建机其他工具的ICU带进成品。
    system_icu = {"icu.dll", "icuuc.dll", "icuin.dll", "icudt.dll"}
    analysis.binaries = [entry for entry in analysis.binaries if Path(entry[0]).name.casefold() not in system_icu]
    return analysis


gui = analyze("gui_entry.py")
worker = analyze("worker_entry.py")
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="DesktopHarness",
              debug=False, strip=False, upx=False, console=False)
worker_exe = EXE(PYZ(worker.pure), worker.scripts, [], exclude_binaries=True, name="DesktopHarnessWorker",
                 debug=False, strip=False, upx=False, console=True)
bundle = COLLECT(gui_exe, worker_exe, gui.binaries, worker.binaries, gui.datas, worker.datas,
                 strip=False, upx=False, name="DesktopHarness")
