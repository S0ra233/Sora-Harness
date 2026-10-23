# Sora Harness

简体中文 | [English](README.en.md)

**Sora Harness** 是一个基于 Python 与 PySide6 构建的桌面端模型工作平台（Agent Harness）。

项目将通用的多协议模型调用、流式文件与命令工具、上下文缓存与压缩、多角色子智能体（Subagent）管理作为底层基础设施，并通过清晰的插件与技能接口承载上层业务（如多引擎游戏翻译工作台、本地混合检索资料库等）。整体设计强调工程边界明确、本地数据安全可控，以及长对话场景下的稳定请求前缀与受控上下文开销。

## 核心特性概览

### 1. 预填充（Prelude）与分层提示词预设

在许多精细任务（如特定文风翻译、代码审查或固定格式输出）中，仅靠 System Prompt 往往不如直接给出几组示范对话有效。Sora Harness 在底层与界面上原生支持了**预填充对话（Prelude）**：

* **欢迎页可视化编辑**：在新建对话页面即可直接添加、编辑成对的“用户 / AI”预填充气泡。编辑过程仅在本地保存草稿，不消耗 API 请求；当发送首条真实消息时，预填充内容随会话配置一同固化。
* **聊天气泡投影与保护**：进入会话后，预填充内容会作为受保护的前置气泡展示在聊天流开头，方便随时对照，但不会产生多余的运行轮次，也不会在后续步骤中被重复发送或被上下文压缩器误裁。
* **分类提示词与预设切换**：提示词按 `system`（身份、输出风格、格式）、`application`（环境、任务策略、文件保护、工具规范，仅工作模式加载）、`context`（用户画像、偏好、项目与任务资料）以及 `prelude` 分层组装。支持保存多套命名预设；修改全局设置默认仅影响新对话，已有对话可通过右上角菜单主动切换预设并在下一安全轮次生效。

### 2. 模型输出修改、历史回退与独立分支

针对模型输出偶发偏差或需要探索不同走向的场景，Sora Harness 采用**追加式日志（Append-only JSONL）+ 有效路径投影（Effective History View）**的机制，支持在不破坏原始审计记录的前提下灵活修正对话：

* **模型回答原位修改**：支持对每轮最终的模型公开回答直接进行纯文本编辑并保存。保存后，新的文本会替换当前有效历史中的助手回复，自动截断其后的旧对话后缀，并清除与旧正文绑定的原生回放状态（`ReplayState`），使后续轮次自然基于你修改后的回答继续推进；原始响应、思考过程与真实 Token 用量仍保留在底层日志中备查。
* **用户消息回退（Rewind）**：未压缩会话可从用户消息气泡触发回退（含中途补充 Steer 的精确边界回退，保留此前已完成的模型响应与配对工具批次）。回退后该条消息的文字与图片附件会自动放回输入框供重新编辑，不会自动重跑历史工具。
* **独立会话分支（Fork）**：可从用户消息节点确认后创建同项目下的独立分支会话（标题自动标记 `（分支）`）。分支仅继承所选节点之前的有效历史、当时的模型目标与已生效摘要，不复制未执行的旧队列或活动进程，方便在同一工作区下尝试不同方案。压缩成功后的整个会话隐藏回退入口，仍可查看、复制和分支；压缩前节点的分支按该节点当时的有效历史恢复。会话分支与回退均不回档项目文件。
* **整轮重新生成（Regenerate）**：退回当前轮次起点并重新发起模型请求与工具调用；若此前产生过文件或命令副作用，系统会在新请求尾部追加事实提醒，避免模型盲目假定文件已自动回滚。

### 3. 插件架构与游戏翻译套件（Translation Suite）

