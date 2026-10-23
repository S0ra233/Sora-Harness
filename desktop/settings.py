"""旧模型设置入口；职责已由配置存储与应用服务承担。"""
from .application.models import (
    load_model_catalog, load_settings, save_settings, select_model,
    update_model_catalog, write_model_catalog,
)
