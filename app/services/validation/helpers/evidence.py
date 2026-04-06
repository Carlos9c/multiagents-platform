from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.execution_engine.contracts import EvidenceItem, ExecutionEvidence


@dataclass(frozen=True)
class ProducerEvidenceView:
    producer: str
    items: list[EvidenceItem]

    def has_any(self) -> bool:
        return bool(self.items)

    def paths(self) -> list[str]:
        ordered_paths: list[str] = []

        for item in self.items:
            path = item.path
            if path and path not in ordered_paths:
                ordered_paths.append(path)

            payload = item.payload or {}
            depends_on = payload.get("depends_on")
            if isinstance(depends_on, list):
                for dependency in depends_on:
                    if isinstance(dependency, str) and dependency and dependency not in ordered_paths:
                        ordered_paths.append(dependency)

        return ordered_paths

    def filter_by_type(self, *evidence_types: str) -> list[EvidenceItem]:
        allowed = set(evidence_types)
        return [item for item in self.items if item.evidence_type in allowed]


def flatten_evidence(evidence: ExecutionEvidence) -> list[EvidenceItem]:
    return evidence.to_evidence_items()


def filter_items_by_producer(
    items: list[EvidenceItem],
    *,
    producer: str,
) -> list[EvidenceItem]:
    return [item for item in items if item.producer == producer]


def build_producer_evidence_view(
    evidence: ExecutionEvidence,
    *,
    producer: str,
) -> ProducerEvidenceView:
    items = flatten_evidence(evidence)
    return ProducerEvidenceView(
        producer=producer,
        items=filter_items_by_producer(items, producer=producer),
    )


def has_evidence_for_producer(
    evidence: ExecutionEvidence,
    *,
    producer: str,
) -> bool:
    return build_producer_evidence_view(evidence, producer=producer).has_any()


def collect_all_producers(evidence: ExecutionEvidence) -> list[str]:
    ordered: list[str] = []

    for item in flatten_evidence(evidence):
        if item.producer and item.producer not in ordered:
            ordered.append(item.producer)

    return ordered


def collect_paths_from_items(items: list[EvidenceItem]) -> list[str]:
    ordered_paths: list[str] = []

    def _append(path: str | None) -> None:
        if not path:
            return
        if path not in ordered_paths:
            ordered_paths.append(path)

    for item in items:
        _append(item.path)

        payload = item.payload or {}
        depends_on = payload.get("depends_on")
        if isinstance(depends_on, list):
            for dependency in depends_on:
                if isinstance(dependency, str):
                    _append(dependency)

    return ordered_paths


def collect_paths_from_producer(
    evidence: ExecutionEvidence,
    *,
    producer: str,
) -> list[str]:
    view = build_producer_evidence_view(evidence, producer=producer)
    return view.paths()


def filter_items_by_type(
    items: list[EvidenceItem],
    *,
    evidence_types: Iterable[str],
) -> list[EvidenceItem]:
    allowed = set(evidence_types)
    return [item for item in items if item.evidence_type in allowed]


def order_producers_by_execution_sequence(
    *,
    execution_agent_sequence: Iterable[str],
    available_producers: Iterable[str],
    ignored_producers: Iterable[str] | None = None,
) -> list[str]:
    ignored = set(ignored_producers or [])
    available = [item for item in available_producers if item not in ignored]

    ordered: list[str] = []

    for producer in execution_agent_sequence:
        if producer in ignored:
            continue
        if producer not in available:
            continue
        if producer in ordered:
            continue
        ordered.append(producer)

    for producer in available:
        if producer not in ordered:
            ordered.append(producer)

    return ordered