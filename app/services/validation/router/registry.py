from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationRouterCatalogEntry:
    validator_key: str
    discipline: str
    description: str
    typical_deliverables: list[str]
    typical_evidence: list[str]


VALIDATION_ROUTER_CATALOG: tuple[ValidationRouterCatalogEntry, ...] = (
    ValidationRouterCatalogEntry(
        validator_key="code_task_validator",
        discipline="code",
        description=(
            "Validates repository-level implementation work such as source code changes, "
            "test updates, configuration changes, and code-adjacent deliverables using "
            "execution evidence, workspace inspection, and file reading."
        ),
        typical_deliverables=[
            "source code implementation",
            "test updates",
            "configuration changes",
            "repository documentation tightly coupled to implementation",
        ],
        typical_evidence=[
            "changed files",
            "command results",
            "persisted artifacts",
            "workspace contents",
            "execution summaries",
        ],
    ),
)


def list_validation_router_catalog() -> list[ValidationRouterCatalogEntry]:
    return list(VALIDATION_ROUTER_CATALOG)


def render_validation_router_catalog() -> str:
    lines: list[str] = []
    for entry in VALIDATION_ROUTER_CATALOG:
        deliverables = "; ".join(entry.typical_deliverables)
        evidence = "; ".join(entry.typical_evidence)
        lines.append(
            "\n".join(
                [
                    f"- validator_key: {entry.validator_key}",
                    f"  discipline: {entry.discipline}",
                    f"  description: {entry.description}",
                    f"  typical_deliverables: {deliverables}",
                    f"  typical_evidence: {evidence}",
                ]
            )
        )
    return "\n\n".join(lines)