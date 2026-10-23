# 扩展接入接口 v1／v2

## 2026-10-19 扩展管理、资料范围和文档转换

原v1插件继续读取；v2是向后兼容的新增能力标识。v2导出`convert_document(data_root, path, server_id, tool_name)`、`document_readers(data_root)`及UI的`DisplayRow`。`ToolContext.reference_scope_id`由会话服务提供，子任务采用父会话编号，不进入API JSON。使用这些新能力的插件应声明`api_version: 2`、`requires_host_api: 2`，旧宿主会明确拒绝，避免缺接口后才崩溃。

插件管理页可安装本地ZIP或单个插件目录。先有界复制到`extension-versions/<批次>`，校验路径、清单、入口及接口版本，不执行插件工厂；完成后原子保存`extension-packages.json`，下次启动才加载。相同ID的新版本替代扫描入口，旧版本、失败候选和`plugin-data/<id>`都保留；没有热重载、自动依赖安装或在线更新。安装上限256MiB／10000条目，不接受链接、加密ZIP或越界路径。此接口管理可信的本地Python代码，不是代码沙箱。示例见`desktop-extensions/examples/reference_page`，默认关闭，安装后启用并重启。

`convert_document`首批仅接受本地PDF／DOCX，通过用户已启用、已同步、stdio的`convert_to_markdown(uri)`工具工作。它使用单次MCP连接作用域和既有超时／取消，不安装转换服务、不调用主模型、不把远程服务当本地文件系统。RAG保存原字节、转换正文和来源哈希；转换后原文件变化拒绝发布。转换文本行号不伪装成PDF页码，扫描件／版面质量取决于所配服务。

新增内置页面插件`file_compare`和`diagnostics`只贡献UI，不增加模型工具。`/比对`、`/诊断`仍先填草稿，发送后由原页面指令路由打开。比对只读；诊断不读取请求正文、不发额外API，也不改变压缩后的用量口径。

2026-10-15：本轮仅增补UI入口，工具／请求／持久化接口不变。插件从`desktop.plugin_api.ui`取得`MotionButton`、`CardSurface`、`AnimatedDisclosure`、`CardPopup`、`SurfaceDialog`、`ClickComboBox`、`ElidedLabel`和`theme`，避免直接依赖宿主内部文件。

`MotionButton(text, parent, variant="secondary", size="medium")`的新增参数均为可选关键字：variant可为primary（深色主操作）、accent（蓝色发送）、secondary（浅底无框）、outline（细边框）、ghost（悬停显底）、danger（危险操作）、segment（分段选中）；size可为small／medium／large／icon，不传则沿用Qt和原布局尺寸。shape可传rounded／pill，icon_position可传left／right。外观不授予权限，也不负责执行业务；`set_variant`可将已有确认按钮改为危险外观。未传variant的旧插件保留原objectName兼容路线。

`CardSurface(content, parent, radius=16, padding=12)`只负责单层圆角外壳。`CardPopup.present()`打开；`dismiss(restore_focus=True)`在确认或Esc后尝试返回打开前的控件，默认False适用于点击别处或切换弹层；旧无参调用仍可用。只有原窗口仍活动、原控件可用且没有新弹层时返回焦点。Popup的Tab在可交互子项内循环；`/`候选仍保留输入框焦点，点击或Enter只填入指令，发送后才执行。`SurfaceDialog`沿用QDialog模态和键盘机制，不新增模型请求入口。

2026-10-13：扩展接口沿用v1。独立RAG插件已通过本接口接通，源码见`desktop-extensions/rag`，静态审查与生产打包，真实语料、界面及Embedding待用户实测；没有增加Harness内置RAG工具或会话字段。

## 本地插件

从插件管理页点击“打开扩展目录”。源码模式对应`desktop/local/extensions`，安装模式对应当前数据目录中的`extensions`。每个插件一个子目录，包含`plugin.json`及自己的Python模块：

```json
{
  "api_version": 1,
  "id": "my_reference",
  "name": "项目资料",
  "description": "检索与辅助查看项目资料",
  "default_enabled": false,
  "tools_entry": "tools.py:create_tools",
  "ui_entry": "ui.py:create_ui",
  "skill_roots": ["skills"]
}
```

上述目录与模块由插件作者提供，不是本批已安装的插件。清单只填写实际实现的入口；可以只有工具、只有页面或只有技能。另支持`business_entry`及`tool_ui_entry`。入口必须是插件目录内的`文件.py:函数名`；模块可相对导入同目录代码。插件开关、清单和代码改变后重启应用加载，不支持运行中换代码。清单API版本不匹配、目录越界、重复ID或入口损坏时记录该插件诊断，其他插件继续。

业务／工具文件使用`desktop.plugin_api.core`，界面文件使用`desktop.plugin_api.ui`。后者导入Qt，不能从后台业务模块导入。

