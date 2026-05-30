from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from app.services.analysis.base import BaseFileAnalyzer, FileAnalysis, FileInput
from app.services.llm.base import LLMProvider
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)

# Extensions whose content is meaningful as UTF-8 text passed to an LLM.
TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Source code
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".java",
        ".kt",
        ".scala",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".pl",
        ".pm",
        ".cs",
        ".fs",
        ".vb",
        ".cpp",
        ".c",
        ".h",
        ".hpp",
        ".cc",
        ".swift",
        ".m",
        ".ex",
        ".exs",
        ".clj",
        ".cljs",
        ".hs",
        ".ml",
        ".mli",
        ".lua",
        ".r",
        ".dart",
        ".elm",
        # Shell / scripts
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        # Web / markup
        ".html",
        ".htm",
        ".xml",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".vue",
        ".svelte",
        ".graphql",
        ".gql",
        # Data / config
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".env",
        ".csv",
        ".tsv",
        ".sql",
        ".proto",
        # Infrastructure
        ".tf",
        ".hcl",
        ".dockerfile",
        # Docs / text
        ".md",
        ".rst",
        ".txt",
        ".adoc",
        # Build / tooling
        ".mk",
        ".makefile",
        ".gradle",
        ".groovy",
        ".rake",
    }
)

_MAX_BATCH_CHARS = 60_000
_BINARY_DETECTION_CHUNK = 8_000

# ── LLM output schema ──────────────────────────────────────────────────────


class _FileAnalysisItem(BaseModel):
    path: str
    file_type: str
    language: str | None
    summary: str
    key_elements: list[str]
    dependencies: list[str]


class _BatchOutput(BaseModel):
    file_analyses: list[_FileAnalysisItem]


# ── Prompts ────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = prompt_loader.get("file_analyzer")


def _build_user_prompt(batch: list[tuple[str, str]]) -> str:
    from app.services.prompt_loader import prompt_loader

    prompt_loader.validate_builder_inputs(
        "file_analyzer",
        "main",
        {
            "files_batch": batch,
        },
    )
    parts = [f"Analyse the following {len(batch)} file(s):\n"]
    for path, content in batch:
        parts.append(f"[FILE]: {path}\n```\n{content}\n```\n")
    return "\n".join(parts)


# ── Analyzer ───────────────────────────────────────────────────────────────


class TextFileAnalyzer(BaseFileAnalyzer):
    """
    Reads UTF-8 text files and analyses them via an LLM.

    Files are grouped into batches capped at _MAX_BATCH_CHARS of combined
    content. A file that alone exceeds the cap occupies its own batch so
    nothing is silently dropped.  On LLM failure the affected files receive
    a fallback entry rather than raising.
    """

    supported_extensions = TEXT_EXTENSIONS

    def __init__(self, llm_provider: LLMProvider) -> None:
        self._llm = llm_provider

    def analyze_files(self, files: list[FileInput]) -> list[FileAnalysis]:
        batches = self._build_batches(files)
        results: list[FileAnalysis] = []
        for batch in batches:
            results.extend(self._call_llm(batch))
        return results

    # ── internal ────────────────────────────────────────────────────────────

    def _build_batches(self, files: list[FileInput]) -> list[list[tuple[str, str]]]:
        batches: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        current_chars = 0

        for fi in files:
            content = self._read(fi)
            chars = len(content)

            if current and current_chars + chars > _MAX_BATCH_CHARS:
                batches.append(current)
                current = []
                current_chars = 0

            current.append((fi.path, content))
            current_chars += chars

        if current:
            batches.append(current)

        return batches

    def _read(self, fi: FileInput) -> str:
        try:
            return fi.absolute_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "[could not read file]"

    def _call_llm(self, batch: list[tuple[str, str]]) -> list[FileAnalysis]:
        try:
            raw: dict[str, Any] = self._llm.generate_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=_build_user_prompt(batch),
                schema_name="batch_analysis_output",
                json_schema=_BatchOutput.model_json_schema(),
            )
            output = _BatchOutput.model_validate(raw)
        except Exception as exc:
            logger.error(
                "text_analyzer_batch_failed files=%s error=%s",
                [p for p, _ in batch],
                str(exc),
            )
            return [
                FileAnalysis(
                    path=path,
                    file_type="other",
                    language=None,
                    summary="Analysis failed for this file.",
                    key_elements=[],
                    dependencies=[],
                )
                for path, _ in batch
            ]

        return [
            FileAnalysis(
                path=item.path,
                file_type=item.file_type,
                language=item.language,
                summary=item.summary,
                key_elements=item.key_elements,
                dependencies=item.dependencies,
            )
            for item in output.file_analyses
        ]
