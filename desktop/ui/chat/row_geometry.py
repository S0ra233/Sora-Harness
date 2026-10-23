"""保留聊天原导入入口；行高算法由聊天和对照阅读共用。"""
from ..common.row_geometry import RowGeometry as _RowGeometry
from .viewport_rows import estimated_height


class RowGeometry(_RowGeometry):
    def __init__(self, spacing=28):
        super().__init__(spacing, estimate=estimated_height)
