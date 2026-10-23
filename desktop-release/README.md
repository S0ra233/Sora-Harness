# Windows安装版与一键构建

双击本目录的`build.cmd`。首轮需联网准备独立Python构建环境和官方Inno Setup便携编译器；已有编译器可通过`-ISCC`指定。构建用Python必须已安装，默认先找当前用户的Miniconda，再找PATH里的Python。成品运行不需要安装Python。

每次输出到`artifacts/时间-编号/`，不删除旧产物。`dist/DesktopHarness/`是完整可运行目录，`installer/DesktopHarness-Setup-0.1.0.exe`是安装包。构建只生成产物，不启动GUI或执行安装包。失败日志位于该批次的`build.log`。

```powershell
.\build.ps1 -Python "C:\Python314\python.exe"
.\build.ps1 -Version 0.1.1
.\build.ps1 -SkipInstall -ISCC "已有编译器目录\ISCC.exe"
.\build.ps1 -ExeOnly
```

## 使用现有配置

构建成功后，项目根目录新增`DesktopHarness.exe`。它只是轻量启动器，通过`current-build.txt`定位最新成品，明确传入原项目`desktop/local`路径。现有模型配置、提示词预设、对话、草稿和目录全部继续使用原位置，不复制或迁移。请保留对应的完整构建目录。

源代码修改后需要再次运行`build.cmd`，根目录启动器会指向新批次；它不会自动执行尚未打包的新源码。安装后的开始菜单入口默认使用独立用户数据目录，读取原配置时采用下方参数。

也可以直接启动安装后的程序，并指定已有数据目录：

```powershell
& "安装目录\DesktopHarness.exe" --data-dir "D:\your-project\desktop\local"
```

## 安装与数据目录

安装包默认安装到当前用户的`%LOCALAPPDATA%\Programs\DesktopHarness`，提供开始菜单入口和可选桌面快捷方式，不要求管理员权限。未传`--data-dir`时，运行数据位于`%LOCALAPPDATA%\DesktopHarness`。

首次启动只补建缺失的空`settings.json`；其他配置和记录沿现有服务按需生成。已有文件不覆盖，损坏内容沿现有校验报告错误。安装器和卸载器只管理程序文件，不包含或移除用户数据目录。源代码启动默认仍使用原`desktop/local`。

`--data-dir`会在界面和插件加载前固定当前进程的`DESKTOP_HARNESS_DATA_DIR`，子执行器继承相同选择；不修改系统环境变量。其他默认路径调用也会采用这个目录，避免插件或工具记录写进另一份数据根。

资源清单包含内置协议配置、默认提示词、摘要、引擎时限配置、帮助脚本及许可文本，不包含`desktop/local`、私人Skills、请求记录、游戏、截图或用户自建提示词预设。内置默认提示词属于程序资源，个人预设是独立数据。

## 进程和外部工具

GUI使用无控制台入口；后台执行器保留stdin/stdout，并由现有CREATE_NO_WINDOW隐藏启动。子Agent、引擎启动屏障和Ren'Py读取脚本使用固定后台模式，保留现有进程树和取消处理。后台另提供`DesktopHarnessWorker.exe translation <原CLI子命令及参数>`，直接复用`run.main(argv)`；该入口不接受任意Python脚本，也不调用GUI。两个EXE共享`_internal`依赖目录，不要单独复制其中一个EXE。

翻译插件通过既有skill_roots登记仓库`skills/game-translation`中的公开技能；下次打包收录到`_internal/skills/game-translation`，不复制或覆盖用户Skills目录。根`SKILL.md`保留旧入口并链接到实际正文，详细引擎和流程按需读取。插件的`/翻译`只打开阅读页，技能分类的game-translation才用于引导主聊天工作；命令执行仍受当前宿主权限控制。启用新增技能会使技能目录更新，若先前无技能，既有加载工具也可能首次开放；不是在历史中改写旧正文。

Qt加载沿用PyInstaller／PySide6的启动hook，额外保留`_internal`、`PySide6`和`shiboken6`的DLL目录句柄；不在导入Qt前重置DLL目录或清理PATH。外部程序的库冲突应在具体启动边界处理，不能靠提前清理GUI环境解决。

成品要求Windows 10 1809或更新的64位系统。Qt使用Windows提供的ICU接口，spec排除误从其他工具PATH收集的同名icuuc／icuin／icudt／icu库；不能把Poppler版本的同名库放进程序依赖目录。窗口和列表销毁时，滚动、侧栏分组和辅助页的过滤器会解除监听并停止动画。

Git、rg、PowerShell和模型用于执行脚本的外部Python继续使用实际环境配置。可选UnityPy、unrpa、unrpyc、引擎解包器和SDK不因打包自动安装；对应功能需要原有外部依赖。这里只打包通用Desktop、内置插件和Core源码，不宣称所有游戏引擎工具已独立封装。

本批成品只证明构建过程完成。GUI、真实API、后台任务与安装后的行为仍由用户实测。

每次构建使用新的时间／编号目录；实际成品以本轮控制台输出和current-build.txt为准。仓库不附带本机构建产物或指针，请先按本文构建。运行效果由实际目标环境确认，构建完成不等于GUI、API或游戏兼容性验证。