| 入口 | 参数 | 返回与职责 |
| --- | --- | --- |
| `create_tools(host)` | `PluginHost` | `Iterable[ToolDefinition]`；纯登记，不在工厂中读大文件、联网或启动进程 |
| `create_plugin(host)` | `PluginHost` | `Plugin(plugin_id, actions)`，给既有业务动作入口使用，可省略 |
| `create_ui(settings_path, *, services)` | 设置路径、`UiServices` | `Iterable[UiContribution \| UiPageDefinition]`；`services.plugin_host`提供所属插件目录 |
| `create_presenters(host)` | `PluginHost` | `(工具名, formatter)`条目；只格式化真实结果，不改变成功状态 |

`host.code_directory`是代码目录，`host.data_directory`是当前数据根下的`plugin-data/<id>`。平台不替插件创建数据库、安装依赖或修改其业务数据。公共Core接口提供`ToolSchema`、`ToolDefinition`、`ToolDisplay`、`ToolError`、`ToolContext`、`ToolArtifacts`和工作区路径检查函数；名称建议使用`插件id_动作`，须是ASCII字母／下划线开头、最多64字符。

工具的`execute(arguments, context)`是异步函数，返回JSON对象。`context.workspace`及`allowed_tools`由宿主确定，模型参数不能另传目录来扩大权限。读取项目文件复用`resolve_workspace_path(value, context.workspace, kind="file")`；创建目标复用`resolve_new_file_path`。纯读取声明`execution_class="read"`；修改和命令保留默认`exclusive`，沿已有父子修改锁串行处理。错误使用`ToolError(code, message, details=...)`；取消继续抛出，不伪装成功。实际副作用需要记录状态，避免保存故障诱导重复执行。

外部工具声明`default_profiles=("desktop_work",)`后，由插件启用开关在工作请求准备时加入。关闭后下一独立请求移除，当前运行维持快照；不写入新会话永久默认工具列表。普通聊天只开放已有技能／提问能力，不自动接入工作工具。登记冲突整批拒绝，不能覆盖平台工具。

页面复用`UiPageDefinition`：填写所属`plugin_id`、稳定`page_id`、`aliases`、`create_page(context)`，`presentation`选择`aside`或`window`。宿主统一创建标题栏、圆角、关闭和展开；插件创建内容控件。创建函数返回`UiContribution`，可绑定`bind_context`、`invalidate`、`close_check`与`dispose`。主Agent请求仍在原聊天中；辅助页面没有主聊天发送入口。按`UiPageContext.workspace`读取已审核的Entry／Task或插件资料，项目变化通知只刷新辅助展示。

Python插件是用户信任的本地代码，不是操作系统沙箱。公共路径函数和工具范围约定不能限制任意Python代码；接入第三方插件时需核对其实现与依赖。这里只说明实际执行边界，不用提示词代替权限检查。

## MCP工具

设置→MCP工具→新建服务。默认关闭。支持本地程序`stdio`与远程`Streamable HTTP`；配置保存在当前数据目录的`mcp.json`，不混入模型提供商配置。

本地程序填写启动程序、逐行参数和可选启动目录；远程服务填写实际`/mcp`地址。高级抽屉填写环境变量／请求头JSON和时限。值支持`${MY_MCP_KEY}`环境变量引用；只在实际连接时解析，不放进模型请求或子任务日志。直接填写的键值仍保存在本地配置中。

点击“连接并同步工具”才在后台建立连接、协商协议并按游标读完整工具目录。成功后原子保存；失败、取消、重复游标或预算超限不公布半份目录。保存连接参数改变时清除旧工具快照，需要重新同步。配置目录不自动扫描或安装MCP服务器。

启用后，工作运行将公开声明适配为`ToolDefinition`。服务UUID和远端名称派生稳定工具名；名称／参数／说明在一轮内固定，认证、协商结果和连接状态不加入工具说明。同步声明或切换开放工具会真实改变API前缀，不能承诺旧前缀继续命中。

模型调用→原`ToolRegistry.execute`→MCP适配→本轮连接池→SDK的`Client.session.call_tool`→真实`is_error`转换为工具状态→原检查点、收据与UI。首次实际调用才连接，每个服务在该轮复用一个连接；连接由独立异步任务持有，建立与关闭保持在同一任务，不能跨GUI工作线程的事件循环复用。连接时核对服务端声明与快照，不一致时拒绝执行并提示重新同步，不在当前模型循环改Schema。

默认连接20秒、调用120秒，可在设置中调整。未知副作用一律按`exclusive`处理，不根据服务端只读注解放宽执行权限；失败、超时和取消不自动重试。超时／停止先回收连接，后退出原修改锁；远程服务是否实际停止仍取决于服务端，结果标明未知。stdio进程树关闭沿官方SDK实现。服务端必须自行限制可访问的文件和资源；Desktop工作目录不是外部MCP进程或远程服务的沙箱。

模型结果最多约24000字符，完整返回保存在本调用的`mcp-result.json`；使用`read_tool_record(record_id, file="mcp", offset, length)`继续读取。图片、音频及资源内容保留在该文件，首版不把它们自动转换成模型附件。完整记录保存失败只报告记录告警，不把已成功的服务结果改成失败。每个结果仍带调用ID与真实收据。

子Agent只继承父任务已开放的服务／工具，独立连接。子日志存连接指纹和工具声明，不复制认证键值；子任务启动时配置已变则拒绝换用新版连接。摘要可以复用工具声明以保持前缀，但不启动MCP或执行任何工具。

