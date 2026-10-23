# 宿主能力适配

通用流程只依赖能够读取资料、生成结果并按需执行 Core 的宿主。先看当前会话的实际工具，不因产品名称假定所有功能都已开放；不主动改宿主配置或创建永久角色。

| 能力 | 可用时 | 不可用时 |
| --- | --- | --- |
| Core 命令执行 | 使用已确认的源码／后台执行前缀 | 只翻译已有包并交付结果，明确尚未提交；需要提取或构建时说明缺口 |
| 子 Agent | 依[协作说明](collaboration.md)分配独立任务 | 主 Agent 完成同样的翻译与审核 |
| MCP／资料检索 | 按实际工具声明取相关片段与来源 | 使用用户提供的本地文件；没有资料时不补设定 |
| 翻译阅读页 | 提交后展示真实已保存内容 | 报告 WORK、结果位置和 Core 状态，不影响翻译闭环 |

## Codex 与 Claude Code

把完整 game-translation 技能目录装入宿主支持的技能位置，保留 references；不需要仓库根目录兼容入口。正文只使用通用 Skill 元数据，具体安装方式和权限沿宿主当前设置。

使用宿主本轮实际开放的原生子 Agent／委托工具，按其参数、等待和取消语义操作；不调用 Desktop 专有的 subagent 工具名。不把 Claude Code 的 context: fork、角色名称或 Codex 的工具函数写成通用必需项，也不把整份 Skill 强制放进一个隔离子任务，主 Agent 仍负责统筹和提交。

子任务未必读取同一 Skill 或继承主聊天。提供[协作说明](collaboration.md)中的独立任务信息；可访问共享文件时给真实路径，不可访问时给必要包和资料。MCP 工具也不保证自动传给子任务，主 Agent 可以检索后转交相关原文。不要在 Skill 中另行发送模型 API 请求来模拟宿主子任务。

## 本项目 Desktop

Skill 是同一份工作流程，Desktop 额外提供读取工具、命令执行、可配置子角色、可选资料库与[翻译阅读页](desktop.md)。没有开启这些插件时按上表降级，不把缺插件当作 Core 已损坏。

其子任务接口为实际开放的 subagent／send_subagent、list_subagents、get_subagent_result、wait_subagents、cancel_subagent；使用环境中真实启用的 profile_id，不虚构默认角色。启动返回 child_id 后再按终态读取结果；get_subagent_result 有续读时不能只取第一段，wait_subagents 超时不取消任务。

资料库插件开放时，可用 rag_libraries 查看允许的库，rag_search 以 query 检索并按需指定 library_id、top_k、mode，rag_read 以真实 chunk_id 续读同版本原文。keyword 不需要 Embedding，semantic／hybrid 需要已配置并建立的索引；具体范围和预算以工具结果为准。这些名称只属于当前宿主映射，其他宿主使用自己的工具。