Sora Harness 将通用平台与垂直业务解耦。外部扩展（`extensions/*/plugin.json` 或本地目录/ZIP 安装包）与内置插件共用统一的注册边界（`plugin_api.core` 与 `plugin_api.ui` 分离，CLI 与后台执行不加载 Qt）。插件可以按需贡献业务动作、模型工具（Tools）、技能目录（`skill_roots`）、设置子页以及侧栏/独立窗口视图（`PageHost`）。

目前重点实现了**游戏翻译插件与跨宿主翻译技能（Game Translation）**：

* **多引擎检测与项目准备**：配合 Translation Core 支持 Unity、Godot、Ren'Py、WOLF RPG Editor、RPG Maker 等引擎项目的检测、文本提取与工作区准备（`prepare`），内置引擎进程树管理与超时回收（默认 900 秒可调）。
* **标准包流转与 Agent 自主临时适配**：模型可通过后台 `translation` 命令入口或已授权工具调用 `make_package`（导出待译包）、`apply_results`（校验并提交 UTF-8 JSON 译文数组）与 `build`（构建独立游戏副本）。遇到非标准格式或提取缺口时，Agent 可在授权范围内编写单文件 `WORK/adapter.py` 进行项目级临时适配，无需逐款游戏修改底层 Core 代码。
* **`/翻译` 双栏对照阅读器**：在输入框发送 `/翻译` 可呼出只读双栏阅读页；模型可调用 `show_translation` 选择有效工作目录，更新已打开页面或提供“查看翻译”入口，不强制弹窗。页面采用左原文、右已存译文的全场景连续对照布局，自动格式化 `说话人  [对白]`，支持双栏等高对齐、共用纵向滚动、视口分批排版以及同列跨条目选择复制。
* **按会话记忆与安全刷新**：阅读页仅展示经 Core 正式提交落盘的译文（未提交的聊天草稿不会混入正式视图）。相关工具结果触发页面异步重读项目，保持当前阅读锚点；每个对话绑定的翻译项目与 Scene 位置独立保存在 `views.json` 中，再次打开该对话的翻译页时恢复；独立窗口不会因普通聊天导航被强制切换。

## 通用底座能力

