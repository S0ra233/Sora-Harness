# 单条重翻与字体

示例采用源码 CLI 写法；安装版的对应入口见[运行入口](runtime.md)。

本页的 `tools/` 路径均相对实际 Core 资源目录，按运行入口定位后用宿主允许的文件工具读取；它们不在独立安装的技能目录内。

- `python run.py package WORK --target TASK_ID`：目标只有一条，保留完整 Scene 上下文。
- `python run.py package WORK --target TASK_ID --context neighbors --window 2`：动态生成可选 `context_before/context_after`，不把它们重复存进 Task。用于长场景或局部重翻；需要整段语义时优先完整 Scene。
- 每包默认 60000 字符上限，超限报错而不截断；可选择单条邻近模式或明确调整 `--max-chars`。
- MV/MZ：`build WORK OUTPUT --font PATH`，支持 TTF/OTF/WOFF/WOFF2 文件。Unity：`--tmp-font PATH`，需要兼容的 TMP AssetBundle 和游戏中已有的 XUnity.AutoTranslator。其他情况保持字体不变并另做引擎适配；不自动下载或安装字体/插件。
- `python run.py verify WORK OUTPUT` 可在人工修改输出后重新核对。build 已做相同的文本读回检查，成功后无需立即重复运行 verify。
- Unity 来源调查可用 `python run.py locate GAME "已知对白"`：搜索全部已解析候选，返回完整文本、文件／对象／字段或方法、候选组 ID。未命中时看诊断并调查其他来源，不据此宣称不存在对白；不会自动选组或创建任务。
- Unity 字体用 `python run.py fonts GAME REPORT.json --characters "需要的字符"` 扫描引用，再按`tools/unity_fonts.md` 的字体清单说明制作显式替换计划，使用 `build WORK OUTPUT --font-plan PLAN.json`。不要凭名称盲换所有字体；TMP 需兼容的字体资产／材质／图集及引用映射。字符表检查不能证明回退字体、动态字库或实际显示。
- Unity build 在复制前检查文本和字体将修改的 Bundle。可选 Addressables 辅助程序（`tools/unity_addressables/README.md`）支持 JSON／二进制 v2 的本地标准 Provider 直读路线：验证原 CRC 和目录往返，写回后更新相关条目的 CRC/大小以及 catalog 缓存标识。不修改未关联目录，不关闭原有 CRC；远程／WebRequest 缓存／自定义 Provider 或不明确关系仍返回 `needs_catalog_support`。`load_checks` 列出适配范围；不能将读回成功当作游戏可运行。
- 构建中途失败时，保留输出目录旁的 `.game-translation-build-*` 目录，以及未完成替换的临时文件。CLI 错误和 WORK/build_report.json 提供 `stage`、`retained_dir`；检查这些文件后重试会创建新的目录，不覆盖旧失败产物。未经用户确认不删除或归档。复制前被阻止时 `retained_dir=null`，因为尚未创建副本。成功时暂存目录直接成为 OUTPUT。
