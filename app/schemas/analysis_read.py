from __future__ import annotations

from pydantic import BaseModel


class FileAnalysisRead(BaseModel):
    path: str
    file_type: str
    language: str | None = None
    summary: str
    key_elements: list[str]
    dependencies: list[str]


class CodebaseAnalysisRead(BaseModel):
    analyzed_at: str
    source_path: str
    total_files: int
    project_summary: str
    main_technologies: list[str]
    entry_points: list[str]
    files: list[FileAnalysisRead]