| 模块领域 | 功能与实现说明 |
| :--- | :--- |
| **四协议原生模型层** | 原生对接 OpenAI Chat Completions、OpenAI Responses API、Anthropic Messages 与 Google Gemini `generateContent`。支持公开思考过程（Reasoning）流式展示与折叠抽屉、原生签名/加密块续接、多档位推理强度（Effort）滑条，以及已有对话跨模型/跨提供商继续（按目标协议投影历史与配对工具结果，签名等原生块受原模型／连接身份约束）。 |
| **流式文件与命令工具** | 内置 11+ 核心工具并按 `read`、`exclusive`、`control` 分类调度：连续只读工具支持最多 4 项并发，写操作与命令通过跨进程写锁自动串行。`read_text_file` 支持 1～32 个文件区间批量流式读取（共享 20,000 字符预算，无文件总大小上限）；`search_text` 支持 `rg` 与 Python 64KiB 分块流式搜索及 `files_with_matches` / `count` / `summary` 模式；`edit_text_file` 强制唯一精确匹配并保留 BOM 与原换行符；`run_command` 基于 Windows Job Object 管理 PowerShell / Git Bash 进程树，默认采用 `compact` 模式折叠连续重复行，完整字节可通过 `read_tool_record` 按收据 ID 续读。 |
| **非破坏性数据保护** | 平台文件工具不提供删除接口，新建与移动操作拒绝覆盖已有目标，编辑操作禁止将非空文件清空；整理旧文件时通过 `create_directory` 与同卷冲突安全的 `move_file` 归档保留。设置页保存的聊天模型密钥使用 Windows 凭据管理器（`wincred`），模型连接保存凭据引用；兼容 CLI 可从指定环境变量读取。RAG Embedding 密钥与直接填写的 MCP 认证值仍可能保存在本地配置中，因此整个数据目录均需排除上传。命令和受信任的 Python 插件不是操作系统沙箱。 |
| **运行中引导、排队与提问** | 任务运行期间支持直接输入补充说明（Steer，在当前步骤与关联工具批次结算边界安全注入），或加入 FIFO 队列开启下一独立轮次；支持中断后统一继续（复用已落盘工具结果，不重跑已完成调用）。内置 `request_user_input` 非阻塞提问工具，在输入框上方停靠圆角问题卡，支持选项点选与自由回复。 |
| **前缀缓存与上下文压缩** | 保持 System / Tool Schema / 环境基线顺序稳定以复用服务端前缀缓存；实际命中取决于提供商策略、缓存存续和请求内容，不保证固定命中率，环境变化仅做尾部持久追加。支持手动 `/compact` 与阈值自动压缩：复用稳定前缀与当前模型，压缩后总输入目标约 10,000 Tokens（在预算内最多保留最近 2 轮完整原文，摘要目标约 2,000 Tokens，自动剥离覆盖区旧图片引用）。底栏按接口实际 usage 显示整轮缓存读取率，以及最近一次成功请求的输出 Token ÷ 请求耗时（包含首次等待、排除工具执行和失败重试）的 TPS；缺失字段不补估。压缩后清除旧用量，等待下次正常请求的真实 usage。 |
| **多角色子智能体（Subagents）** | 支持配置多套命名子角色（独立连接、模型、Effort、提示词与时限/步数预算）。后台子任务以独立进程运行，拥有独立对话历史，共享父会话目录租约与跨进程写锁。提供 `subagent`、`send_subagent`、`list_subagents`、`wait_subagents`、`get_subagent_result`、`cancel_subagent` 6 个标准工具，支持完成通知自动唤醒空闲主 Agent（带连续唤醒上限保护）及右侧 `/子智能体` 实时详情面板。 |
| **Skills、RAG 与 MCP 扩展** | **Skills**：支持项目 `.agents/skills`、用户目录、自定义路径与插件贡献，后台线程扫描元数据，`/` 菜单标记或由模型通过 `load_skill` / `read_skill_resource` 按需只读加载。<br>**本地 RAG 插件**：基于 SQLite 倒排 BM25 关键词检索 + 独立 Embedding 接口（OpenAI 兼容 / Ollama / Gemini）的 RRF 混合检索，支持项目/会话范围选择、片段去重、版本哈希续读及 TXT / Markdown 导入；PDF / DOCX 需先配置受支持的文档转换 MCP。语义／混合查询也会调用 Embedding 接口，可能产生用量费用。<br>**MCP 2.x**：支持 `stdio` 与 `Streamable HTTP` 协议，按轮次复用连接，超长输出走 `read_tool_record` 收据续读。 |
| **请求录制与运行诊断** | 支持按需开启“记录下一轮请求”（或 CLI `--record-requests`），在 HTTPX 发送边界原样保存转换后的请求实体字节（`body.json`）、尝试记录（`attempts.jsonl`）与清单（`manifest.json`），自动脱敏鉴权头，且诊断落盘与正式模型重试完全隔离。 |

## 目录结构与模块分工

