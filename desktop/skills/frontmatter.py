"""标准YAML元数据边界；只读取说明和调用开关，不执行私有扩展字段。"""
import re


class SkillDependencyError(ValueError):
    """依赖缺失是目录级问题，不在每个技能上重复产生同一条诊断。"""


def parse_metadata(header: str, default_name: str):
    try:
        import yaml
    except ImportError as exc:
        raise SkillDependencyError("缺少PyYAML，请安装已声明的desktop依赖后重新读取技能目录。") from exc
    try:
        value = yaml.safe_load(header)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"技能YAML元数据格式错误：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("技能元数据须为键值对象。")
    name, description = value.get("name", default_name), value.get("description")
    if (not isinstance(name, str) or not 1 <= len(name) <= 64
            or re.fullmatch(r"[^\s/@]+", name) is None):
        raise ValueError("技能名称须为1到64字符，不能包含空白、/或@。")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("技能需要非空description。")
    description = " ".join(description.split())
    if len(description) > 1024:
        raise ValueError("技能description超过1024字符，请把详细说明移到正文。")
    user = value.get("user-invocable", True)
    disabled_model = value.get("disable-model-invocation", False)
    if type(user) is not bool or type(disabled_model) is not bool:
        raise ValueError("user-invocable及disable-model-invocation须为布尔值。")
    return {"name": name, "description": description, "user_invocable": user,
            "model_invocable": not disabled_model}
