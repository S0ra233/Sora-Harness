"""不加载Qt的插件接口；具体业务只依赖这里的稳定约定。"""
from dataclasses import dataclass
from pathlib import Path

from ..llm.messages import ToolSchema
from ..runtime.contracts import ExecutionContext, Plugin
from ..tools.contracts import ToolContext, ToolDefinition, ToolDisplay, ToolError
from ..tools.artifacts import ToolArtifacts
from ..tools.filesystem.paths import resolve_new_file_path, resolve_workspace_path
from ..application.document_conversion import convert_document, document_readers


@dataclass(frozen=True)
class PluginHost:
    """宿主提供代码与私有数据目录；创建目录和数据格式由插件决定。"""

    plugin_id: str
    code_directory: Path
    data_directory: Path


__all__ = ["PluginHost", "Plugin", "ExecutionContext", "ToolSchema", "ToolContext",
           "ToolDefinition", "ToolDisplay", "ToolError", "ToolArtifacts",
           "resolve_workspace_path", "resolve_new_file_path", "convert_document", "document_readers"]