```text
.
├── desktop/                         # Harness 桌面端源码
│   ├── cli.py / gui.py / bootstrap.py   # CLI 与桌面 GUI 启动入口、运行时组装
│   ├── plugin_catalog.py                # 插件延迟加载登记表（隔离业务与 UI 入口）
│   ├── configuration/                   # 接口目录、模型适配规则（adapters.json）、子角色配置存取
│   ├── credentials/                     # Windows 系统凭据管理（wincred）读写封装
│   ├── environment/                     # 本地程序探测（Python / Git / rg / Shell）与环境快照
│   ├── llm/                             # 模型协议层
│   │   ├── protocols.py                 # 协议静态目录与原生续接校验
│   │   ├── history_projection.py        # 跨模型/跨协议发送视图投影与签名指纹核对
│   │   ├── usage.py / reasoning.py      # 统一 Token 用量归一化与 Effort 档位映射
│   │   └── providers/                   # Chat Completions / Responses / Messages / Gemini 实现
│   ├── prompts/                         # 默认提示词配置（default.json）、压缩指令（summary.txt）与前缀装配
│   ├── tools/                           # 工具契约、注册表、收据存储与内置工具实现
│   │   ├── filesystem/                  # 流式读取、rg/Python 搜索、安全创建/编辑、同卷移动
│   │   ├── command/                     # PowerShell / Git Bash 配置与 Job Object 进程树回收
│   │   └── questions.py                 # 非阻塞用户提问工具（request_user_input）
│   ├── skills/                          # SKILL.md Frontmatter 解析、目录发现与附属资源只读访问
│   ├── runtime/                         # 核心执行层
│   │   ├── agent/                       # AgentLoop 主循环、步数/工具配额与检查点
│   │   ├── conversations/               # 追加式 JSONL 存储、历史修订/分支、运行中控制（Steer/Queue）
│   │   ├── context/                     # Token 预算估算与前缀复用上下文压缩（Compactor）
│   │   └── subagents/                   # 独立子进程 Runner 与状态帧通信
│   ├── application/                     # 应用服务层（会话编排、预设管理、SubagentManager、任务门控）
│   ├── ui/                              # PySide6 桌面界面层
│   │   ├── shell/ & common/             # 主题、三色容器、可中断动画、自定义圆角弹层与公共组件
│   │   ├── workspace/ & sidebar/        # 工作区宿主、PageHost 插件承载、会话目录与归档管理
│   │   ├── chat/                        # 虚拟化消息视口、工作过程折叠块、工具详情、输入台与 / 菜单
│   │   └── settings/                    # 接口配置、提示词预设、技能、MCP、子智能体与归档设置页
│   └── plugins/                         # 内置业务插件（translation 翻译套件、subagent、model_trial）
├── game_translation/                # Translation Core：提取、包协议、校验与独立副本构建
├── run.py                           # Translation Core 命令行入口
├── process_support/                 # 主程序与引擎共用的进程树支持
├── skills/game-translation/         # 可供不同 Agent 宿主复用的翻译 Skill
├── tools/                           # 可选引擎工具说明与桥接源码（不含下载的二进制）
├── desktop-extensions/              # 独立插件及接入示例
│   └── rag/                        # BM25 + 向量混合检索与辅助页面
└── desktop-release/                # PyInstaller / Inno Setup 构建入口
```

## 快速开始

### 1. 环境准备

项目运行在 Windows 环境下（推荐 Windows 10 1809 及以上版本，Python 3.11+）：

```bash
python -m pip install -r desktop/requirements.txt
```

主要运行时依赖包括：`PySide6`（Qt6 桌面界面）、`httpx`（异步网络请求与流式 SSE）、`Pygments`（后台代码语法着色）、`PyYAML`（技能元数据解析）与 `mcp`（官方 MCP SDK 2.x）。

以上命令在仓库根目录执行。建议先建立虚拟环境，再安装依赖。Translation Core 的可选引擎依赖不包含在这份桌面运行依赖中，按 `tools/` 的对应说明另外准备；不能据此认定任意游戏均可提取或构建。

### 2. 启动桌面端（GUI）

```bash
python -m desktop.gui
```

源码启动默认在 `desktop/local/` 保存数据；可用 `python -m desktop.gui --data-dir "D:/HarnessData"` 指定独立数据目录。安装版默认使用 `%LOCALAPPDATA%/DesktopHarness`。

