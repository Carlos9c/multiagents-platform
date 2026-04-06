from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.execution_engine.contracts import ExecutionRequest


@dataclass(frozen=True)
class ResolvedContextPath:
    logical_path: str
    resolved_path: str | None
    source_kind: str | None
    exists: bool


@dataclass(frozen=True)
class TextResource:
    logical_path: str
    resolved_path: str | None
    source_kind: str | None
    exists: bool
    content: str | None
    error: str | None = None


def resolve_context_path(
    execution_request: ExecutionRequest,
    *,
    logical_path: str,
) -> ResolvedContextPath:
    normalized = (logical_path or "").strip()
    if not normalized:
        return ResolvedContextPath(
            logical_path="",
            resolved_path=None,
            source_kind=None,
            exists=False,
        )

    workspace_path = execution_request.context.workspace_path
    if workspace_path:
        workspace_candidate = Path(workspace_path) / normalized
        if workspace_candidate.exists():
            return ResolvedContextPath(
                logical_path=normalized,
                resolved_path=str(workspace_candidate),
                source_kind="workspace",
                exists=True,
            )

    source_path = execution_request.context.source_path
    if source_path:
        source_candidate = Path(source_path) / normalized
        if source_candidate.exists():
            return ResolvedContextPath(
                logical_path=normalized,
                resolved_path=str(source_candidate),
                source_kind="source",
                exists=True,
            )

    return ResolvedContextPath(
        logical_path=normalized,
        resolved_path=None,
        source_kind=None,
        exists=False,
    )


def read_text_from_context(
    execution_request: ExecutionRequest,
    *,
    logical_path: str,
) -> TextResource:
    resolved = resolve_context_path(
        execution_request,
        logical_path=logical_path,
    )

    if not resolved.exists or not resolved.resolved_path:
        return TextResource(
            logical_path=resolved.logical_path,
            resolved_path=None,
            source_kind=None,
            exists=False,
            content=None,
            error="resource_not_found",
        )

    path = Path(resolved.resolved_path)
    if not path.is_file():
        return TextResource(
            logical_path=resolved.logical_path,
            resolved_path=str(path),
            source_kind=resolved.source_kind,
            exists=False,
            content=None,
            error="resource_is_not_a_file",
        )

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return TextResource(
                logical_path=resolved.logical_path,
                resolved_path=str(path),
                source_kind=resolved.source_kind,
                exists=True,
                content=None,
                error=f"resource_read_failed: {str(exc)}",
            )
    except Exception as exc:
        return TextResource(
            logical_path=resolved.logical_path,
            resolved_path=str(path),
            source_kind=resolved.source_kind,
            exists=True,
            content=None,
            error=f"resource_read_failed: {str(exc)}",
        )

    return TextResource(
        logical_path=resolved.logical_path,
        resolved_path=str(path),
        source_kind=resolved.source_kind,
        exists=True,
        content=content,
        error=None,
    )


def read_many_text_resources(
    execution_request: ExecutionRequest,
    *,
    logical_paths: list[str],
) -> list[TextResource]:
    seen: set[str] = set()
    resources: list[TextResource] = []

    for logical_path in logical_paths:
        normalized = (logical_path or "").strip()
        if not normalized or normalized in seen:
            continue

        seen.add(normalized)
        resources.append(
            read_text_from_context(
                execution_request,
                logical_path=normalized,
            )
        )

    return resources


def read_relevant_files_from_request(
    execution_request: ExecutionRequest,
) -> list[TextResource]:
    return read_many_text_resources(
        execution_request,
        logical_paths=list(execution_request.context.relevant_files or []),
    )
