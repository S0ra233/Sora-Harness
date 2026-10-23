"""真实工具结果投影为查看入口；选择状态不写回模型历史。"""
from collections import OrderedDict
from dataclasses import dataclass, replace
from functools import lru_cache
import json
import logging
from pathlib import Path
import weakref

from shiboken6 import isValid

from ....ui.chat.projection import DisplayRow
from ....ui.contracts import UiContribution
from .content_page import TranslationContentPage
from .view_state import TranslationViewState


@dataclass(frozen=True)
class _Selection:
    call_id: str
    workspace: str
    work_dir: str
    resource_id: str


@lru_cache(maxsize=128)
def _reference(result):
    try:
        value = json.loads(result)
        data = value.get("data") if value.get("status") == "ok" else None
        if not isinstance(data, dict):
            return None
        view = data.get("view")
        if not isinstance(view, dict) or view.get("page_id") != "translation":
            return None
        workspace, relative, resource = data.get("workspace"), data.get("work_dir"), view.get("resource_id")
        if not all(isinstance(item, str) and item for item in (workspace, relative, resource)):
            return None
        if not Path(workspace).is_absolute() or not Path(resource).is_absolute():
            return None
        Path(resource).relative_to(Path(workspace))
        return workspace, relative, resource
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None


class TranslationProjection:
    """复用宿主投影与页面工厂，统一保存真实工具选择和用户的阅读位置。"""

    def __init__(self, store):
        self._store = store
        self._selections = OrderedDict()
        self._seen_calls = {}
        self._view = None
        self._save_error = None

    def _page(self):
        page = self._view() if self._view is not None else None
        return page if page is not None and isValid(page) else None

    def create(self, context):
        view = TranslationContentPage(context, self._remember)
        self._view = weakref.ref(view)
        return UiContribution("translation-content", view, view.close_block,
                              bind_context=self.bind_context, invalidate=view.invalidate)

    def bind_context(self, context):
        view = self._page()
        if view is None:
            return
        view.bind_context(context)
        if context.resource_id is None:
            saved = self._store.get(context.conversation_id)
            selection = self._selections.get(context.conversation_id)
            if saved is not None:
                view.restore_selection(saved.root, saved.scene_id)
            elif selection is not None:
                view.bind_selection(context.conversation_id, selection.workspace, selection.work_dir)
        if self._store.error or self._save_error:
            view.show_notice(self._store.error or self._save_error)

    def _remember(self, conversation_id, root, workspace, scene_id):
        """页面只在Core读取成功后回调；工具选择则已经通过工具层的Core校验。"""
        if conversation_id is None:
            return
        previous = self._store.get(conversation_id)
        calls = set(previous.seen_calls) if previous is not None else set()
        calls.update(self._seen_calls.get(conversation_id, ()))
        state = TranslationViewState(root, workspace, scene_id, tuple(sorted(calls)))
        try:
            self._store.save(conversation_id, state)
            self._save_error = None
        except (OSError, ValueError) as exc:
            self._save_error = f"翻译阅读位置未保存，当前内容仍可查看：{exc}"
            logging.getLogger(__name__).warning("翻译阅读位置保存失败：对话=%s 原因=%s", conversation_id, exc)
            view = self._page()
            if view is not None and view.conversation_id == conversation_id:
                view.show_notice(self._save_error)

    def decorate(self, rows, context):
        found = []

        def project(items):
            output = []
            for row in items:
                if row.key.startswith("translation-reference:"):
                    continue
                if row.process:
                    row = replace(row, process=replace(row.process, rows=project(row.process.rows)))
                output.append(row)
                for tool in row.tools:
                    if tool.name != "show_translation" or tool.status != "succeeded" or not tool.result:
                        continue
                    reference = _reference(tool.result)
                    if reference is None:
                        continue
                    selection = _Selection(tool.call_id, *reference)
                    found.append(selection)
                    output.append(DisplayRow(f"translation-reference:{tool.call_id}", "", "查看翻译",
                                             page_reference=("translation", selection.resource_id),
                                             regeneratable=False, rewindable=False))
            return tuple(output)

        result = project(rows)
        identifier = context.conversation_id
        if identifier is not None:
            known = identifier in self._selections
            previous = self._selections.pop(identifier, None)
            saved = self._store.get(identifier)
            if identifier not in self._seen_calls:
                # 重启后仍识别已处理的旧调用，避免历史重绘覆盖后来手选的项目。
                self._seen_calls[identifier] = set(saved.seen_calls) if saved is not None else set()
            seen = self._seen_calls[identifier]
            fresh = [item for item in found if item.call_id not in seen]
            seen.update(item.call_id for item in found)
            # 宿主分别投影历史和本轮；空本轮不能清除历史选择，旧结果重绘也不能反复切换。
            selected = fresh[-1] if fresh else previous
            self._selections[identifier] = selected
            if fresh:
                scene_id = saved.scene_id if saved is not None and saved.root == selected.resource_id else None
                self._remember(identifier, selected.resource_id, selected.workspace, scene_id)
            while len(self._selections) > 128:
                expired, _ = self._selections.popitem(last=False)
                self._seen_calls.pop(expired, None)
            view = self._page()
            # 重绘同一结果不覆盖手动选择；初次打开另一份旧聊天也不抢走独立窗口。
            if selected is not None and selected != previous and view is not None and (
                    known or view.conversation_id in {None, identifier}):
                view.bind_selection(identifier, selected.workspace, selected.work_dir)
        return result
