from __future__ import annotations

from dataclasses import dataclass

from app.services.validation.contracts import (
    TaskValidationInput,
    ValidationEvidenceItem,
)
from app.services.validation.validators.code.capabilities import (
    supports_code_validation_evidence,
)


@dataclass
class CodeValidationRenderableEvidence:
    supported_items: list[ValidationEvidenceItem]
    unsupported_items: list[ValidationEvidenceItem]
    rendered_text: str


def _render_evidence_item(item: ValidationEvidenceItem) -> str:
    header = [
        f"Evidence ID: {item.evidence_id}",
        f"Kind: {item.evidence_kind}",
        f"Source: {item.source}",
    ]
    if item.path:
        header.append(f"Path: {item.path}")
    if item.logical_name:
        header.append(f"Logical name: {item.logical_name}")
    if item.change_type:
        header.append(f"Change type: {item.change_type}")
    if item.media_type:
        header.append(f"Media type: {item.media_type}")
    header.append(f"Representation: {item.representation_kind}")

    body_parts: list[str] = []
    if item.content_summary:
        body_parts.append("Summary:")
        body_parts.append(item.content_summary)
    if item.content_text:
        body_parts.append("Content:")
        body_parts.append(item.content_text)
    if item.structured_content:
        body_parts.append("Structured content:")
        body_parts.append(str(item.structured_content))

    return "\n".join(header + [""] + body_parts).strip()


def render_code_validation_evidence(
    *,
    validation_input: TaskValidationInput,
) -> CodeValidationRenderableEvidence:
    supported_items: list[ValidationEvidenceItem] = []
    unsupported_items: list[ValidationEvidenceItem] = []

    for item in validation_input.evidence_package.evidence_items:
        if supports_code_validation_evidence(item):
            supported_items.append(item)
        else:
            unsupported_items.append(item)

    sections: list[str] = []

    for index, item in enumerate(supported_items, start=1):
        sections.append(f"--- Evidence Item {index} ---")
        sections.append(_render_evidence_item(item))

    rendered_text = "\n\n".join(sections).strip()

    return CodeValidationRenderableEvidence(
        supported_items=supported_items,
        unsupported_items=unsupported_items,
        rendered_text=rendered_text,
    )
