# 资料库插件

## 1.1.0：范围、混合检索、引用与文档导入

此版要求宿主扩展API v2，v1宿主会拒绝加载。可在插件管理页从本目录安装新版，旧版本和语料保留；重启后生效。

`/资料库`辅助页的“本次资料范围”可绑定项目或会话，默认继承启用库。两个范围取交集；显式空列表表示不允许任何库，恢复继承移除该层限制。设置保存在插件数据的scopes.json，检索、列库和chunk_id续读都核对范围；子任务使用父会话范围。改变范围不会删除已进入对话的旧资料；分支是新会话，继承项目范围，需独立选择会话范围。范围约束不是Python代码沙箱。

`hybrid`需要实际启用并建立Embedding索引；先查关键词和语义候选，按RRF名次融合，再按完整片段哈希及明显重叠去重，保留两路覆盖状态，不把未完成索引当全库检索。没有新增重排接口或向量数据库。hybrid枚举会改变rag_search声明，新版首次旧前缀不保证命中，之后声明固定。

聊天工作过程里真实rag_search／rag_read结果下方有“查看来源”入口，点击在资料页定位同版本chunk；也可手动粘贴chunk_id。模型历史与原工具结果不改，资料关闭或不在当前范围时拒绝续读。

导入支持TXT／Markdown及通过本地MCP转换的PDF／DOCX。先在设置的MCP页面自行接入并同步MarkItDown兼容的convert_to_markdown工具，再到资料库配置选择转换服务；选择自动保存，不联网，导入时才调用。没有安装服务、OCR或新依赖。原文件字节保留，转换正文单独保存；引用是转换Markdown的行号，不是PDF页码，旧DOC不在本批范围。单文档仍最多64MiB，转换正文最多16Mi字符。以下首版记录保留为历史说明，变更以本节为准。

这是通过 `plugin.json` 加载的独立 Python 扩展。Harness 的内置工具、ToolContext、Provider 和会话格式没有增加 RAG 字段。插件默认启用，工具声明固定为 `rag_libraries`、`rag_search`、`rag_read`；资料库、Embedding 设置及索引变化不改这些声明。

## 使用

1. 重启应用，在「插件 → 资料库」新建资料库，选择 TXT／Markdown 导入。可建多个库，关闭的库不会进入检索或续读。
2. 在工作会话让模型检索资料，或输入 `/资料库`，发送命令后打开右侧辅助页。页面只检索和查看资料，正常模型请求仍从主聊天发出。
3. 子 Agent 继承父任务已开放的插件工具，因此也能检索。同一工作会话中的翻译、写作、RP 不需要各自重建检索工具。旧式直接调用生成接口的业务动作不会自动执行 RAG；这里没有给 Core 增加隐式检索或自动资料注入。

普通聊天当前不开放外部工具，但可以打开资料页。资料库是用户启用的应用资料，不以模型传入的路径扩大权限；所有工作会话共用启用的库，可传 `library_id` 缩小范围。首版没有每会话绑定、自动监视目录、PDF／Word／JSONL、专用双语字段或翻译流程自动检索。

## 关键词

无需联网。中文／日文等连续文字按相邻双字索引，单字查询有单字索引；英文按词，不做词干还原。原文不做这些转换。BM25 排名只反映关键词匹配，不代表事实可靠性或语义理解。

按段落／行／句末尽量切为约1400字符、120字符重叠片段；位置按解码后原文字符和行号计算，不伪装成原始字节偏移。自动编码只认 UTF-8／带 BOM 的 UTF-16，旧中文／日文文件可显式选 GB18030／Shift-JIS。单文件导入最多64MiB，可多选，一批最多10000份。原始字节和旧版本保存在插件数据库，不修改来源文件。

同一库同一路径是同一文档，内容哈希、编码及切片版本共同确定资料版本；未变化的文件跳过。每份文件的原文、片段、词项和有效版本在一个事务提交。失败／取消不会发布该文件半份索引，之前完成的文件保留。没有自动删除旧版本。

`rag_search` 默认返回8项，最多20项，片段正文合计最多12000字符、每项最多900字符。返回来源、版本、行号和 `chunk_id`；截掉的片段正文可用 `rag_read` 看原文。续读是固定版本，即使重新导入，也不把旧 ID 指向新文字；`is_current_revision`说明是否仍为当前资料版本。

