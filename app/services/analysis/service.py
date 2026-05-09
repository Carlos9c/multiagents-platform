from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.services.analysis.analyzers.default_analyzer import DefaultFileAnalyzer
from app.services.analysis.analyzers.text_analyzer import TextFileAnalyzer
from app.services.analysis.base import (
    ANALYSIS_FILENAME,
    CodebaseAnalysis,
    FileAnalysis,
    FileInput,
)
from app.services.analysis.registry import FileAnalyzerRegistry
from app.services.llm.base import LLMProvider
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema
from app.services.project_storage import ProjectStorageService

logger = logging.getLogger(__name__)

# ── Project summary schema ─────────────────────────────────────────────────


class _ProjectSummaryOutput(BaseModel):
    project_summary: str
    main_technologies: list[str]
    entry_points: list[str]


_SUMMARY_SYSTEM_PROMPT = """
You are a codebase analyst. Given per-file analyses of a software project, produce a project-level summary.

- project_summary: 3-6 sentences describing what the project is, what problem it solves, and how it is structured.
- main_technologies: primary languages, frameworks, and notable libraries (e.g. ["Python", "FastAPI", "SQLAlchemy"]).
- entry_points: main files that act as entry points (executable scripts, API mounts, package roots, etc.).

Base your answer on the analyses provided. Return ONLY valid JSON matching the schema.
""".strip()


def _build_summary_user_prompt(file_analyses: list[FileAnalysis], source_path: str) -> str:
    catalog = [
        {
            "path": fa.path,
            "file_type": fa.file_type,
            "language": fa.language,
            "summary": fa.summary,
        }
        for fa in file_analyses
    ]
    return (
        f"Source path: {source_path}\n\n"
        f"Per-file analyses ({len(catalog)} files):\n"
        f"{json.dumps(catalog, ensure_ascii=False, indent=2)}"
    )


# ── Service ────────────────────────────────────────────────────────────────


class CodebaseAnalysisService:
    """
    Thin orchestrator: lists files, routes each to the right analyzer via the
    registry, accumulates FileAnalysis results, then calls the LLM once more
    to produce the project-level summary.

    Adding support for a new file type = create a BaseFileAnalyzer subclass
    and register it here.  This service does not change.
    """

    def __init__(
        self,
        storage_service: ProjectStorageService | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self.storage_service = storage_service or ProjectStorageService()
        self._llm_provider = llm_provider

    @property
    def _llm(self) -> LLMProvider:
        if self._llm_provider is None:
            self._llm_provider = get_llm_provider()
        return self._llm_provider

    def _build_registry(self) -> FileAnalyzerRegistry:
        return FileAnalyzerRegistry(
            analyzers=[TextFileAnalyzer(llm_provider=self._llm)],
            fallback=DefaultFileAnalyzer(),
        )

    # ── public API ────────────────────────────────────────────────────────

    def analyze(self, project_id: int, source_path: str) -> CodebaseAnalysis:
        paths = self.storage_service.ensure_project_storage(project_id)
        source_dir = paths.source_dir

        logger.info(
            "codebase_analysis_started project_id=%s source_dir=%s",
            project_id,
            str(source_dir),
        )

        files = self._collect_files(source_dir)
        registry = self._build_registry()

        all_analyses: list[FileAnalysis] = []
        for analyzer, group in registry.group_by_analyzer(files):
            logger.info(
                "codebase_analysis_dispatching analyzer=%s files=%d",
                type(analyzer).__name__,
                len(group),
            )
            all_analyses.extend(analyzer.analyze_files(group))

        all_analyses.sort(key=lambda fa: fa.path)

        if all_analyses:
            project_summary, main_technologies, entry_points = self._generate_project_summary(
                all_analyses, source_path
            )
        else:
            project_summary = "The project source directory is empty."
            main_technologies = []
            entry_points = []

        analysis = CodebaseAnalysis(
            analyzed_at=datetime.now(timezone.utc).isoformat(),
            source_path=source_path,
            total_files=len(all_analyses),
            project_summary=project_summary,
            main_technologies=main_technologies,
            entry_points=entry_points,
            files=all_analyses,
        )

        self._save(paths.project_meta_dir, analysis)

        logger.info(
            "codebase_analysis_complete project_id=%s total_files=%d",
            project_id,
            analysis.total_files,
        )

        return analysis

    def get_analysis(self, project_id: int) -> CodebaseAnalysis | None:
        paths = self.storage_service.get_project_paths(project_id)
        analysis_path = paths.project_meta_dir / ANALYSIS_FILENAME
        if not analysis_path.exists():
            return None
        try:
            return self._load(analysis_path)
        except Exception:
            logger.exception("codebase_analysis_load_error project_id=%s", project_id)
            return None

    # ── internal ──────────────────────────────────────────────────────────

    def _collect_files(self, source_dir: Path) -> list[FileInput]:
        return [
            FileInput(
                path=item.relative_to(source_dir).as_posix(),
                absolute_path=item,
            )
            for item in sorted(source_dir.rglob("*"))
            if item.is_file()
        ]

    def _generate_project_summary(
        self,
        file_analyses: list[FileAnalysis],
        source_path: str,
    ) -> tuple[str, list[str], list[str]]:
        strict_schema = to_openai_strict_json_schema(_ProjectSummaryOutput.model_json_schema())
        try:
            raw: dict[str, Any] = self._llm.generate_structured(
                system_prompt=_SUMMARY_SYSTEM_PROMPT,
                user_prompt=_build_summary_user_prompt(file_analyses, source_path),
                schema_name="project_summary_output",
                json_schema=strict_schema,
            )
            out = _ProjectSummaryOutput.model_validate(raw)
            return out.project_summary, out.main_technologies, out.entry_points
        except Exception as exc:
            logger.error("codebase_analysis_summary_failed error=%s", str(exc))
            return "Project summary could not be generated.", [], []

    def _save(self, project_meta_dir: Path, analysis: CodebaseAnalysis) -> None:
        project_meta_dir.mkdir(parents=True, exist_ok=True)
        path = project_meta_dir / ANALYSIS_FILENAME
        path.write_text(
            json.dumps(asdict(analysis), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self, analysis_path: Path) -> CodebaseAnalysis:
        raw = json.loads(analysis_path.read_text(encoding="utf-8"))
        files = [FileAnalysis(**f) for f in raw.pop("files", [])]
        return CodebaseAnalysis(**raw, files=files)
