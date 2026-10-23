"""Optional build hooks, with compatibility for existing single-file adapters."""


def prepare_build(engine, root, info, changed_files, font_plan=None):
    prepare = getattr(engine, "prepare_build", None)
    if font_plan is not None and (prepare is None or not hasattr(engine, "configure_font_plan")):
        raise ValueError("此引擎适配器尚不支持字体替换清单")
    if prepare is not None:
        return prepare(root, info, changed_files, font_plan=font_plan)
    check = getattr(engine, "check_build", None)
    return check(root, info, changed_files) if check is not None else None


def configure_fonts(engine, output, info, font, tmp_font, font_plan):
    if font_plan is not None:
        return engine.configure_font_plan(output, info, font_plan)
    return engine.configure_font(output, info, font, tmp_font)
