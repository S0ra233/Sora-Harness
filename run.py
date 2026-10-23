"""Thin Skill CLI. The reusable package has no terminal or provider dependencies."""
import argparse
import json
from pathlib import Path
import sys

import game_translation as api


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scene 游戏翻译工作流")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("detect", help="检测引擎和字体能力")
    p.add_argument("game")
    p.add_argument("--godot-exe", help="Godot 实际启动 EXE，可用游戏内相对路径或绝对路径")
    p = commands.add_parser("inspect", help="收集资源数据特征与人工处理路线，不解包或修改游戏")
    p.add_argument("game")
    p.add_argument("--work", help="读取同一游戏已有的提取诊断或项目记录")
    p = commands.add_parser("candidates", help="预览 Unity/WOLF/Godot 文本候选，供主 Agent 按组选择")
    p.add_argument("game")
    p.add_argument("output", help="保存到新的候选 JSON 文件，不创建翻译项目")
    p.add_argument("--work", help="WOLF/Godot 候选所需的独立空目录；与后续 prepare 目录分开")
    p.add_argument("--godot-exe", help="Godot 实际启动 EXE；候选报告保存对应主包选择")
    p = commands.add_parser("locate", help="用一句已知文本搜索全部 Unity 候选，返回来源和候选组")
    p.add_argument("game")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=100)
    p = commands.add_parser("fonts", help="扫描 Unity 字体与资源引用，不修改游戏")
    p.add_argument("game")
    p.add_argument("output", help="新的字体清单报告路径")
    p.add_argument("--characters", default="", help="对照 TMP 字符表，不代表实机显示检查")
    p = commands.add_parser("prepare", help="检测、解包、提取并建立 Scene/Task/Entry")
    p.add_argument("game")
    p.add_argument("work")
    p.add_argument("--source-locale", help="多语言补全的源语言，须与目标语言同时指定")
    p.add_argument("--target-locale", help="已有目标语言，仅补全缺失或空白条目")
    p.add_argument("--text-locale", help="普通提取只选择该语言来源；译文写回原位置，不补全目标列")
    p.add_argument("--text-overrides", help="来源 ID 到语言名称的 JSON 确认文件，与 --text-locale 配合")
    p.add_argument("--adapter", help="执行工作目录内可信的临时 Python 适配文件，接入统一项目流程")
    p.add_argument("--selection", help="已填写 selected_groups 的候选 JSON 文件")
    p.add_argument("--godot-exe", help="Godot 实际启动 EXE；省略时沿用候选或已有项目记录")
    p = commands.add_parser("status", help="查看任务状态和 Scene ID")
    p.add_argument("work")
    p = commands.add_parser("package", help="导出下一 Scene，或指定任务重翻")
    p.add_argument("work")
    p.add_argument("--scene")
    p.add_argument("--target", action="append")
    p.add_argument("--context", choices=("scene", "neighbors"), default="scene")
    p.add_argument("--window", type=int, default=2)
    p.add_argument("--include-review", action="store_true")
    p.add_argument("--max-chars", type=int, default=60000)
    p = commands.add_parser("apply", help="校验并提交翻译结果 JSON 数组")
    p.add_argument("work")
    p.add_argument("package_id")
    p.add_argument("results")
    p = commands.add_parser("build", help="写回一个全新的游戏副本")
    p.add_argument("work")
    p.add_argument("output")
    p.add_argument("--allow-partial", action="store_true")
    fonts = p.add_mutually_exclusive_group()
    fonts.add_argument("--font")
    fonts.add_argument("--tmp-font")
    fonts.add_argument("--font-plan", help="明确选定目标与来源的 Unity 字体替换清单 JSON")
    p = commands.add_parser("verify", help="重新读取已提取位置，核对写回内容")
    p.add_argument("work")
    p.add_argument("output")
    args = parser.parse_args(argv)
    try:
        if args.command == "detect":
            result = api.detect(args.game, godot_executable=args.godot_exe)
        elif args.command == "inspect":
            result = api.inspect_resources(args.game, args.work)
        elif args.command == "locate":
            result = api.locate_text(args.game, args.query, limit=args.limit)
        elif args.command == "fonts":
            output = Path(args.output)
            if output.exists():
                raise FileExistsError("字体报告已存在，请使用新文件名")
            result = api.inspect_fonts(args.game, characters=args.characters)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=True, indent=2)
            result = {"report": str(output.resolve()), "fonts": len(result["fonts"]),
                      "text_components": len(result["text_components"]), "issues": len(result["issues"])}
        elif args.command == "candidates":
            output = Path(args.output)
            if output.exists():
                raise FileExistsError("候选文件已存在，请使用新文件名保留原记录")
            result = api.collect_candidates(args.game, work_dir=args.work, godot_executable=args.godot_exe)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
            result = {"report": str(output.resolve()), "groups": len(result["groups"]),
                      "candidates": result["candidate_count"], "warnings": len(result["warnings"])}
        elif args.command == "prepare":
            result = api.prepare(args.game, args.work, source_locale=args.source_locale,
                                 target_locale=args.target_locale, text_locale=args.text_locale,
                                 text_overrides=json.loads(Path(args.text_overrides).read_text(encoding="utf-8-sig"))
                                 if args.text_overrides else None, adapter_file=args.adapter,
                                 candidate_selection=json.loads(Path(args.selection).read_text(encoding="utf-8-sig"))
                                 if args.selection else None, godot_executable=args.godot_exe)
        elif args.command == "status":
            result = api.status(args.work)
        elif args.command == "package":
            result = api.make_package(args.work, args.scene, args.target, context=args.context,
                                      window=args.window, include_review=args.include_review,
                                      max_chars=args.max_chars)
        elif args.command == "apply":
            values = json.loads(Path(args.results).read_text(encoding="utf-8-sig"))
            result = api.apply_results(args.work, args.package_id, values)
        elif args.command == "build":
            result = api.build(args.work, args.output, allow_partial=args.allow_partial,
                               font=args.font, tmp_font=args.tmp_font,
                               font_plan=json.loads(Path(args.font_plan).read_text(encoding="utf-8-sig"))
                               if args.font_plan else None)
        else:
            result = api.verify(args.work, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command == "verify" and not result["indexed_values_match"] else 0
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        error = {"error": str(exc)}
        report = getattr(exc, "build_report", None)
        if report is not None:
            error.update(stage=report["stage"], retained_dir=report["retained_dir"],
                         report=getattr(exc, "build_report_path", None))
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
