# Tyrano / Electron ASAR

标准 `resources/app.asar` 中存在 `package.json` 和 `data/scenario/*.ks` 时，内置 Tyrano 路线读取 ASAR 索引。普通 `data/scenario` 与 Electron 松散的 `resources/app/data/scenario` 仍可直接处理。后缀只是定位线索；检测会实际解析归档索引和文件边界。

Python 实现仅使用标准库，不安装 Node.js 或第三方 ASAR 工具，不执行归档里的 JavaScript。

## 数据流

```text
原 app.asar / app.asar.unpacked（只读）
  → WORK/_tyrano/prepare-*/resources/resources/app/data/scenario/*.ks
  → 复用 Tyrano 剧本提取与 Scene 分组
  → 原 package / apply 流程
  → build 复制完整游戏
  → 修改输出 app.asar 中选中的剧本并重算偏移
  → 从输出归档读回每个已索引字段
```

工作目录只展开剧情脚本供索引与来源校验，不解出整份音画资源。Entry 的 `file` 是工作副本中的相对路径；`container` 保存原归档和内部脚本位置。Core 的公共参数与 Entry/Task/Scene 规则不变，Tyrano 的 `read_many` / `write_many` 负责这层定位。

使用已有 `prepare GAME WORK`、`package`、`apply`、`build WORK OUTPUT` 即可。输出保留 ASAR 布局，不生成用于覆盖加载的 `resources/app` 目录，也不修改原游戏。

归档写回按文件流复制未修改内容，保留链接、外置资源标记及可执行标记。选中外置脚本时同时修改输出的 `.unpacked` 文件；原有 SHA-256 文件校验记录会更新。失败时保留 Core 暂存目录及临时归档。

## 加载边界

- 构建前只读扫描顶层 EXE 的标准 Electron v1 fuse wire。检测到 `EnableEmbeddedAsarIntegrityValidation` 启用或无法解释的开关时，停止自动构建，报告 `needs_agent_loader_support`；不改 EXE、不关闭校验。未发现开关不能证明不存在游戏自定义校验。
- `OnlyLoadAppFromAsar` 可以保留，因为输出仍为 ASAR。`app` 与 `app.asar` 同时存在时先由 Agent 确认实际加载来源，避免修改未使用的副本。
- 私有、加密 ASAR 和混合引擎容器需 Agent 分析。标准容器展开后，文本仍使用已有适配器或临时适配，统一走翻译流程。
- 当前 `.ks` 的支持范围不变：动态脚本、命令简写、游戏专用语法和字体 CSS 需另外处理。
- 本次没有运行测试或游戏。归档读回、真实译文写回和启动结果等待其他模型验证；不能把代码接入视为实机可用。

## 格式与来源

索引、文件偏移和完整性字段的实现依据 [electron/asar disk.ts](https://github.com/electron/asar/blob/main/src/disk.ts) 与 [integrity.ts](https://github.com/electron/asar/blob/main/src/integrity.ts)；这些是格式参考，不是新增依赖，未复制或分发上游程序。

加载设置依据 [Electron fuses](https://github.com/electron/electron/blob/main/docs/tutorial/fuses.md)、[ASAR integrity](https://github.com/electron/electron/blob/main/docs/tutorial/asar-integrity.md) 和 [fuse 索引定义](https://github.com/electron/fuses/blob/main/src/config.ts)。完整性校验涉及 EXE 的情形交给 Agent 个案处理。
