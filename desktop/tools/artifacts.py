"""工具输出与文件修订的本地留存；不把完整大输出塞回模型。"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import ToolContext, ToolError


class ToolArtifacts:
    def __init__(self, context: ToolContext, tool: str, **details: Any) -> None:
        if context.artifacts_dir is None:
            raise ToolError("tool_artifacts_not_configured", "应用尚未配置工具记录目录，未执行文件修改。")
        self.directory = context.artifacts_dir / uuid4().hex
        self.directory.mkdir(parents=True)
        self.receipt = self.directory / "receipt.json"
        self._details = {
            "tool": tool, "call_id": context.call_id, "workspace": str(context.workspace),
            "started_at": datetime.now(timezone.utc).isoformat(), **details,
        }
        self._execution_details = context.execution_details
        self._audit_details = context.audit_details
        context.artifact_calls.add(context.call_id)
        context.artifact_ids.add(self.directory.name)
        self.update("prepared")

    def update(self, status: str, **details: Any) -> None:
        """先记录意图，完成后更新事实；原文件副本和完整输出另存，不覆盖。"""
        self._details.update(status=status, **details)
        if self._audit_details:
            self._details["execution_audit"] = dict(self._audit_details)
        if self._execution_details is not None:
            self._execution_details.update(self.references(), mutation_status=status)
        candidate = self.directory / f"receipt-{uuid4().hex}.pending"
        try:
            with candidate.open("x", encoding="utf-8", newline="\n") as target:
                json.dump(self._details, target, ensure_ascii=False, indent=2, allow_nan=False)
                target.flush()
                os.fsync(target.fileno())
            # 写满并刷盘后才更新稳定收据；失败的候选按保留规则留下。
            os.replace(candidate, self.receipt)
        except OSError:
            if self._execution_details is not None:
                self._execution_details["record_warning"] = "收据保存失败；请核对实际文件，不能直接重跑。"
            raise

    def references(self) -> dict[str, str]:
        return {"record_id": self.directory.name, "receipt_path": str(self.receipt), "artifacts_path": str(self.directory)}

    def finish_mutation(self, status: str, **details: Any) -> None:
        """副作用事实先进入执行上下文，记录失败也不能诱导模型把成功操作重跑。"""
        failure = sys.exception()
        try:
            self.update(status, **details)
        except OSError as exc:
            if failure is not None:
                failure.add_note("文件操作的终态收据保存失败，请先核对实际文件。")
                if self._execution_details is not None:
                    self._execution_details["record_warning"] = "终态收据保存失败；mutation_status来自执行事实。"
                return
            raise ToolError("mutation_record_error", "文件操作已结束，但收据保存失败；先核对实际文件，不要直接重跑。",
                            details={"mutation_status": status, **self.references()}) from exc
