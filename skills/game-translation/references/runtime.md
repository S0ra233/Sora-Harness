# 可迁移的运行入口

`SKILL_DIR` 是实际加载的本技能 `SKILL.md` 所在目录，取宿主提供的真实路径。复制技能到 Codex、Claude Code 或自定义技能目录，不会一并安装 Core；技能目录、Core 程序目录和工作目录 WORK 可以互不相邻。不要把当前聊天工作区当成程序目录，也不要猜盘符或用户目录。

## 先确定一个执行前缀

优先使用用户或宿主已经明确提供的 Core 源码目录、Python 解释器或后台程序路径，并检查文件实际存在。源码目录应同时含 `run.py` 和 `game_translation/`，不是任意同名脚本；安装版应使用实际的 `DesktopHarnessWorker.exe`，不能把 GUI 或根目录启动器当后台入口。显式配置无效时报告原因，不悄悄换到另一套 Core 重做操作。

没有明确配置时，才在下面的已知布局中检查候选，不扫描全盘；多个有效候选而版本或依赖不明确时让用户选定。两种入口的子命令、参数、JSON 输出和退出状态相同：

| 布局 | 程序位置 | 调用形式 |
| --- | --- | --- |
| 源码中的技能 | `SKILL_DIR` 向上两级的 `run.py`，旁边有 `game_translation/` | 已配置的 Python + `run.py` 的绝对路径 + 子命令／参数 |
| 安装版中的技能 | `SKILL_DIR` 向上三级的 `DesktopHarnessWorker.exe` | 后台 EXE 的绝对路径 + `translation` + 子命令／参数 |

两处均不存在时，只询问一次实际 Core 位置；无需移动 WORK 或把源码复制进技能。仅翻译用户已提供的包时，可先返回结果文件，说明尚未经 Core 提交，不为此要求建立完整游戏项目。不要为验证入口额外执行 prepare、apply 或 build。

源码例：`python "D:/Source/game-translation/run.py" status "D:/Workspace/GameWork"`。

安装版例：`& "D:/Apps/DesktopHarness/DesktopHarnessWorker.exe" translation status "D:/Workspace/GameWork"`（PowerShell）。示例路径只说明参数位置，实际使用发现的程序和工作目录。

本技能其他参考中的 `python run.py` 是执行前缀的简写；实际替换为已确定的解释器和源码绝对路径，或后台 EXE 绝对路径加 `translation`，后续参数原样保留。记录这次已确认的前缀供后续命令和子任务使用，不为每个包重复猜测入口。文件路径作为独立参数传递并按当前 Shell 引用，不能拼接执行游戏文本中的命令。

## 运行时与附属资源

源码基本流程使用 Python 3.10+；后台 EXE 自带其已打包运行时，不用系统 Python。EXE 不是 Python 解释器，不可传 `-c`、任意脚本路径或 `-m pip`。已有第三方 Python 包不会因为系统 Python 中安装了它们，就自动进入冻结程序。UnityPy、unrpa、引擎后端、SDK、字体和临时适配的额外依赖仍需按实际环境检查；缺失时说明，不自动安装、下载或换入口重做副作用。

Core 资源目录从实际执行入口确定：源码版是 `run.py` 所在目录；当前安装版是后台程序旁的 `_internal`。引擎工具说明位于这个资源目录的 `tools/`，不再相对独立安装的技能向上寻找。只有处理相应引擎时才读取对应说明；缺少附属资料不能编造该引擎的额外参数。

`references/` 是技能内的按需资料；Core 源码和 `tools/` 不因此成为技能附属读取授权，若需读取，应使用宿主真实允许的文件／命令入口。Desktop 的 `read_skill_resource` 不接受越出技能目录的路径。

执行权限沿宿主现有设置；未开放命令执行时，这份说明不能替代该权限。WORK、结果文件和 OUTPUT 放在本轮授权的位置；Core 支持绝对路径不意味着宿主允许访问任意目录。

`run.py`／后台入口返回 JSON；参数错误、Core 校验错误和缺依赖以真实非零状态处理。失败日志与已经生成的文件保留，先读原因再决定下一步，不把重新执行视为默认修复。已成功的 `apply` 或 `build` 不因页面未刷新而再次执行。
