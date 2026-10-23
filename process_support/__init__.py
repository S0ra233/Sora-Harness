"""桌面命令与引擎共用的轻量进程资源，不提前导入业务引擎。"""

class EngineCancelled(Exception):
    """用户停止引擎动作，已经生成的文件由业务层保留。"""
