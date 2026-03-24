from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.task import (
    PLANNING_LEVEL_HIGH_LEVEL,
    PLANNING_LEVEL_REFINED,
    Task,
)
from app.schemas.code_context_selection import (
    CandidatePathSignal,
    CodeContextSelectionConstraints,
    CodeContextSelectionInput,
    CodeContextSelectionResult,
    CodeContextSourcesUsed,
    CodeContextTaskHierarchy,
    CodeContextTaskPayload,
    RelatedTaskContext,
    RepositoryFileDescriptor,
    RepositoryIndexSnapshot,
    SelectedCodeFile,
)
from app.services.artifacts import create_artifact
from app.services.code_context_selector_client import (
    CodeContextSelectorClientError,
    select_code_context_with_model,
)
from app.services.project_memory_service import build_project_operational_context
from app.services.workspace_runtime import PreparedWorkspace, WorkspaceRuntimeError


CODE_CONTEXT_SELECTION_ARTIFACT_TYPE = "code_context_selection"

_PATH_PATTERN = re.compile(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9_]+")
_WORD_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

_TOKEN_BLACKLIST = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "only",
    "must",
    "should",
    "task",
    "tasks",
    "code",
    "file",
    "files",
    "project",
    "system",
    "workflow",
    "execution",
    "agent",
    "agents",
}


class CodeContextSelectorError(Exception):
    """Base error for code context selection."""


class CodeContextSelectorInternalError(CodeContextSelectorError):
    """Raised when context selection fails internally."""