关键词累计最多100000条倒排记录；达到预算时 `coverage.scan_complete=false`，排名只是部分候选。不是全库结论，模型不能把没有命中解释成资料里一定没有。

## Embedding

配置在插件页，与聊天接口独立。保存本身不联网；启用语义接口后，点击「建立／补齐向量」才把所选启用库的片段发送到配置的服务。模型或辅助页选择 `semantic` 检索时，只发送查询词的 Embedding 请求。

| 协议 | 地址填写 | 请求 |
| --- | --- | --- |
| OpenAI兼容 | 含版本的基础地址，例如 `https://接口/v1`，也可完整 `/embeddings` | `model`／`input`／`encoding_format=float`；可选 `dimensions` |
| Ollama | 服务根地址，或 `/api`、完整 `/api/embed` | `/api/embed`，`truncate=false`，不默默截掉片段 |
| Gemini | 基础地址填至 `/v1beta` | `models/<模型>:batchEmbedContents`；区分 `RETRIEVAL_DOCUMENT`／`RETRIEVAL_QUERY` |

填写真实 Embedding 模型，不是普通聊天模型。密钥可填 `${RAG_EMBEDDING_KEY}`；只有实际请求时解析。直接填入的密钥在本地插件配置里保存，界面掩码，工具、索引空间及错误不回显密钥。服务端 HTTP 错误只显示状态码，没有自动重试或跳转。

协议、地址、模型和维度决定向量空间；不同空间不混算。同空间片段已完成则跳过，批次完整验证后事务提交。失败／停止保留已完成批次，主动再次建立可补齐。没有自动调用模型去摘要资料，也没有把资料全文加入 system 或每轮 history。

使用标准库 SQLite 和现有 httpx，没有新增向量库依赖。首版采用归一化 float32 向量的余弦扫描，每次最多20000向量；超过时 `scan_complete=false`。尚未全部建立时 `index_complete=false`。大库要另评估索引方案，不能据这版宣称任意规模语义检索都快。

## 安装与数据

原项目：运行 `desktop-extensions/install-rag.ps1`。安装版默认新增到 `%LOCALAPPDATA%/DesktopHarness/extensions/rag`；自定义数据目录可以调用脚本 `-DataDirectory <目录>`，打包里的脚本使用 `-Installed` 指向默认安装数据目录。

脚本完整复制后才发布，已有另一版插件代码则拒绝覆盖，保留旧目录；不修改其他插件开关、模型、预设或对话。安装程序也不覆盖已经存在的 RAG 扩展目录。源码在 `desktop-extensions/rag`，私有数据在实际数据根的 `plugin-data/rag`：`library.sqlite3` 保存资料与索引，`embedding.json` 保存独立接口。不要公开这两份私有数据。

导入、建立向量和检索在后台运行，线程退出才释放宿主任务登记。停止会关闭当前异步接口，文件／SQL扫描协作退出；未结束或未保存配置时沿宿主关闭检查提示。库开关下一次读取生效，正在执行的SQL采用自己的事务快照。

## 实现参考与边界

实际阅读 [Haystack InMemoryBM25Retriever.run](https://github.com/deepset-ai/haystack/blob/main/haystack/components/retrievers/in_memory/bm25_retriever.py) 的检索／存储职责分离及 [LlamaIndex IngestionPipeline._ahandle_upserts](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/ingestion/pipeline.py) 的 ID＋哈希变化处理。本插件自行实现小型 SQLite 索引，不引入上述框架，也没有移植其整套管线。

Embedding字段以[OpenAI接口](https://developers.openai.com/api/reference/resources/embeddings/methods/create)、[Ollama接口](https://docs.ollama.com/api/embed)、[Gemini接口](https://ai.google.dev/api/embeddings)为依据。复用本项目已核对的 Pi／Codex 插件贡献边界，不声称读过闭源 Desktop 源码。

本批由 Agent 静态审查和生产打包，未运行 GUI、导入语料、真实 API 或测试；检索效果、编码和取消行为由用户实测。
