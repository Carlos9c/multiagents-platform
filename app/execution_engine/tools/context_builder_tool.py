from __future__ import annotations

from pathlib import Path

from app.execution_engine.context_selection import ContextSelectionResult
from app.execution_engine.tools.file_reader_tool import read_text_file


def build_selected_file_context(
    *,
    workspace_root: str,
    selection: ContextSelectionResult,
    max_chars_per_file: int = 20000,
) -> str:
    root = Path(workspace_root)
    blocks: list[str] = []

    for file_selection in selection.files:
        abs_path = root / file_selection.path
        blocks.append(f"FILE: {file_selection.path}")
        blocks.append(f"REASON: {file_selection.reason}")
        blocks.append(f"RELEVANCE: {file_selection.relevance}")

        if file_selection.symbol_hints:
            blocks.append(
                "SYMBOL_HINTS: " + ", ".join(file_selection.symbol_hints)
            )

        if not abs_path.exists() or not abs_path.is_file():
            blocks.append("CONTENT: [missing file]")
            blocks.append("")
            continue

        try:
            content = read_text_file(str(abs_path))
        except Exception as exc:
            blocks.append(f"CONTENT_ERROR: {str(exc)}")
            blocks.append("")
            continue

        if not file_selection.include_full_content and len(content) > max_chars_per_file:
            content = content[:max_chars_per_file] + "\n...[truncated]"

        blocks.append("CONTENT_START")
        blocks.append(content)
        blocks.append("CONTENT_END")
        blocks.append("")

    return "\n".join(blocks)