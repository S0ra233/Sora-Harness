# 能力边界与代码入口

示例采用源码 CLI 写法；安装版的对应入口见[运行入口](runtime.md)。

保留原游戏；GAME、WORK和OUTPUT相互独立，使用用户授权的位置，不把当前宿主或源码工作区的位置当作固定数据目录。RGSS 输出使用解包资源并移除副本中的原主归档，不重新加密。Unity 私有资源/未识别脚本会记录警告，检测到引擎不代表支持全部游戏资源。文本读回通过也不证明游戏可启动、字体完整、界面不溢出；最终报告区分索引核验和实机检查。

`run.py` 只处理命令行。`game_translation/__init__.py` 导出 `detect / prepare / status / load_project / make_package / apply_results / build / verify / inspect_localization / inspect_resources / collect_candidates / locate_text / inspect_fonts`，供 CLI 与 Desktop 插件复用；函数返回字典或抛出异常，不打印、不退出进程。`resource_inspection.py` 收集资源证据与候选路线，`candidates.py` 按字段分组、定位文本并校验 Agent 选择，均不自行管理翻译状态。当前接口同步执行，同一 WORK 的修改需由调用方串行调度。

阅读顺序：`project.py`（Entry → Scene/Task）→ `translation.py`（包 → 校验 → 状态）→ `build.py`（任务 → 引擎写回 → 读回）→ `engines/`（各引擎协议）→ `formats/`（序列化）。任务 ID 基于引擎和资源定位；源文本哈希独立。数组/行号定位不能保证插入或移动文本后的身份稳定，不做跨位置自动搬译。

Ruby Marshal 解析代码保留 RPGMTL 来源，许可在 Core 资源目录 game_translation/formats/RPGMTL-LICENSE.txt。
