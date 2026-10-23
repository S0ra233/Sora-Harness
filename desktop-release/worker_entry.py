"""冻结后的后台入口；只接受实际登记的工作模式，不加载GUI。"""
from pathlib import Path
import runpy
import sys

from process_support.launcher import configure_frozen_process


def main() -> int:
    configure_frozen_process()
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode == "subagent" and len(sys.argv) == 2:
        from desktop.runtime.subagents.runner import main as run
        run()
        return 0
    if mode == "engine" and len(sys.argv) == 2:
        from game_translation.process_launcher import main as run
        return run()
    if mode == "translation":
        # 复用CLI的参数、Core校验、JSON结果和退出码，不把EXE作为任意Python解释器。
        from run import main as run
        # argparse的帮助文字也保留必要的模式前缀，实际参数仍显式交给原CLI。
        sys.argv[0] += " translation"
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        return run(sys.argv[2:])
    if mode == "renpy-read" and len(sys.argv) == 4:
        helper = Path(__file__).resolve().parent / "tools" / "renpy" / "read_rpyc.py"
        sys.argv = [str(helper), *sys.argv[2:]]
        runpy.run_path(str(helper), run_name="__main__")
        return 0
    print("后台入口参数无效，只接受subagent、engine、renpy-read或translation。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
