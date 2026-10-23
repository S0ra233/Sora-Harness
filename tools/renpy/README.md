# Ren'Py 后端与处理方式

适配器接入现有 `detect → prepare → package → apply → build` 流程，不增加另一套任务状态。基础格式处理只用标准库；RPA 和 RPYC 分别使用以下可选依赖，本项目不自动下载、安装或运行游戏。

| 后端 | 来源 | 用途 |
|---|---|---|
| unrpa | [Lattyware/unrpa](https://github.com/Lattyware/unrpa) | 识别 RPA 格式、规范化索引和读取编译脚本成员 |
| unrpyc | [CensoredUsername/unrpyc](https://github.com/CensoredUsername/unrpyc) | 使用替代 AST 类读取 RPYC，恢复原生翻译 ID 和语句结构 |
| 匹配版本 Ren'Py SDK（显式备用路线） | [Ren'Py](https://www.renpy.org/) | 原生生成翻译模板，处理源脚本缺少可靠 ID 的情况 |

第三方工具不随项目分发。使用和再分发时保留各上游的许可与版权说明；unrpa 仓库有 GPL 许可，unrpyc 有 MIT 许可。具体以使用版本的上游文件为准。建议记录所用包版本或仓库提交，反馈兼容问题时同时提供游戏的 Ren'Py 版本。

## 可选工具配置

运行本项目的 Python 环境中安装 `unrpa` 即可启用归档读取。RPYC 后端使用完整 unrpyc 仓库，须包含 `decompiler/`，不要只下载 `unrpyc.py`。

```powershell
python -m pip install unrpa
$env:GAME_TRANSLATION_UNRPYC = "D:\Tools\unrpyc"
python run.py prepare "D:\Games\Example" "D:\TranslationWork\Example"
```

也可以把工具放在 `tools/renpy/vendor/unrpyc/`。该目录仅存放本地依赖，不应随项目提交。这里的 `read_rpyc.py` 是实际使用的读取辅助程序，不是测试文件。

SDK 路线默认关闭。只有明确设置 `GAME_TRANSLATION_RENPY_SDK` 时，`prepare` 才会把游戏完整复制到 WORK，再调用指定启动程序的 `translate` 命令。这会执行副本中的项目初始化，不是静态读取；需要使用与游戏匹配的 SDK，并由执行流程的用户或 Agent 明确选择该路线。

```powershell
$env:GAME_TRANSLATION_RENPY_SDK = "D:\Tools\renpy-sdk\renpy.exe"
python run.py prepare "D:\Games\Example" "D:\TranslationWork\ExampleSDK"
```

失败产物和日志保留在 WORK 的 `_renpy/` 下。已有失败诊断目录没有项目记录时，下一次 prepare 使用新的空工作目录。

## 提取与写回

- 检测结合运行库、目录和 RPA/RPC2 文件头；仅凭 `.rpy` 后缀不会认定引擎。
- 归档在 WORK 中单独读取，仅导出 `.rpyc`；归档中的 `.rpy` 不当作运行脚本。保留松散文件优先、同名归档成员按加载顺序选择的来源记录。
- RPYC 优先读取静态转换后的 slot 2，直接保留原生翻译 ID。只有 slot 1/旧格式且缺少 ID 时，不按原文猜 ID。松散 `.rpy` 与 `.rpyc` 同时存在时，使用本游戏运行库的 `RPYC_MAGIC` 核对缓存；无法确认的对白交给 SDK 模板路线或人工分析。
- 有原文注释的原生翻译模板可以提供可靠的 ID/语句；已有其他语言的译文不直接当作原文。源脚本中明确的菜单、screen 文本、翻译标记调用和 Character 名称进入原生 `old/new` 键；任意 Python 字符串、动态拼接与用途不明的数据保留在提取报告中。
- 默认创建 `gt_zh_cn` 翻译命名空间，重名时使用数字后缀。它是新翻译的内部标识；第一版面向简体中文，不增加通用语言选择参数。已有语言记录在提取报告中，疑似中文时提示先检查已有汉化。
- Entry 的 `file/path` 指向 WORK 中模板的可写位置。对白按原生 ID 保留独立 Entry；界面共享一个原生字符串键时，只建立一个可写位置。Scene/Task/翻译包继续由现有公共流程管理。
- 输出完整复制原游戏，保留脚本和 RPA，仅新增 `game/tl/<语言>/game_translation.rpy` 与语言启动配置；不把恢复的主脚本放回游戏，不重建 RPA。未翻译位置初始化为原文，支持已有 `--allow-partial` 流程。
- 写回只替换文本字面量，保留语句、属性、语音命令和控制逻辑；按同一原生 ID/字符串键读回。变量与嵌套插值、文本标签通过现有翻译包占位符流程保护。
- `build --font FONT.ttf` 支持原生语言字体配置与默认样式；自定义字体样式可能需要后续单独处理。

`config.force_archives`、动态归档列表、自定义搜索路径或加载回调需要人工确认；当前原生松散翻译路线会在已识别的不兼容加载关系下阻止构建。未知资源或混淆不静默视作成功。

## 当前状态与反馈

本轮只实现代码，没有安装依赖、编写或运行测试，也没有真实游戏验证。其他模型跑流程后，请反馈：游戏/Ren'Py 版本、后端版本、`extraction_report.json`、出现问题的源文件或原生 ID，以及实际报错。读回相等只证明对应模板位置的内容，不能证明游戏启动、全部对白覆盖或字体正常。

实现参考：[原生翻译机制](https://www.renpy.org/doc/html/translation.html)、[命令行模板生成](https://www.renpy.org/doc/html/cli.html#translate)、[脚本加载实现](https://github.com/renpy/renpy/blob/master/renpy/script.py)、[unrpyc AST 读取](https://github.com/CensoredUsername/unrpyc/blob/master/unrpyc.py)。
