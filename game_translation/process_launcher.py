"""等宿主登记进程树后才启动引擎程序，禁止后台启动窗口或自动重试。"""
import json
import subprocess
import sys


def main() -> int:
    request = json.loads(sys.stdin.readline())
    result = subprocess.run(request["command"], cwd=request["cwd"], stdin=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