class CodeContextSelector:
    def __init__(self, db: Session, workspace_runtime):
        self.db = db
        self.workspace_runtime = workspace_runtime

    def select_context(
        self,
        task: Task,
        prepared_workspace: PreparedWorkspace,
        execution_run_id: int | None = None,
    ) -> CodeContextSelectionResult:
        selection_input = self.build_selection_input(
            task=task,
            prepared_workspace=prepared_workspace,
            execution_run_id=execution_run_id,
        )

        try:
            raw_selection = select_code_context_with_model(selection_input)
        except CodeContextSelectorClientError as exc:
            raise CodeContextSelectorInternalError(
                f"Failed to select code context with model: {str(exc)}"
            ) from exc

        selection = self._normalize_selection(
            selection=raw_selection,
            selection_input=selection_input,
        )

        self._persist_selection_artifact(
            task=task,
            selection=selection,
        )

        return selection

    def build_selection_input(
        self,
        task: Task,
        prepared_workspace: PreparedWorkspace,
        execution_run_id: int | None = None,
    ) -> CodeContextSelectionInput:
        constraints = CodeContextSelectionConstraints()
        project_operational_context = build_project_operational_context(
            db=self.db,
            project_id=task.project_id,
        )
        hierarchy = self._build_task_hierarchy(task)
        repository_index = self._build_repository_index(prepared_workspace)
        related_tasks = self._build_related_task_contexts(
            task=task,
            project_operational_context=project_operational_context,
            constraints=constraints,
        )
        candidate_paths = self._build_candidate_paths(
            task=task,
            hierarchy=hierarchy,
            project_operational_context=project_operational_context,
            related_tasks=related_tasks,
            repository_index=repository_index,
            constraints=constraints,
        )
        context_gaps = self._derive_context_gaps(
            task=task,
            project_operational_context=project_operational_context,
            related_tasks=related_tasks,
            candidate_paths=candidate_paths,
            repository_index=repository_index,
        )

        return CodeContextSelectionInput(
            project_id=task.project_id,
            task_id=task.id,
            execution_run_id=execution_run_id,
            task=self._build_task_payload(task),
            hierarchy=hierarchy,
            project_operational_context=project_operational_context,
            related_tasks=related_tasks,
            repository_index=repository_index,
            candidate_paths=candidate_paths,
            context_gaps=context_gaps,
            constraints=constraints,
        )

    def _persist_selection_artifact(
        self,
        task: Task,
        selection: CodeContextSelectionResult,
    ) -> int:
        artifact = create_artifact(
            db=self.db,
            project_id=task.project_id,
            task_id=task.id,
            artifact_type=CODE_CONTEXT_SELECTION_ARTIFACT_TYPE,
            content=selection.model_dump_json(indent=2),
            created_by="code_context_selector",
        )
        return artifact.id

    @staticmethod
    def _build_task_payload(task: Task) -> CodeContextTaskPayload:
        return CodeContextTaskPayload(
            task_id=task.id,
            title=task.title,
            description=task.description,
            summary=task.summary,
            objective=task.objective,
            proposed_solution=task.proposed_solution,
            implementation_notes=task.implementation_notes,
            implementation_steps=task.implementation_steps,
            acceptance_criteria=task.acceptance_criteria,
            tests_required=task.tests_required,
            technical_constraints=task.technical_constraints,
            out_of_scope=task.out_of_scope,
            task_type=task.task_type,
            priority=task.priority,
            planning_level=task.planning_level,
            executor_type=task.executor_type,
            status=task.status,
        )

    def _build_task_hierarchy(self, task: Task) -> CodeContextTaskHierarchy:
        refined_parent: Task | None = None
        high_level_parent: Task | None = None

        current = task.parent_task
        while current is not None:
            if current.planning_level == PLANNING_LEVEL_REFINED and refined_parent is None:
                refined_parent = current
            if current.planning_level == PLANNING_LEVEL_HIGH_LEVEL and high_level_parent is None:
                high_level_parent = current
            current = current.parent_task

        return CodeContextTaskHierarchy(
            atomic_task_id=task.id,
            atomic_title=task.title,
            parent_refined_task_id=refined_parent.id if refined_parent else None,
            parent_refined_title=refined_parent.title if refined_parent else None,
            parent_refined_summary=refined_parent.summary if refined_parent else None,
            parent_refined_objective=refined_parent.objective if refined_parent else None,
            parent_high_level_task_id=high_level_parent.id if high_level_parent else None,
            parent_high_level_title=high_level_parent.title if high_level_parent else None,
            parent_high_level_summary=high_level_parent.summary if high_level_parent else None,
            parent_high_level_objective=high_level_parent.objective if high_level_parent else None,
        )

    def _build_repository_index(
        self,
        prepared_workspace: PreparedWorkspace,
    ) -> RepositoryIndexSnapshot:
        workspace_dir = prepared_workspace.workspace_dir
        paths = self.workspace_runtime.list_files(workspace_dir)

        files: list[RepositoryFileDescriptor] = []
        for path in paths:
            content = self._safe_read_file(workspace_dir, path)
            files.append(
                RepositoryFileDescriptor(
                    path=path,
                    module_hint=Path(path).stem or None,
                    symbols=self._extract_symbols(content),
                    summary=self._build_file_summary(path=path, content=content),
                )
            )

        return RepositoryIndexSnapshot(
            repo_root=str(workspace_dir),
            total_files=len(files),
            files=files,
        )

    def _build_related_task_contexts(
        self,
        task: Task,
        project_operational_context,
        constraints: CodeContextSelectionConstraints,
    ) -> list[RelatedTaskContext]:
        tasks_by_id = {
            item.task_id: item for item in project_operational_context.task_memory
        }
        related_scores: dict[int, tuple[float, str]] = {}
        current_tokens = self._task_tokens(task)
        current_paths = set(self._extract_task_paths(task))

        ancestor_ids = self._ancestor_ids(task)
        descendant_ids = self._descendant_ids(task)

        for task_summary in project_operational_context.task_memory:
            if task_summary.task_id == task.id:
                continue

            score = 0.0
            reasons: list[str] = []

            if task_summary.task_id in ancestor_ids or task_summary.task_id in descendant_ids:
                score += 4.0
                reasons.append("same planning branch")

            if task_summary.parent_task_id == task.parent_task_id and task.parent_task_id is not None:
                score += 2.5
                reasons.append("same parent task")

            related_tokens = self._text_tokens(
                " ".join(
                    [
                        task_summary.title or "",
                        task_summary.objective or "",
                        task_summary.work_summary or "",
                        task_summary.completed_scope or "",
                        task_summary.remaining_scope or "",
                        task_summary.validation_notes or "",
                    ]
                )
            )
            overlap = current_tokens.intersection(related_tokens)
            if overlap:
                score += min(3.0, len(overlap) * 0.6)
                reasons.append("shared task language")

            task_paths = self._paths_for_task_memory(task_summary.task_id, project_operational_context)
            if current_paths and task_paths and current_paths.intersection(task_paths):
                score += 3.0
                reasons.append("shared referenced paths")

            if task_summary.last_run_status in {"succeeded", "partial"}:
                score += 0.8
                reasons.append("has recent execution evidence")

            if score > 0:
                related_scores[task_summary.task_id] = (score, ", ".join(reasons))

        shortlisted_ids = [
            task_id
            for task_id, _score_reason in sorted(
                related_scores.items(),
                key=lambda item: item[1][0],
                reverse=True,
            )[: constraints.max_related_tasks]
        ]

        related_contexts: list[RelatedTaskContext] = []
        for task_id in shortlisted_ids:
            task_summary = tasks_by_id.get(task_id)
            if not task_summary:
                continue

            last_run = None
            if task_summary.last_run_id:
                last_run = self.db.get(ExecutionRun, task_summary.last_run_id)

            artifacts = (
                self.db.query(Artifact)
                .filter(Artifact.project_id == task.project_id, Artifact.task_id == task_id)
                .order_by(Artifact.id.desc())
                .limit(6)
                .all()
            )

            artifact_summaries = [
                self._artifact_summary(artifact)
                for artifact in artifacts
            ]

            referenced_paths = list(
                dict.fromkeys(
                    self._extract_paths_from_text(" ".join(artifact_summaries))
                    + self._extract_paths_from_text(last_run.work_details if last_run else None)
                    + self._extract_paths_from_text(last_run.output_snapshot if last_run else None)
                    + self._extract_paths_from_text(task_summary.work_summary)
                    + self._extract_paths_from_text(task_summary.completed_scope)
                    + self._extract_paths_from_text(task_summary.remaining_scope)
                )
            )[:20]

            related_contexts.append(
                RelatedTaskContext(
                    task_id=task_summary.task_id,
                    title=task_summary.title,
                    relationship_reason=related_scores[task_id][1],
                    task_status=task_summary.status,
                    planning_level=task_summary.planning_level,
                    last_run_status=task_summary.last_run_status,
                    work_summary=task_summary.work_summary,
                    completed_scope=task_summary.completed_scope,
                    remaining_scope=task_summary.remaining_scope,
                    blockers_found=task_summary.blockers_found,
                    validation_notes=task_summary.validation_notes,
                    artifact_summaries=artifact_summaries[:6],
                    referenced_paths=referenced_paths,
                )
            )

        return related_contexts

    def _build_candidate_paths(
        self,
        task: Task,
        hierarchy: CodeContextTaskHierarchy,
        project_operational_context,
        related_tasks: list[RelatedTaskContext],
        repository_index: RepositoryIndexSnapshot,
        constraints: CodeContextSelectionConstraints,
    ) -> list[CandidatePathSignal]:
        pool: dict[str, CandidatePathSignal] = {}
        existing_repo_paths = {item.path for item in repository_index.files}

        def add(path: str, score: float, reason: str, source_type: str) -> None:
            normalized_path = self._normalize_repo_path(path)
            if not normalized_path or normalized_path not in existing_repo_paths:
                return

            if normalized_path not in pool:
                pool[normalized_path] = CandidatePathSignal(path=normalized_path, score=0.0)
            pool[normalized_path].score += score
            if reason not in pool[normalized_path].reasons:
                pool[normalized_path].reasons.append(reason)
            if source_type not in pool[normalized_path].source_types:
                pool[normalized_path].source_types.append(source_type)

        explicit_task_paths = self._extract_task_paths(task)
        for path in explicit_task_paths:
            add(path, 5.0, "explicitly mentioned in current task", "task")

        hierarchy_text = " ".join(
            [
                hierarchy.atomic_title or "",
                hierarchy.parent_refined_title or "",
                hierarchy.parent_refined_summary or "",
                hierarchy.parent_refined_objective or "",
                hierarchy.parent_high_level_title or "",
                hierarchy.parent_high_level_summary or "",
                hierarchy.parent_high_level_objective or "",
            ]
        )
        for path in self._extract_paths_from_text(hierarchy_text):
            add(path, 4.0, "mentioned in task hierarchy", "hierarchy")

        current_tokens = self._task_tokens(task)
        for path_signal in project_operational_context.referenced_paths:
            if path_signal.path not in existing_repo_paths:
                continue
            path_tokens = self._text_tokens(path_signal.path.replace("/", " ").replace(".", " "))
            overlap = current_tokens.intersection(path_tokens)
            if overlap:
                add(
                    path_signal.path,
                    min(4.0, 1.0 + 0.5 * path_signal.mention_count + 0.4 * len(overlap)),
                    "referenced in cumulative project memory with token overlap",
                    "project_memory",
                )

        for related_task in related_tasks:
            for path in related_task.referenced_paths:
                if path not in existing_repo_paths:
                    continue
                add(path, 3.0, f"referenced by related task {related_task.task_id}", "related_task")

        for descriptor in repository_index.files:
            path_tokens = self._text_tokens(descriptor.path.replace("/", " ").replace(".", " "))
            symbol_tokens = {item.lower() for item in descriptor.symbols}
            overlap = current_tokens.intersection(path_tokens.union(symbol_tokens))
            if overlap:
                add(
                    descriptor.path,
                    min(3.0, 0.8 + 0.5 * len(overlap)),
                    "repository path or symbols overlap with current task language",
                    "repository",
                )

        ranked = sorted(pool.values(), key=lambda item: item.score, reverse=True)
        return ranked[: constraints.max_candidate_paths]

    def _derive_context_gaps(
        self,
        task: Task,
        project_operational_context,
        related_tasks: list[RelatedTaskContext],
        candidate_paths: list[CandidatePathSignal],
        repository_index: RepositoryIndexSnapshot,
    ) -> list[str]:
        gaps: list[str] = []

        if not project_operational_context.task_memory:
            gaps.append("No cumulative task memory is available yet for this project.")

        if not related_tasks:
            gaps.append("No strongly related historical tasks were found for this task.")

        explicit_task_paths = self._extract_task_paths(task)
        if explicit_task_paths and not any(
            path in {item.path for item in repository_index.files}
            for path in explicit_task_paths
        ):
            gaps.append(
                "Task mentions repository paths, but none of them currently exist in the repository index."
            )

        if not explicit_task_paths:
            gaps.append("Task metadata does not mention explicit existing repository paths.")

        if not candidate_paths and repository_index.total_files > 0:
            gaps.append("No strong existing candidate paths were recovered from project memory and repository index.")

        if repository_index.total_files == 0:
            gaps.append("Repository snapshot is empty; no existing code context is available.")

        return gaps

    def _normalize_selection(
        self,
        selection: CodeContextSelectionResult,
        selection_input: CodeContextSelectionInput,
    ) -> CodeContextSelectionResult:
        existing_repo_paths = {item.path for item in selection_input.repository_index.files}

        selection.task_id = selection_input.task_id
        selection.project_id = selection_input.project_id

        filtered_primary, dropped_primary = self._filter_existing_selected_files(
            selection.primary_targets,
            existing_repo_paths,
        )
        filtered_related, dropped_related = self._filter_existing_selected_files(
            selection.related_files,
            existing_repo_paths,
        )
        filtered_reference, dropped_reference = self._filter_existing_selected_files(
            selection.reference_files,
            existing_repo_paths,
        )
        filtered_tests, dropped_tests = self._filter_existing_selected_files(
            selection.related_test_files,
            existing_repo_paths,
        )

        selection.primary_targets = filtered_primary[: selection_input.constraints.max_primary_targets]
        selection.related_files = filtered_related[: selection_input.constraints.max_related_files]
        selection.reference_files = filtered_reference[: selection_input.constraints.max_reference_files]
        selection.related_test_files = filtered_tests[: selection_input.constraints.max_related_test_files]

        dropped_paths = self._dedupe_paths(
            dropped_primary + dropped_related + dropped_reference + dropped_tests
        )
        if dropped_paths:
            selection.context_gaps = list(selection.context_gaps) + [
                "Model proposed non-existing repository paths that were discarded: "
                + ", ".join(dropped_paths[:10])
            ]
            selection.evidence_summary.caution_signals = list(selection.evidence_summary.caution_signals) + [
                "Some model-selected paths were not present in the repository index and were ignored."
            ]

        primary_target_paths = [item.path for item in selection.primary_targets]

        normalized_pool = [
            path
            for path in self._dedupe_paths(selection.candidate_file_pool)
            if path in existing_repo_paths
        ]
        dropped_pool = [
            path
            for path in self._dedupe_paths(selection.candidate_file_pool)
            if path not in existing_repo_paths
        ]
        if dropped_pool:
            selection.context_gaps = list(selection.context_gaps) + [
                "Model proposed non-existing candidate file pool paths that were discarded: "
                + ", ".join(dropped_pool[:10])
            ]

        fallback_pool = [
            item.path for item in selection_input.candidate_paths
            if item.path in existing_repo_paths
        ]

        if not normalized_pool:
            normalized_pool = fallback_pool[:10]

        selection.candidate_file_pool = self._dedupe_paths(
            primary_target_paths + normalized_pool
        )[: max(selection_input.constraints.max_total_files, 1)]

        if not selection.primary_targets:
            fallback_primary = self._build_primary_fallback(selection_input)
            selection.primary_targets = fallback_primary[: selection_input.constraints.max_primary_targets]
            if fallback_primary:
                selection.context_gaps = list(selection.context_gaps) + [
                    "Primary targets were reconstructed from existing candidate paths because the model did not return a valid existing primary selection."
                ]

        if not selection.candidate_modules:
            inferred_modules: list[str] = []
            for path in selection.candidate_file_pool:
                module_name = Path(path).stem
                if module_name and module_name not in inferred_modules:
                    inferred_modules.append(module_name)
            selection.candidate_modules = inferred_modules[:12]

        merged_gaps: list[str] = []
        for item in selection_input.context_gaps + selection.context_gaps:
            normalized = item.strip() if item else ""
            if normalized and normalized not in merged_gaps:
                merged_gaps.append(normalized)
        selection.context_gaps = merged_gaps[:20]

        strengths = [
            item.strip()
            for item in selection.evidence_summary.strengths
            if item and item.strip()
        ]
        caution_signals = [
            item.strip()
            for item in selection.evidence_summary.caution_signals
            if item and item.strip()
        ]
        selection.evidence_summary.strengths = strengths[:12]
        selection.evidence_summary.caution_signals = caution_signals[:12]

        selection.context_sources_used = CodeContextSourcesUsed(
            used_project_memory=True,
            related_task_ids=[item.task_id for item in selection_input.related_tasks],
            candidate_paths_considered=len(selection_input.candidate_paths),
            repository_paths_considered=selection_input.repository_index.total_files,
        )

        total_unique_paths = set(selection.candidate_file_pool)
        for collection in (
            selection.primary_targets,
            selection.related_files,
            selection.reference_files,
            selection.related_test_files,
        ):
            for item in collection:
                total_unique_paths.add(item.path)

        if len(total_unique_paths) > selection_input.constraints.max_total_files:
            ordered_paths = self._dedupe_paths(
                [item.path for item in selection.primary_targets]
                + [item.path for item in selection.related_files]
                + [item.path for item in selection.reference_files]
                + [item.path for item in selection.related_test_files]
                + selection.candidate_file_pool
            )[: selection_input.constraints.max_total_files]
            selection.candidate_file_pool = ordered_paths

        if selection.repository_index.total_files if hasattr(selection, "repository_index") else False:
            pass

        return selection

    def _build_primary_fallback(
        self,
        selection_input: CodeContextSelectionInput,
    ) -> list[SelectedCodeFile]:
        fallback: list[SelectedCodeFile] = []
        for signal in selection_input.candidate_paths[: selection_input.constraints.max_primary_targets]:
            fallback.append(
                SelectedCodeFile(
                    path=signal.path,
                    role="target",
                    why_selected="Recovered from existing candidate path signals because no valid existing primary target was selected.",
                    selection_score=min(1.0, max(0.1, signal.score / 5.0)),
                    expected_usage="Inspect as primary existing context for this task.",
                    derived_from=signal.source_types[:6],
                )
            )
        return fallback

    @staticmethod
    def _artifact_summary(artifact: Artifact) -> str:
        content = artifact.content or ""
        if len(content) <= 400:
            return content
        return content[:400] + "..."

    def _task_tokens(self, task: Task) -> set[str]:
        return self._text_tokens(
            " ".join(
                [
                    task.title or "",
                    task.description or "",
                    task.summary or "",
                    task.objective or "",
                    task.proposed_solution or "",
                    task.implementation_notes or "",
                    task.implementation_steps or "",
                    task.acceptance_criteria or "",
                    task.tests_required or "",
                    task.technical_constraints or "",
                    task.out_of_scope or "",
                ]
            )
        )

    @staticmethod
    def _text_tokens(text: str) -> set[str]:
        return {
            token.lower()
            for token in _WORD_PATTERN.findall(text or "")
            if token.lower() not in _TOKEN_BLACKLIST and len(token) >= 3
        }

    @staticmethod
    def _extract_paths_from_text(text: str | None) -> list[str]:
        if not text:
            return []
        return list(dict.fromkeys(item.strip() for item in _PATH_PATTERN.findall(text)))

    def _extract_task_paths(self, task: Task) -> list[str]:
        texts = [
            task.title,
            task.description,
            task.summary,
            task.objective,
            task.proposed_solution,
            task.implementation_notes,
            task.implementation_steps,
            task.acceptance_criteria,
            task.tests_required,
            task.technical_constraints,
            task.out_of_scope,
        ]
        paths: list[str] = []
        for text in texts:
            paths.extend(self._extract_paths_from_text(text))
        return self._dedupe_paths(paths)

    def _paths_for_task_memory(self, task_id: int, project_operational_context) -> set[str]:
        found: set[str] = set()
        for path_signal in project_operational_context.referenced_paths:
            if task_id in path_signal.source_task_ids:
                found.add(path_signal.path)
        return found

    def _ancestor_ids(self, task: Task) -> set[int]:
        ids: set[int] = set()
        current = task.parent_task
        while current is not None:
            ids.add(current.id)
            current = current.parent_task
        return ids

    def _descendant_ids(self, task: Task) -> set[int]:
        ids: set[int] = set()
        stack = list(task.child_tasks)
        while stack:
            node = stack.pop()
            ids.add(node.id)
            stack.extend(node.child_tasks)
        return ids

    def _safe_read_file(self, workspace_dir: Path, path: str) -> str | None:
        try:
            return self.workspace_runtime.read_file(workspace_dir, path)
        except WorkspaceRuntimeError:
            return None

    @staticmethod
    def _extract_symbols(content: str | None) -> list[str]:
        if not content:
            return []
        return list(
            dict.fromkeys(
                re.findall(r"^\s*(?:def|class|async\s+def)\s+([A-Za-z_][A-Za-z0-9_]*)", content, re.MULTILINE)
            )
        )[:20]

    @staticmethod
    def _build_file_summary(path: str, content: str | None) -> str:
        if content is None:
            return f"{path} could not be read safely."
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        preview = " ".join(lines[:2])[:180]
        if not preview:
            return f"{path} is currently empty or has no short textual preview."
        return preview

    @staticmethod
    def _normalize_repo_path(path: str | None) -> str | None:
        if not path:
            return None

        normalized = path.strip().replace("\\", "/")
        normalized = re.sub(r"/{2,}", "/", normalized)

        if not normalized:
            return None
        if normalized.startswith("/") or normalized.startswith("../") or "/../" in normalized:
            return None
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized or normalized == ".":
            return None

        return normalized

    @staticmethod
    def _dedupe_paths(paths: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        for path in paths:
            normalized = CodeContextSelector._normalize_repo_path(path)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)

        return result

    @staticmethod
    def _dedupe_selected_files(files: list[SelectedCodeFile]) -> list[SelectedCodeFile]:
        deduped: list[SelectedCodeFile] = []
        seen: set[tuple[str, str]] = set()

        for item in files:
            normalized_path = CodeContextSelector._normalize_repo_path(item.path)
            if not normalized_path:
                continue
            key = (normalized_path, item.role)
            if key in seen:
                continue
            seen.add(key)
            item.path = normalized_path
            deduped.append(item)

        return deduped

    @staticmethod
    def _filter_existing_selected_files(
        files: list[SelectedCodeFile],
        existing_repo_paths: set[str],
    ) -> tuple[list[SelectedCodeFile], list[str]]:
        filtered: list[SelectedCodeFile] = []
        dropped: list[str] = []

        for item in CodeContextSelector._dedupe_selected_files(files):
            if item.path in existing_repo_paths:
                filtered.append(item)
            else:
                dropped.append(item.path)

        return filtered, dropped