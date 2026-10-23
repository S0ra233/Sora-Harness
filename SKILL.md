---
name: game-translation
description: 提取本地游戏文本为 Scene 翻译包，由宿主 Agent 翻译、审校，经 Core 校验后写回独立副本。适用于本地游戏汉化、已有项目续译、漏提取调查与单条重翻。
---

# Game Translation

完整技能入口是 [skills/game-translation/SKILL.md](skills/game-translation/SKILL.md)。开始实际任务前读取该正文，再按任务读取其中链接的参考；本文件保留仓库根目录的旧入口，不再重复维护引擎和流程说明。

如果由支持技能资料工具的宿主加载本入口，先读取相对路径 `skills/game-translation/SKILL.md`；后续资料路径加上 `skills/game-translation/` 前缀。正文中的 `SKILL_DIR` 指向这个实际技能目录，不能使用当前根入口代替。

源码继续使用本目录 `run.py`，例如 `python /实际源码目录/run.py status /实际工作目录`；所有路径可独立传入，不要求进入源码目录。Python 3.10+，可选引擎依赖按真实环境处理。复制技能到其他宿主时安装完整 `skills/game-translation/` 目录，Core路径可独立指定，不要求技能与源码相邻。统一入口见[运行入口](skills/game-translation/references/runtime.md)，宿主工具差异见[能力适配](skills/game-translation/references/hosts.md)。加载技能不扩大文件或命令权限。

主流程仍为 `detect → prepare → package → 宿主翻译 → apply → build/verify`。保留原游戏，WORK 与 OUTPUT 独立；只提交包中的目标 ID，保护占位符，不直接改任务状态。当前用户只要求查看、审校或部分重翻时，按该范围执行，不自行扩展为全流程。
