# Agent 临时适配

示例采用源码 CLI 写法；安装版的对应入口见[运行入口](runtime.md)。

通用引擎已能打开资源，但某种序列化布局、字段用途或脚本语法尚不适配时，由 Agent 分析样本并在已授权的任务范围内决定是否编写临时适配。它也适用于已知商业框架的布局变体，不必先证明整款游戏使用了全新的格式。若用户只要求查看资源，不继续提取或翻译；用户明确停止后不继续处理该游戏。不要把可见字符串片段当作可靠写回位置，先确认结构、显示字段、控制数据及保存方式。

## 从通用读取到项目适配

1. 先复用通用引擎确认容器、对象身份、可读内容与读取失败位置。`inspect` 提供有界结构证据，不会解析全部对象；Unity 的 `candidates` 给出当前可解析候选与警告，未识别资源仍需要针对性查看。已有诊断可复用，不为每个字段重新扫描整款游戏。
2. Agent区分三类情况：已有适配足够；数据可读但显示字段／语言需要选择；资源布局或脚本规则需要补充代码。前两类沿原流程或候选审查，不为每次选择生成新适配器。第三类分析结构和实际消费／加载方式，不能仅凭扩展名、日文片段或一个可读脚本认定它是游戏运行时的完整文本来源。
3. 原任务已授权提取／翻译且信息足够时，自行生成并审阅项目内适配代码，不为每个新字段重复询问用户。缺少决定性样本、需要扩大操作范围或新增尚未获准的依赖时，说明具体缺口再询问；不要把通用读取失败直接交还用户。
4. 用下面的 `--adapter` 显式接入。临时适配返回Entry、诊断和依赖，Core负责建立Scene／Task、包版本、提交状态和副本构建；无需增加Harness工具、修改内置引擎注册表或直接编辑项目状态。

## 文件放置与加载

将一个自包含的 `adapter.py` 放在新的 WORK 目录直属位置。第一次 prepare 时，WORK 除此文件外应为空：

```text
WORK/
└─ adapter.py
```

```text
python run.py prepare GAME WORK --adapter WORK/adapter.py
```

Python API 为 `prepare(game_dir, work_dir, adapter_file=...)`。后续 package、apply、build、verify 均使用原入口。可与 `--text-locale ja` 配合；不要假定游戏文本全是日语。调查输出放在其他目录，不能把已经包含解包文件／诊断的非空目录直接作为首次WORK。

临时文件是会执行的 Python 代码，仅显式接入本次编写或审阅过的代码，不自动加载游戏目录中的脚本。Core 保存文件绝对路径和 SHA-256；重复 prepare 或读写加载时若文件缺失/改变则停止。路径固定、不是可搬运插件包。依赖现有 Core 和已安装库，不把本地辅助代码拆到未记录的相邻文件；摘要只约束这个适配文件，不是整个运行环境的完整性保证。

当前是单文件、项目级适配入口，不是自动发现资源插件的目录。不能假定把多份代码放进 `adapters/` 就会被加载，也不要修改原Core文件、覆盖原解析函数或运行时全局替换分派表来实现游戏专用补丁。

## 适配职责与Core衔接

临时模块遵循与内置引擎相同的函数约定，不另外管理任务状态：

| 函数 | 输入和返回／职责 |
| --- | --- |
| `probe(game)` | 接收游戏 Path；根据实际结构返回至少含 `family`、非空 `evidence` 的字典，不适用返回 None。Unity 自定义对象可保留 `family="unity"`，补充自己的 variant 和 data_dir。 |
| `prepare(info, work)` | 返回含 `root` 的资源字典。无须解包时 root 为 info["game_dir"]；需要展开时仅写入 WORK 子目录。 |
| `extract(info, resources)` | 优先返回既有 `ExtractionResult(entries, warnings, reports, dependencies)`；旧 `(entries, warnings)` 仍兼容。Entry 至少含 `file`、`path`、`text`、`type`、`format`；提供可靠的 scene、scene_kind、speaker/structure，以及必要的对象定位。file 是相对资源 root 的路径，path 在文件/对象内唯一。不自行生成 task_id 或 scene_id。 |
| `read(path, entries)` | 按 entries 顺序返回指定位置的当前文本，定位失败报错；不修改文件。 |
| `write(path, items)` | items 为 `(entry, translation)` 列表；核对原值与身份，只修改传入的副本文件，返回写入条数。 |
| `font_options(info)` | 返回支持选项；不支持时返回空列表。 |
| `configure_font(output, info, font=None, tmp_font=None)` | 返回结构化字体结果；无请求可复用 unchanged_font()，不支持的字体请求应报错，不能默默忽略。 |

使用 `asset` 等额外字段保存对象身份，使用 `text_locale` 提供明确的语言代码。Scene 依据实际命令/对象关系组织，不凭字符串排列捏造剧情顺序。显示字段与资源路径、标签、条件式分开处理。可复用已有 formats/engines 的函数，避免复制整套 Unity 读写代码。

临时入口接管该项目的适配函数，不会自动继承Unity可选构建钩子。可以在文件内复用通用Unity的probe／prepare和已支持格式读写，为特殊资源提供独立处理；实际采用的prepare_build／check_build、finalize_build、copy_resources或字体相关钩子也应明确提供或转交。不要因只补了extract就漏掉原有加载检查、目录关联和构建收尾；不需要的能力不伪造成功。

`read`／`write`按适配器实际输出的format和对象位置分派，特殊格式由本文件处理，不必加入全局Unity格式表。同一容器混有普通和特殊对象时，核对原值、汇总修改并保持一致的保存顺序；不能让两个旧快照分别保存而互相覆盖。通用提取已覆盖的同一位置不重复建Entry，未解决警告保留，不把某个对象成功解析当作整文件完整覆盖。

参与解析的源依赖，例如资源根目录内的类型程序集和元数据，应通过ExtractionResult.dependencies记录为相对资源根目录的路径，供Core计算来源版本；不把依赖文件当成显示文本，也不把目录外文件冒充相对源路径。未知资源保留报告，只有确认能可靠定位和替换的内容才建立正式Entry。只读实现尚未具备写回能力时应明确拒绝写回，不能用空函数绕过必需接口。

结果路线为：`prepare --adapter → 临时extract → ExtractionResult → Core建立Scene／Task → package → 主Agent翻译／审核 → apply → build调用临时read／write及所需构建钩子`。适配器只负责资源，不自行调用模型、分配子Agent或维护另一套任务状态。

## 失败与版本

Core 不沙箱隔离适配代码。临时适配必须保留源游戏、遵守传入的资源和输出路径，不直接修改 tasks.jsonl，也不代替 Agent 调用模型。提取没有成功时不编造 Entry；尚未支持写回时不能把空操作当成成功。

适配代码需要修改时，保留已有项目及代码版本，在新的工作目录放置修正版并重新 prepare；不要绕过摘要检查。当前不自动迁移不同适配版本之间的译文。只有遇到可复用的实际样本后，才讨论把临时适配整理进主项目。
