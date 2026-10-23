# 引擎边界

示例采用源码 CLI 写法；安装版的对应入口见[运行入口](runtime.md)。

本页的 `tools/` 路径均相对实际 Core 资源目录，按运行入口定位后用宿主允许的文件工具读取；它们不在独立安装的技能目录内。

WOLF 使用可选的 UberWolfCli/WolfTL，先阅读 `tools/wolf/README.md`。`prepare` 默认只建立明确显示字段的任务；数据库和用途不明的字符串先通过 `candidates GAME REPORT --work PREVIEW` 审查，PREVIEW 与后续 WORK 使用不同的空目录。后端在副本上解包，输出使用松散资源，不重建归档。检查地图遗漏警告和 `write_supported`；图片文字、未知编码、字体以及部分格式变体尚未支持。本适配的集成、译文写回和实机验证待完成，不把工具导出成功当作游戏完整覆盖。

Ren'Py 使用原生 `tl` 翻译文件，先阅读 `tools/renpy/README.md`。RPA 需要可选 unrpa；RPYC 需要显式配置的 unrpyc 仓库。保留原生翻译 ID、语句和资源来源，Entry 定位到工作模板。对白按 ID 独立，界面共享同一个原生 `old` 键时只有一个写回位置，出现位置保留在 structure。没有可靠 ID 的对白和未知 Python 字符串留在提取报告，不猜 ID。SDK 模板生成默认关闭；显式配置后会执行工作副本的项目初始化，应按本次用户授权选择该路线。输出保留原游戏脚本与 RPA，增加翻译和语言配置；本适配尚未验证，不能宣称文本覆盖、运行或字体正常。

Godot 使用可选 GDRETools，先阅读 `tools/godot/README.md`。检测标准 GDPC 包、EXE 内嵌 PCK 和松散项目；按 `.import/.remap` 关联源名称与实际运行资源。多个包不直接报错：检测返回全部 `containers`、EXE 与包的 `launchers` 关联；入口不唯一时，Agent 确认实际启动程序，并在 detect/candidates/prepare 使用 `--godot-exe`。候选选择文件保留 `godot_source`，正式与重复 prepare 沿用该来源，构建仅重建选中主包，其他包保持原样；不代表 DLC 已处理。文本场景按节点/属性定位；二进制场景先转换为可编辑视图，写回按已确认的 Godot 3/4 版本保存并重建原包，从最终包重新读回。原生 Translation、压缩翻译表及 PO 只修改所选的现有语言值，保留键/哈希位置；不自动增加语言或改菜单。保留已知翻译键引用，压缩表未知键、数据表和自定义属性先候选审查。BBCode 与格式占位符使用 Godot 校验规则。额外包加载顺序、自定义 --main-pack、脚本/插件私有格式、加密、字体与图片文字由 Agent 调查；本适配尚未运行验证。

Unity 资源扫描结合文件头与名称线索，包含改名 Bundle 和 `level数字` 场景文件。TMP 保留 `m_text` 专用处理；其他 MonoBehaviour 在候选模式中遍历字符串字段，包括 UnityEngine.UI.Text 与自定义组件。已有完整类型树时直接读取；缺失时按需使用可选依赖 `TypeTreeGeneratorAPI` 从本游戏 Managed DLL 或 IL2CPP 文件恢复结构。完整解析失败、无效 Unicode 字段保留诊断，不从字节片段生成任务。类型文件纳入源资源变化检查；操作时保留原游戏目录关系。

Unity TextAsset 按内容试读 JSON、无后缀 TSV/CSV、XML 和简单 `key=value`；这些格式也可来自 StreamingAssets，外置键值文件支持 `.properties` 和 `.txt`。保留 UTF-8/UTF-16/UTF-32 的编码与 BOM。无后缀表格、XML 和键值文本先进入候选审查；无表头表格用列号及同行值提供上下文，XML 用元素路径、属性、语言提示分组。Agent 选择显示字段和语言，保留键、其他语言及控制字段。XML 通用路线处理简单叶文本和属性；混合标记、DTD、游戏专用命令需临时适配。键值路线只处理单行字面值，不是完整 Java Properties 解析器；续行、Unicode 转义、转义键等交给专用或临时适配。只替换所选值，保留原键、分隔符空白、注释、空行和行尾，不按重复键合并条目。键值译文不能新增换行、NUL 或值首分隔符空白。

Tyrano 可直接读取标准 Electron `resources/app.asar` 内的剧情脚本，写回输出归档并从归档读回，使用说明和来源见 `tools/tyrano_asar.md`。不通过松散目录绕过加载；EXE 内嵌完整性校验或不明确的加载来源交给 Agent 处理。WOLF 的典型核心包和程序布局允许密态 `.wolf` 进入现有后端，不要求其头部为明文 DX；后端解包和 Game.json 导出仍必须成功。

候选模式还检查代码字符串：IL2CPP 当前仅支持标准路径的 metadata v29 字符串表，不需要新增 Python 依赖；其他版本明确报告未支持。Mono 使用可选 dnlib 辅助程序读取 Managed 中的非框架 DLL，构建与依赖来源见 `tools/unity_mono/README.md`。缺少工具时保留诊断，资源提取仍可进行。代码候选按索引／指令分别选择：Mono 写回只改变选中的 `ldstr`；IL2CPP 的同一索引可能被多处代码共享，修改影响所有引用位置，不能按单次剧情出现位置分别翻译。两者都使用 `text_group`，不保证剧情顺序；代码字符串能读回不代表游戏已实机验证，也不保证文本覆盖完整。检查 `write_supported`；签名或混合模式程序集不进入正式翻译任务。

代码字符串可能是“控制前缀＋显示文本”或查找键。选择前结合所在方法与实际内容判断，保留未确认用途的前缀和格式参数；当前通用读取器不自动拆分游戏自定义语法，Core 的通用占位符检查也不等于已识别这些语法。