首版未接MCP资源／提示词目录、采样、服务端用户提问、OAuth及旧SSE传输。它们需要各自的交互和授权范围，不能用本批“工具已接入”推导为全部MCP能力已实现。

## RAG插件边界

RAG在`desktop-extensions/rag`实现自己的TXT／Markdown导入、SQLite关键词索引、原文版本及Embedding适配，贡献`rag_libraries`／`rag_search`／`rag_read`、配置页与`/资料库`辅助页。独立存储在`host.data_directory`，安装到实际数据根的`extensions/rag`；默认启用固定工具声明，语义接口默认关闭且不自动联网。常规检索通过原工具返回主Agent，辅助页复用同一服务，不发送主聊天模型请求。工作会话及其子任务可使用，普通聊天不自动开放外部工具；插件清单、ToolContext和Provider无需新增字段。字段、范围、预算及使用方法见[插件说明](../../desktop-extensions/rag/README.md)。

参考实际源码：[Pi扩展加载与整批提交](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/extensions/loader.ts)、[Codex动态工具调用与取消](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/dynamic.rs)、[官方MCP客户端与传输](https://py.sdk.modelcontextprotocol.io/client/transports/)。本项目复用贡献边界、真实结果和资源生命周期；不复制其整套运行框架，也不声称读取闭源Desktop内部实现。

## 2026-10-11 工具结果与新参数

平台不在Provider请求发出前重新整理已保存的工具历史。插件需返回稳定、可解释的有界结果；执行状态、调用ID、结果范围和续读入口不能因节省文本而省略。原文、译文条目及精确修改依据不做通用摘要。完整内容可保存在工具自己的收据目录，原有访问校验保持；MCP的原始结果继续保存在mcp-result.json，本批没有自动给任意外部结果套字符串过滤器。

| 工具 | 参数与结果 | 兼容与范围 |
| --- | --- | --- |
| read_text_file | 原path方式，或files=[{path,start_line,max_lines,cursor}]；path/files二选一 | 批量所有区间正文合计20000字符，每个最多200行，最多32项；只填写各条目需要的行或游标字段 |
| 批量读取结果 | entries逐项标ok/error；next_files直接作为下次files参数 | window_complete表示请求区间已返回，eof表示到文件末尾；错误条目不自动重试。取消结果可带batch_read_progress |
| search_text | output_mode为content/files_with_matches/count/summary | 默认content兼容；context_lines仅用于content，count/summary扫描仍有时限与字节等预算 |
| 紧凑搜索结果 | paths，或counts[{path,matched_lines}]，或合计matched_lines | 一行多次出现只计一行。count表最多max_matches项但继续累计扫描；result_truncated表示表省略，coverage.scan_complete决定合计是否完整 |
| run_command | output_mode为compact/raw，默认compact | 成功仅折叠连续重复行，大输出保留首尾；raw与失败保留原末尾20000字符，完整字节保留在output.log |

命令output_summary分别报告原字节数、原字符数、显示字符数、折叠行数和省略字符的范围。这些不是token节省量、缓存率或API用量。小日志不会因首尾窗口预留被截断，整理反而变长时采用原文；未知类型日志不猜命令、不删除“无关”内容。

模型Provider继续接收统一ModelRequest。标准内置Provider在当前请求的异步上下文中标记实际输出，出现正文、思考片段或工具调用后遇到可恢复断流，不自动整轮重发。预先显示的思考状态、心跳及角色声明不算输出。缺少完整结束状态的工具仍不执行，也不把半截流当作完整回答。没有增加自动续写请求、重跑工具或跨模型降级。

## 2026-10-19 执行类别与贡献生命周期

`execution_class="read"`表示可与同批独立读取并发，当前最多四项；插件应确保处理函数可重入，不通过此声明修改项目或共享业务状态。未声明仍按`exclusive`处理；`exclusive`／`control`是批次边界，结果全部结算后才开始后续调用。模型历史的结果按原调用顺序排列，运行通知和检查点按实际完成时间及调用ID关联。`tool_started`只在取得执行许可后产生，等待许可阶段取消不会标成handler已运行。

`execute`须返回可严格JSON编码的对象，`ToolError.details`须为对象或None；具体参数字段由handler验证。外部读取工具的意外异常／非法结果反馈`extension_execution_error`，平台一致性错误仍停止整轮。修改／控制工具的未知执行结果、未预期超时或I/O错误会停止后续批次，不能据错误自动重跑。已知收据应通过`ToolArtifacts`维护；取消继续传播，保存的副作用状态必须真实。

UI工厂只登记声明和创建内容，不应在构造阶段启动无法回收的任务。宿主会保管已经交出的贡献：创建／绑定／登记失败后，只释放允许关闭的实例，活动任务与关闭检查仍保留；注册表和PageHost退出时调用`dispose`。`dispose`应可重复调用并释放插件自己的订阅／引用，不能删除用户资料，也不能在任务尚未退出时销毁其执行所有者。工厂抛错前尚未交给宿主的资源由工厂自行清理。正常辅助页面仍不提供独立的主模型请求入口。
