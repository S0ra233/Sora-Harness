# WOLF RPG Editor 适配

本阶段接入 WOLF 的识别、解包、文本候选、Scene/Task 和副本写回。实现尚未经过项目集成测试、翻译写回验证或游戏运行验证，不代表全部 WOLF 版本兼容。

## 可选工具

Python 部分只用标准库。以下 Windows 程序由用户另外下载，不随仓库分发，也不在运行时自动下载或更新：

| 工具 | 接入版本 | 职责与来源 |
|---|---|---|
| UberWolfCli | v0.6.4 | 资源包解包：[官方版本](https://github.com/Sinflower/UberWolf/releases/tag/v0.6.4) |
| WolfTL | v0.6.2 | 地图、公共事件、数据库及 Game.dat 的 JSON 导出和二进制写回：[官方版本](https://github.com/Sinflower/WolfTL/releases/tag/v0.6.2) |

两项目仓库标注 MIT 许可，分别见 [UberWolf LICENSE](https://github.com/Sinflower/UberWolf/blob/main/LICENSE) 和 [WolfTL LICENSE](https://github.com/Sinflower/WolfTL/blob/main/LICENSE)。WolfTL 的解析工作参考了 Wolf Trans；这里通过其 CLI 调用，不把上游源码复制到本项目。若将来分发工具二进制，应一并整理工具及其依赖的许可声明。

将程序放在已忽略的 `tools/wolf/bin/`，或在当前 PowerShell 中设置路径：

```powershell
$env:GAME_TRANSLATION_UBERWOLF = 'D:/Tools/WOLF/UberWolfCli.exe'
$env:GAME_TRANSLATION_WOLFTL = 'D:/Tools/WOLF/WolfTL.exe'
```

程序只把复制到工作目录中的资源文件交给 UberWolf；不把原游戏路径或游戏 EXE 作为解包执行入口。新型加密、WolfX、需要运行时取密钥的游戏不在当前自动路线内。工具失败时保留日志与工作副本。

## 使用流程

直接 `prepare` 默认建立消息、选项、已确认的文字图片命令以及游戏标题等显示字段的任务。

数据库记录名称、字符串字段、字符串赋值和其他命令参数可能是显示文字，也可能是路径或逻辑条件。需要这些内容时，先预览候选：

```powershell
python run.py candidates 'D:/Games/Example' 'D:/Translation/Example-candidates.json' --work 'D:/Translation/Example-preview'
```

`--work` 是独立空目录，用来保存解包与 WolfTL 导出；不创建翻译项目。原文与已有译文不会按语言自动丢弃。

主 Agent 根据每组 `samples`、`context` 和字段用途填写 `selected_groups`。一组中的样本不代表全部条目；需要时查看预览工作目录 `_wolf/prepare-*/export/dump/` 中的完整文档。明确显示字段也必须加入选择清单，不能只选数据库组。带 `write_supported=false` 的组不能选择写回。

然后使用另一个空目录建立项目：

```powershell
python run.py prepare 'D:/Games/Example' 'D:/Translation/Example-work' --selection 'D:/Translation/Example-candidates.json'
```

后续继续使用原来的 `package → apply → build`。重新 prepare 继承此前的候选选择；改变选择需建立新项目。无候选选择时，不会自动纳入含义不明的数据库字段。

## 数据流与写回范围

```text
原 Data（只读）
  → 工作目录中的 Data 副本
  → UberWolf 解包
  → WolfTL 导出 JSON
  → Python 按命令/字段定位产生 Entry
  → Core Scene / Task / 翻译包
  → WolfTL 对输出副本导出新 JSON，并应用译文
  → WolfTL 生成修改后的二进制文件
  → 只发布选中的容器及数据库配套 .project
  → 从输出副本二进制重新导出并读回
```

Entry 的 `file` 是实际 `.mps/.dat` 位置；`asset` 是 WolfTL 文档名，`path` 是文档内 JSON Pointer。地图按事件页分 Scene，公共事件按事件分 Scene，数据库按类型分组。事件名称和开发注释不作为正文任务；记录名称仅作为候选，因为游戏可能按名字查找它。

Core 新增可选批量读写和资源复制钩子，避免每个文件都重新解析整套 Data。原有引擎仍走逐文件接口。原归档和松散输入记录在 `input_hashes`，解析所需的 `.dat/.project/.mps` 记录在 `source_hashes`；写回前检查来源是否改变。

输出副本省略已经成功解包的归档，保留其解包文件，不重新生成 `.wolf`。原归档仍保留在原游戏与准备目录中。松散目录和同名归档同时存在时停止，要求先确认哪一份应被游戏加载。此输出方式的实际加载效果仍需实机确认。

## 当前限制和交接重点

- 已识别明文头的 UTF-8/CP932 文件允许写回；CP932 无法表示的译文会报错，不允许静默变成问号。不能确认编码的加密资源只读，未实现编码迁移。
- WolfTL 可能跳过不能解析的地图。本项目将地图清单与导出清单比对，遗漏进入警告；同名地图导出冲突直接报错。
- 未选中的二进制容器保持原文件；被修改容器内部由 WolfTL 序列化，未知字段是否完整保留仍需要其他模型验证。
- 图片文字、外部自定义文本、字体替换和所有加密变体不属于本阶段。
- 本次参考资源中的两个包可被工具解出，JSON 中可见现成中文和日文开发注释；同时存在地图未导出的情况。这只是格式研究结果，不能当作本适配器通过验证。
- 后续验证请重点检查：候选选择和场景定位；带控制码的真实译文写回；输出二进制读回；未选字段及数据库 schema 保留；松散资源被游戏实际加载。不要只用原文写回证明翻译可用。

工作目录 `_wolf/` 下每次操作使用新的批次目录，日志、JSON 和失败产物保留，不自动清理。单文件兼容入口没有 work 参数时，辅助产物放在系统临时目录 `game-translation-wolf/`；正常 Core 流程放在指定项目工作目录。

## English

Optional Windows backend using UberWolfCli v0.6.4 and WolfTL v0.6.2 (upstream MIT projects). Download executables separately into `tools/wolf/bin/`, or set `GAME_TRANSLATION_UBERWOLF` and `GAME_TRANSLATION_WOLFTL`. No automatic download or game execution is performed.

WOLF candidates require `candidates GAME REPORT --work PREVIEW`, with an empty directory separate from the later prepare project. Review `selected_groups`, then run `prepare GAME WORK --selection REPORT`. Default preparation only includes explicit display fields; database and ambiguous command strings require review. Core translation packages remain unchanged.

Builds use unpacked resources in a new game copy, not rebuilt archives. Readback exports the actual output binaries again. Unsupported encodings, skipped maps and custom resources are reported; font handling is not implemented. The adapter has not undergone integration, translated round-trip or runtime testing. Tool logs and intermediate files are retained.