首次启动后：
1. 进入左下角 **设置 → 模型**，新建 API 接口连接（支持填写完整请求地址或基础 URL，密钥保存在当前系统的 Windows 凭据管理器中），拉取或添加模型并设为默认。
2. 在欢迎页可选择 **普通聊天（Dialogue）** 或绑定本地目录开启 **项目工作（Work）**；也可以直接在欢迎页点击气泡配置本次会话的 **预填充（Prelude）**。
3. 在输入框中输入 `/` 可快速呼出本地命令与视图菜单；点击条目先填入输入框，发送后执行（如 `/compact` 手动压缩、`/翻译` 打开双栏翻译阅读页、`/子智能体` 查看后台子任务、`/资料库` 打开 RAG 面板，或从“技能”分组选择实际列出的技能指令）。

源码首次使用独立 RAG 插件时，可执行 `pwsh -NoProfile -File desktop-extensions/install-rag.ps1`，重启后在插件页配置；该命令只部署插件代码，不导入语料或开启 Embedding。

### 3. 命令行模式（CLI）

除图形界面外，平台也提供完整的命令行入口，支持单次试运行、工具循环与持久会话续接：

```bash
# 先在GUI保存模型配置；my_project须为已有目录。使用其他数据目录时调整settings-file。
# 持续会话模式（新建或指定 --conversation <UUID> 继续，继续时仍需选择原连接）
python -m desktop.cli --settings-file desktop/local/settings.json --mode conversation --workspace ./my_project --prompt "先查看目录结构并总结核心模块"

# 按需录制本轮最终发送给服务端的完整 HTTP 请求体（默认保存至 desktop/local/request-records/）
python -m desktop.cli --settings-file desktop/local/settings.json --mode conversation --conversation <UUID> --prompt "继续" --record-requests

# 手动对指定会话执行上下文压缩
python -m desktop.cli --settings-file desktop/local/settings.json --mode conversation --conversation <UUID> --compact
```

### 4. 构建独立发行包与安装程序

在仓库根目录运行 `pwsh -NoProfile -File desktop-release/build.ps1`（或双击 `desktop-release/build.cmd`）；首次构建需要联网准备依赖与编译器。完整参数见 [构建说明](desktop-release/README.md)。

项目包含基于 `PyInstaller`（双入口：主程序 GUI 与独立后台 Runner EXE）和 `Inno Setup` 的打包配置：

* 构建完成后会生成独立产物目录与 `DesktopHarness-Setup-0.1.0.exe` 安装包。
* 构建过程默认隔离个人数据目录（`local/` 下的会话日志、提示词预设、插件数据与凭据引用不会随安装包打包导出），安装器不覆盖用户数据；需要沿用指定配置时使用相同的 `--data-dir`。源码更新后需重新打包，已有 EXE 不会自动加载新源码。


## 扩展与使用边界

插件工具与辅助 UI 接口见 [扩展说明](desktop/docs/EXTENSION_API.md)，最小页面示例见 [扩展示例](desktop-extensions/examples/README.md)。翻译流程见 [翻译 Skill](skills/game-translation/SKILL.md)，资料库部署与检索边界见 [RAG 说明](desktop-extensions/rag/README.md)。

本仓库发布源码、公共默认提示词、插件与构建脚本，不包含个人模型连接、密钥、会话、请求记录、个人预设、语料索引、真实游戏或翻译工作目录。完整请求记录可能含用户正文、工具返回和图片编码，鉴权头脱敏不等于正文不敏感，请勿随仓库上传。

不同引擎的支持范围以已适配资源格式和实际诊断为准；商业游戏的私有资源、加密、字体及加载逻辑可能仍需项目级适配。协议实现、源码静态检查与特定场景使用结果不代表所有提供商或游戏均已验证。

## 许可证

项目自有源码采用 [MIT License](LICENSE)。保留的第三方许可见 [RPGMTL 许可](game_translation/formats/RPGMTL-LICENSE.txt)；PySide6／Qt、MCP SDK、其他运行依赖及可选外部引擎工具分别遵循各自许可证。发布二进制安装包时还需处理实际随包依赖的许可与分发要求。

## 作者

项目作者：[S0ra233](https://github.com/S0ra233)。
