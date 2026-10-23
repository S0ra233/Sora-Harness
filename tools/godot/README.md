# Godot 可选后端与资源流程

Godot 适配使用 [GDRETools / gdsdecomp](https://github.com/GDRETools/gdsdecomp) 处理标准资源容器、二进制资源和原生翻译表。JSON、CSV、TSV、键值文本、XML、PO 及文本场景的解析由本项目完成。提取结果仍进入现有 Entry → Scene/Task → package/apply → build 流程，不另建翻译状态。

本轮接口参考 [GDRETools v2.6.4](https://github.com/GDRETools/gdsdecomp/releases/tag/v2.6.4) 的源码；**尚未安装后端或运行流程、游戏测试**。这不是已验证的兼容版本声明。该版本包含文本资源和二进制项目配置读取时的内联脚本执行修复；辅助程序还为当前会话关闭 VisualShader 节点脚本执行。

## 配置

从上述发布页选择对应系统的发行包，保留其配套文件。将实际可执行文件路径配置到环境变量：

```powershell
$env:GAME_TRANSLATION_GDRE = "D:\Tools\GDRETools\gdre_tools.exe"
```

也可以将发行包放在本项目 `tools/godot/bin/`，默认入口为 `gdre_tools.exe`。该目录已被 Git 忽略。发行包中的文件名可能不同；环境变量应指向实际文件。不要用普通 Godot 编辑器代替 GDRETools：辅助程序调用的是 GDRETools 扩展接口。本项目不自动下载、安装或分发工具。

`detect` 只检查文件结构，不启动后端。PCK/内嵌包解包、二进制资源转换和原生 Translation 读写需要后端。纯文本场景、PO 和普通数据文件可以由 Python 读取。

## 资源如何流转

```text
原游戏：标准 PCK / 带 PCK 的 EXE / 松散项目
  → 识别 GDPC 头、内嵌包位置或项目配置，列出启动 EXE 与包的关联
  → 确定一个启动入口和主包，候选报告与项目记录保留该选择
  → 在独立 WORK 内解包
  → 读取 .import / .remap，关联源文件名与实际运行资源
  → 文本直接解析，二进制转换为独立的可编辑视图
  → 生成 Entry，保留原容器、运行资源路径、节点/属性或语言表位置
  → Core 创建 Scene/Task，导出翻译包，校验并接收译文
  → 复制完整游戏，修改选中资源，按原版本保存二进制
  → 用原包的成员和格式生成完整替换包，保留内嵌关系
  → 从最终副本的包重新解包、读回已索引位置
```

`.import/.remap` 指向的运行资源存在时，优先处理该资源，避免同时为编辑器源文件和运行文件建立重复任务。转换视图不会被当作松散资源覆盖到游戏旁边。标准包写回保留原包内路径；不创建一个需要额外脚本加载的补丁包。

## 多个资源包与启动入口

`detect` 保留所有通过结构检查的 `containers`，并用 `launchers` 列出 Windows 启动 EXE 与包的关联：先查该 EXE 的内嵌包，再查 `EXE基本名.pck`，最后查 `EXE完整名.pck`。这反映标准启动关系，不证明某个程序是游戏本体，也不推断 DLC 用途。只有一个关联入口时自动选择它；没有关联入口但只有一个包时沿用单包处理。无法确定入口时，检测仍返回清单，候选预览和正式提取会提示 Agent 选择。

例如目录同时有 `Brotato.pck`、`BrotatoAbyssalTerrors.pck` 和带内嵌包的 `GodotWorkshopUtility.exe`，且 Agent 确认游戏入口为 `Brotato.exe`，可执行：

```powershell
python run.py detect "D:\Games\Brotato" --godot-exe Brotato.exe
python run.py candidates "D:\Games\Brotato" "D:\TranslationWork\Brotato-candidates.json" --work "D:\TranslationWork\Brotato-preview" --godot-exe Brotato.exe
```

`--godot-exe` 接受游戏目录内的相对文件名或绝对路径，只选择已有的标准关联，不执行 EXE。选中包位于 `container`，其他包列在 `additional_containers`，仍保留在完整 `containers` 清单中。程序不把其他包合并到主包，也不根据大小或文件名给它们自动判定用途。

Python 的 `detect`、`collect_candidates`、`prepare` 对应可选参数均为 `godot_executable`。候选报告新增 `godot_source`（`executable`、`container` 两个相对文件名，松散来源可为 `null`）；选择文件需要原样保留此字段。`prepare --selection ...` 自动沿用它，直接提取也可以指定 `prepare GAME WORK --godot-exe Brotato.exe`。项目清单保存启动入口、主包和原输入摘要，重复 prepare 沿用原选择；入口或主包不同则要求新工作目录。

构建复制完整游戏，只重建选中主包，再从该包读回。其他包保持原样；本次流程不代表 DLC 已提取或翻译。额外包是否由脚本加载、覆盖顺序、显式 `--main-pack` 和自定义启动器仍由 Agent 调查，不通过数量推断加载关系。

## 先审查候选

用途明确、未识别为翻译键的显示节点字段可以直接 `prepare`。数据表和自定义属性先走现有候选流程，确认实际语言列、字段用途和游戏加载关系：

```powershell
python run.py detect "D:\Games\ExampleGodot"
python run.py candidates "D:\Games\ExampleGodot" "D:\TranslationWork\Godot-selection.json" --work "D:\TranslationWork\Godot-preview"
```

`Godot-preview` 需要为空，与后续正式 WORK 分开。主 Agent 在报告的 `reviews` 记录用途、理由和 `translate / skip / pending` 决定，在 `selected_groups` 填写要翻译的组 ID，保留 `source_hashes` 和 `godot_source`。然后：

```powershell
python run.py prepare "D:\Games\ExampleGodot" "D:\TranslationWork\Godot-project" --selection "D:\TranslationWork\Godot-selection.json"
python run.py package "D:\TranslationWork\Godot-project"
```

后续 `apply`、`build` 参数与其他引擎一致。构建必须指定不存在的新输出目录；少量翻译预览可使用已有 `--allow-partial`。

候选组和 Scene 依据同一份文件/字段/语言信息建立。`source_context` 提供原始资源路径、实际运行路径、节点类型、属性或消息上下文等事实；不根据文件名猜剧情。表格还提供同行键和其他语言值。未登记在项目语言配置中的翻译资源需要审查，脚本也可能动态加载它。样本只能帮助判断，需要时查看完整资源。`write_supported=false` 的组不能选作写回来源。

## 各格式的写回方式

| 来源 | 定位与处理 |
| --- | --- |
| JSON / CSV / TSV / 键值 / XML / TXT | 复用现有格式模块，候选选择后只替换选中字段，保留原编码和 BOM |
| `.tscn/.tres` | 按资源段、节点身份、属性、字符串位置定位；只替换对应引号内的值，保留头部、引用和字典键 |
| `.scn/.res` | GDRETools 转为文本视图后使用相同定位；写回按已确认的 Godot 3/4 版本序列化到原运行路径 |
| Translation / TranslationPO | 保留语言、消息键、上下文和已有复数形式；只改选中的消息值 |
| OptimizedTranslation / PHashTranslation | 按该资源自己的哈希桶位置定位值；追加 UTF-8 消息并更新值的偏移和长度，保留键哈希及其他消息位置 |
| `.po` | 保留 `msgid/msgctxt`、已有复数形式和头部；写选中的 `msgstr`，提交过的条目移除 `fuzzy` 标记 |

已知翻译键在场景中不建立替换任务，修改对应语言表。压缩表不能可靠恢复键时，不拿另一语言的行号推断对应关系；场景字符串进入候选审查，避免误改查表键。没有明确版本的二进制资源、Godot 2 二进制资源保留为只读候选。

语言表写回当前只替换**原有语言资源中的选中值**。不自动添加语言、修改语言菜单或默认语言，不自动扩展复数形式，不批量覆盖所有语言。翻译日语资源后仍需让游戏加载这份日语资源；译文的语言要求由 `style.md` 表达。Godot 本轮不接入跨语言补全。

Godot BBCode 和常见格式占位符交给现有 Core 校验，但这不检查游戏的自定义脚本语法。

## 失败与范围

每次解包、转换、写回和最终读回使用新的 `_godot/` 会话目录，保留请求、响应、视图和日志。单个资源无法解析时进入提取报告；选中资源无法保存或包无法重建时构建停止，由现有 Core 保留暂存副本和 `build_report.json`，不会发布一个读回失败的输出。缺少后端时提示配置路径，原游戏保持只读。

以下内容留给 Agent 调查或专用适配：额外包的实际加载顺序、自定义/加密外壳、GDScript/C# 任意字面量、Dialogue Manager/Dialogic 私有语法、字体和图片文字。本轮不会反编译并改写全部游戏脚本，也不执行游戏主场景来提取文本。临时适配继续复用 Core 的任务流程。

之后找游戏验证时，先分别找标准 PCK、二进制场景和已有多语言表的样本。确认候选语言与显示用途，写一两个明显标记到新副本，检查最终包读回和实际界面；再确认未选择的语言/字段保留原值。字体、加载成功和对白覆盖应分别记录，不能由读回结果代替。本轮未增加测试脚本。

## 依赖与参考来源

- 外部 GDRETools 按需配置，遵循其 [MIT 许可](https://github.com/GDRETools/gdsdecomp/blob/v2.6.4/LICENSE)。二进制和上游源码不随本项目分发。
- `gdre_bridge.gd` 是本项目的辅助程序；调用方式参考上游 [`ResourceCompatLoader`](https://github.com/GDRETools/gdsdecomp/blob/v2.6.4/compat/resource_loader_compat.cpp)。压缩翻译值替换遵循 [`optimized_translation_extractor.cpp`](https://github.com/GDRETools/gdsdecomp/blob/v2.6.4/compat/optimized_translation_extractor.cpp) 的数据布局与操作思路，没有复制整份解析器。
- Godot [资源包加载说明](https://docs.godotengine.org/en/stable/tutorials/export/exporting_pcks.html) 与 [国际化说明](https://docs.godotengine.org/en/stable/tutorials/i18n/internationalizing_games.html) 用于明确加载和翻译键边界。
- Windows 标准主包选择参考 [Godot 3.6 启动配置源码](https://github.com/godotengine/godot/blob/3.6/core/project_settings.cpp#L311-L410)；运行时额外包的加载顺序需要结合游戏脚本确认。
- [GodotPckTool](https://github.com/hhyyrylainen/GodotPckTool) 仅作容器方案对照，未接入；[Dialogue Manager](https://github.com/nathanhoad/godot_dialogue_manager) 与 [Dialogic](https://github.com/dialogic-godot/dialogic) 仅作插件格式参考，未作为本轮依赖。
