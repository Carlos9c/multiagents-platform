from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

ANALYSIS_FILENAME = "analysis.json"


@dataclass
class FileInput:
    path: str  # Relative POSIX path inside source_dir
    absolute_path: Path


@dataclass
class FileAnalysis:
    path: str
    file_type: str  # "code" | "config" | "test" | "docs" | "data" | "asset" | "other"
    language: str | None
    summary: str
    key_elements: list[str]
    dependencies: list[str]


@dataclass
class CodebaseAnalysis:
    analyzed_at: str
    source_path: str
    total_files: int
    project_summary: str
    main_technologies: list[str]
    entry_points: list[str]
    files: list[FileAnalysis] = field(default_factory=list)


class BaseFileAnalyzer(ABC):
    """
    Contract for file-type-specific analyzers.

    Each subclass declares which extensions it handles via `supported_extensions`
    and implements `analyze_files` to produce one `FileAnalysis` per input file.
    The reading strategy, LLM interaction (if any), and batching are all internal
    to the subclass — the service sees only `FileAnalysis` objects.
    """

    supported_extensions: frozenset[str] = frozenset()

    @abstractmethod
    def analyze_files(self, files: list[FileInput]) -> list[FileAnalysis]:
        """Return one FileAnalysis per FileInput, in the same order."""
