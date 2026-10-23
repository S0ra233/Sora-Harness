"""Explicit adapter results; reports and dependencies are not text Entries."""
from dataclasses import dataclass, field


@dataclass
class ExtractionResult:
    """Text occurrences and extraction metadata, before Core creates Tasks.

    Dependencies are paths relative to the prepared resource root, e.g. type
    assemblies or WOLF schemas used while interpreting the text resources.
    """

    entries: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reports: list[dict] = field(default_factory=list)
    dependencies: set[str] = field(default_factory=set)

    def source_files(self) -> set[str]:
        return {entry["file"] for entry in self.entries} | self.dependencies

    def __iter__(self):
        # Existing callers may still unpack entries, warnings. Core uses the
        # named fields so metadata never relies on this compatibility shortcut.
        yield self.entries
        yield self.warnings


def extract_resources(engine, info, resources):
    """Normalize old single-file adapters at the Core boundary only."""
    result = engine.extract(info, resources)
    if isinstance(result, ExtractionResult):
        return result
    entries, warnings = result
    dependencies = set(resources.get("source_files", []))
    dependencies.update(name for entry in entries for name in entry.get("type_dependencies", []))
    return ExtractionResult(entries, warnings, resources.get("extraction_report", []), dependencies)


def representative_entries(entries):
    """A small preview spread through the group, not a coverage claim."""
    if not entries:
        return []
    return [entries[i] for i in sorted({0, len(entries) // 2, len(entries) - 1})]


def summarize_source_context(entries):
    """Keep factual source hints brief; the selected text lives in Tasks."""
    context = dict(entries[0]["source_context"])
    examples = []
    for entry in representative_entries(entries):
        field = (entry.get("structure", {}).get("language_column")
                 or entry.get("structure", {}).get("field")
                 or entry["path"].rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~"))
        if entry["format"] in {"csv", "tsv"} and not entry.get("structure", {}).get("language_column"):
            field = "column_" + entry["path"].rsplit("/", 1)[-1]
        fields = {key: value for key, value in entry.get("candidate_context", {}).items()
                  if key != field}
        example = {"path": entry["path"], "fields": fields}
        row_fields = entry.get("row_fields")
        if row_fields:
            example["row_fields"] = row_fields
        if fields or row_fields:
            examples.append(example)
    if examples:
        context["record_examples"] = examples
    return context
