from __future__ import annotations

from dataclasses import dataclass

from app.services.validation.contracts import ValidationEvidenceItem


@dataclass(frozen=True)
class CodeValidatorCapabilities:
    supported_evidence_kinds: tuple[str, ...] = (
        "produced_file",
        "command_output",
        "persisted_artifact",
        "artifact_reference",
    )
    supported_media_types: tuple[str, ...] = (
        "text/plain",
        "application/json",
        None,
    )
    supported_representation_kinds: tuple[str, ...] = (
        "full_text",
        "command_output",
        "artifact_preview",
        "summary",
    )


CODE_VALIDATOR_CAPABILITIES = CodeValidatorCapabilities()


def supports_code_validation_evidence(item: ValidationEvidenceItem) -> bool:
    if item.evidence_kind not in CODE_VALIDATOR_CAPABILITIES.supported_evidence_kinds:
        return False
    if item.media_type not in CODE_VALIDATOR_CAPABILITIES.supported_media_types:
        return False
    if (
        item.representation_kind
        not in CODE_VALIDATOR_CAPABILITIES.supported_representation_kinds
    ):
        return False
    return True
