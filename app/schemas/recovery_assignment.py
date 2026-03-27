from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AssignmentResolvedAction = Literal[
    "continue_current_plan",
    "resequence_remaining_batches",
]

AssignmentMode = Literal[
    "continue_with_assignment",
    "resequence_with_assignment",
]

AssignmentTaskType = Literal[
    "implementation",
    "test",
    "documentation",
    "configuration",
    "refactor",
]

AssignmentPriority = Literal[
    "low",
    "medium",
    "high",
]

AssignmentImpactType = Literal[
    "immediate_blocking",
    "future_blocking",
    "additive_deferred",
    "corrective_local",
    "structural_conflict",
]

AssignmentPlacementRelation = Literal[
    "before_next_useful_progress",
    "before_first_consumer_batch",
    "after_current_tail",
    "requires_replan",
]

KnownTaskRelationType = Literal[
    "depends_on_existing",
    "existing_depends_on_new",
    "same_scope_affinity",
    "possible_consumer_relation",
]

TaskGroupingRole = Literal[
    "core",
    "dependent",
    "independent",
]


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_required(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Field cannot be empty.")
    return cleaned


def _dedupe_preserve_order(values: list[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


class ExecutedBatchAssignmentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(..., min_length=1)
    batch_name: str = Field(..., min_length=1)
    goal: str = Field(..., min_length=3)

    executed_task_ids: list[int] = Field(default_factory=list)
    completed_task_ids: list[int] = Field(default_factory=list)
    partial_task_ids: list[int] = Field(default_factory=list)
    failed_task_ids: list[int] = Field(default_factory=list)

    summary: str = Field(..., min_length=10)
    key_findings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_fields(self) -> "ExecutedBatchAssignmentSummary":
        self.batch_id = _clean_required(self.batch_id)
        self.batch_name = _clean_required(self.batch_name)
        self.goal = _clean_required(self.goal)
        self.summary = _clean_required(self.summary)

        self.key_findings = [item.strip() for item in self.key_findings if item and item.strip()]

        for field_name in (
            "executed_task_ids",
            "completed_task_ids",
            "partial_task_ids",
            "failed_task_ids",
        ):
            values = getattr(self, field_name)
            if any(task_id <= 0 for task_id in values):
                raise ValueError(f"{field_name} must contain only positive integers.")
            setattr(self, field_name, _dedupe_preserve_order(values))

        return self


class AssignmentEvaluationSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(..., min_length=3)
    decision_summary: str = Field(..., min_length=10)

    recommended_next_action: str | None = None
    recommended_next_action_reason: str | None = None

    plan_change_scope: Literal[
        "none",
        "local_resequencing",
        "remaining_plan_rebuild",
        "high_level_replan",
    ] = "none"

    remaining_plan_still_valid: bool
    new_recovery_tasks_blocking: bool | None = None
    single_task_tail_risk: bool = False

    decision_signals: list[str] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_fields(self) -> "AssignmentEvaluationSignals":
        self.decision = _clean_required(self.decision)
        self.decision_summary = _clean_required(self.decision_summary)
        self.recommended_next_action = _clean_optional(self.recommended_next_action)
        self.recommended_next_action_reason = _clean_optional(self.recommended_next_action_reason)

        self.decision_signals = [
            item.strip() for item in self.decision_signals if item and item.strip()
        ]
        self.key_risks = [item.strip() for item in self.key_risks if item and item.strip()]
        self.notes = [item.strip() for item in self.notes if item and item.strip()]

        return self


class AssignmentRecoverySignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_task_id: int = Field(..., gt=0)
    source_run_id: int = Field(..., gt=0)

    recovery_action: str = Field(..., min_length=3)
    recovery_reason: str = Field(..., min_length=10)
    covered_gap_summary: str = Field(..., min_length=10)

    still_blocks_progress: bool
    execution_guidance: str | None = None
    evaluation_guidance: str | None = None

    @model_validator(mode="after")
    def normalize_fields(self) -> "AssignmentRecoverySignal":
        self.recovery_action = _clean_required(self.recovery_action)
        self.recovery_reason = _clean_required(self.recovery_reason)
        self.covered_gap_summary = _clean_required(self.covered_gap_summary)
        self.execution_guidance = _clean_optional(self.execution_guidance)
        self.evaluation_guidance = _clean_optional(self.evaluation_guidance)
        return self


class AssignmentRecoverySignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[AssignmentRecoverySignal] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_entries(self) -> "AssignmentRecoverySignals":
        seen_pairs: set[tuple[int, int]] = set()
        for entry in self.entries:
            key = (entry.source_task_id, entry.source_run_id)
            if key in seen_pairs:
                raise ValueError(
                    "AssignmentRecoverySignals.entries cannot contain duplicate "
                    "(source_task_id, source_run_id) pairs."
                )
            seen_pairs.add(key)
        return self


class RecoveryTaskForAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: int = Field(..., gt=0)
    title: str = Field(..., min_length=3)
    description: str = Field(..., min_length=10)

    objective: str | None = None
    implementation_notes: str | None = None
    acceptance_criteria: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None

    task_type: AssignmentTaskType = "implementation"
    priority: AssignmentPriority = "medium"

    parent_task_id: int | None = Field(default=None, gt=0)
    parent_task_title: str | None = None
    sequence_order: int | None = None

    source_task_id: int | None = Field(default=None, gt=0)
    source_run_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def normalize_fields(self) -> "RecoveryTaskForAssignment":
        self.title = _clean_required(self.title)
        self.description = _clean_required(self.description)
        self.objective = _clean_optional(self.objective)
        self.implementation_notes = _clean_optional(self.implementation_notes)
        self.acceptance_criteria = _clean_optional(self.acceptance_criteria)
        self.technical_constraints = _clean_optional(self.technical_constraints)
        self.out_of_scope = _clean_optional(self.out_of_scope)
        self.parent_task_title = _clean_optional(self.parent_task_title)

        if self.sequence_order is not None and self.sequence_order < 0:
            raise ValueError("sequence_order must be >= 0 when provided.")

        if (self.source_task_id is None) != (self.source_run_id is None):
            raise ValueError(
                "source_task_id and source_run_id must either both be present or both be omitted."
            )

        return self


class RemainingBatchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(..., min_length=1)
    batch_name: str = Field(..., min_length=1)
    batch_index: int = Field(..., ge=1)
    goal: str = Field(..., min_length=3)

    task_ids: list[int] = Field(default_factory=list)
    task_titles: list[str] = Field(default_factory=list)

    checkpoint_reason: str | None = None
    is_patch_batch: bool = False

    @model_validator(mode="after")
    def normalize_fields(self) -> "RemainingBatchSummary":
        self.batch_id = _clean_required(self.batch_id)
        self.batch_name = _clean_required(self.batch_name)
        self.goal = _clean_required(self.goal)
        self.checkpoint_reason = _clean_optional(self.checkpoint_reason)

        if any(task_id <= 0 for task_id in self.task_ids):
            raise ValueError("task_ids must contain only positive integers.")
        self.task_ids = _dedupe_preserve_order(self.task_ids)

        self.task_titles = [item.strip() for item in self.task_titles if item and item.strip()]

        if self.task_titles and len(self.task_titles) != len(self.task_ids):
            raise ValueError(
                "task_titles must be empty or have the same length as task_ids."
            )

        return self


class LivePlanSummaryForAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_version: int = Field(..., ge=1)

    current_batch_id: str = Field(..., min_length=1)
    current_batch_name: str = Field(..., min_length=1)

    remaining_batches: list[RemainingBatchSummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_batches(self) -> "LivePlanSummaryForAssignment":
        self.current_batch_id = _clean_required(self.current_batch_id)
        self.current_batch_name = _clean_required(self.current_batch_name)

        batch_ids = [batch.batch_id for batch in self.remaining_batches]
        if len(batch_ids) != len(set(batch_ids)):
            raise ValueError("remaining_batches cannot contain duplicate batch_id values.")

        return self


class NextUsefulProgressSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., min_length=10)
    task_ids: list[int] = Field(default_factory=list)
    batch_id: str | None = None
    batch_name: str | None = None

    @model_validator(mode="after")
    def normalize_fields(self) -> "NextUsefulProgressSummary":
        self.summary = _clean_required(self.summary)
        self.batch_id = _clean_optional(self.batch_id)
        self.batch_name = _clean_optional(self.batch_name)

        if any(task_id <= 0 for task_id in self.task_ids):
            raise ValueError("task_ids must contain only positive integers.")
        self.task_ids = _dedupe_preserve_order(self.task_ids)

        if (self.batch_id is None) != (self.batch_name is None):
            raise ValueError(
                "batch_id and batch_name must either both be present or both be omitted."
            )

        return self


class PendingTaskSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: int = Field(..., gt=0)
    title: str = Field(..., min_length=3)

    parent_task_id: int | None = Field(default=None, gt=0)
    parent_task_title: str | None = None

    status: str = Field(..., min_length=3)
    is_blocked: bool = False
    sequence_order: int | None = None

    @model_validator(mode="after")
    def normalize_fields(self) -> "PendingTaskSummary":
        self.title = _clean_required(self.title)
        self.parent_task_title = _clean_optional(self.parent_task_title)
        self.status = _clean_required(self.status)

        if self.sequence_order is not None and self.sequence_order < 0:
            raise ValueError("sequence_order must be >= 0 when provided.")

        return self


class NewTaskInternalDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predecessor_task_id: int = Field(..., gt=0)
    successor_task_id: int = Field(..., gt=0)
    reason: str = Field(..., min_length=3)

    @model_validator(mode="after")
    def validate_dependency(self) -> "NewTaskInternalDependency":
        if self.predecessor_task_id == self.successor_task_id:
            raise ValueError("A task cannot depend on itself.")
        self.reason = _clean_required(self.reason)
        return self


class NewTaskExistingTaskRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_task_id: int = Field(..., gt=0)
    existing_task_id: int = Field(..., gt=0)
    relation: KnownTaskRelationType
    reason: str = Field(..., min_length=3)

    @model_validator(mode="after")
    def validate_relationship(self) -> "NewTaskExistingTaskRelationship":
        self.reason = _clean_required(self.reason)
        return self


class KnownAssignmentRelationships(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_task_internal_dependencies: list[NewTaskInternalDependency] = Field(default_factory=list)
    new_task_to_existing_task_dependencies: list[NewTaskExistingTaskRelationship] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_relationships(self) -> "KnownAssignmentRelationships":
        seen_internal: set[tuple[int, int]] = set()
        for item in self.new_task_internal_dependencies:
            key = (item.predecessor_task_id, item.successor_task_id)
            if key in seen_internal:
                raise ValueError(
                    "new_task_internal_dependencies cannot contain duplicate edges."
                )
            seen_internal.add(key)

        seen_external: set[tuple[int, int, str]] = set()
        for item in self.new_task_to_existing_task_dependencies:
            key = (item.new_task_id, item.existing_task_id, item.relation)
            if key in seen_external:
                raise ValueError(
                    "new_task_to_existing_task_dependencies cannot contain duplicate relations."
                )
            seen_external.add(key)

        return self


class RecoveryAssignmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(..., gt=0)
    project_goal: str = Field(..., min_length=10)
    current_stage_summary: str | None = None

    resolved_action: AssignmentResolvedAction
    assignment_mode: AssignmentMode

    executed_batch_summary: ExecutedBatchAssignmentSummary
    evaluation_signals: AssignmentEvaluationSignals
    recovery_signals: AssignmentRecoverySignals = Field(
        default_factory=AssignmentRecoverySignals
    )

    new_tasks: list[RecoveryTaskForAssignment] = Field(default_factory=list)

    live_plan_summary: LivePlanSummaryForAssignment
    next_useful_progress: NextUsefulProgressSummary | None = None
    pending_valid_tasks: list[PendingTaskSummary] = Field(default_factory=list)
    known_relationships: KnownAssignmentRelationships = Field(
        default_factory=KnownAssignmentRelationships
    )

    @model_validator(mode="after")
    def validate_input(self) -> "RecoveryAssignmentInput":
        self.project_goal = _clean_required(self.project_goal)
        self.current_stage_summary = _clean_optional(self.current_stage_summary)

        if not self.new_tasks:
            raise ValueError(
                "RecoveryAssignmentInput requires at least one new recovery task."
            )

        if self.resolved_action == "continue_current_plan":
            if self.assignment_mode != "continue_with_assignment":
                raise ValueError(
                    "assignment_mode must be 'continue_with_assignment' when "
                    "resolved_action='continue_current_plan'."
                )

        if self.resolved_action == "resequence_remaining_batches":
            if self.assignment_mode != "resequence_with_assignment":
                raise ValueError(
                    "assignment_mode must be 'resequence_with_assignment' when "
                    "resolved_action='resequence_remaining_batches'."
                )

        new_task_ids = [task.task_id for task in self.new_tasks]
        if len(new_task_ids) != len(set(new_task_ids)):
            raise ValueError("new_tasks cannot contain duplicate task_id values.")

        new_task_id_set = set(new_task_ids)

        for edge in self.known_relationships.new_task_internal_dependencies:
            if edge.predecessor_task_id not in new_task_id_set:
                raise ValueError(
                    "All internal dependency predecessor_task_id values must exist in new_tasks."
                )
            if edge.successor_task_id not in new_task_id_set:
                raise ValueError(
                    "All internal dependency successor_task_id values must exist in new_tasks."
                )

        for relation in self.known_relationships.new_task_to_existing_task_dependencies:
            if relation.new_task_id not in new_task_id_set:
                raise ValueError(
                    "All new_task_to_existing_task_dependencies.new_task_id values "
                    "must exist in new_tasks."
                )

        pending_ids = [task.task_id for task in self.pending_valid_tasks]
        if len(pending_ids) != len(set(pending_ids)):
            raise ValueError("pending_valid_tasks cannot contain duplicate task_id values.")

        return self


class AssignmentTaskAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: int = Field(..., gt=0)
    impact_type: AssignmentImpactType
    grouping_role: TaskGroupingRole = "core"
    suggested_cluster_id: str = Field(..., min_length=1)

    depends_on_new_task_ids: list[int] = Field(default_factory=list)
    depends_on_existing_task_ids: list[int] = Field(default_factory=list)

    rationale: str = Field(..., min_length=10)

    @model_validator(mode="after")
    def normalize_fields(self) -> "AssignmentTaskAssessment":
        self.suggested_cluster_id = _clean_required(self.suggested_cluster_id)
        self.rationale = _clean_required(self.rationale)

        if any(task_id <= 0 for task_id in self.depends_on_new_task_ids):
            raise ValueError("depends_on_new_task_ids must contain only positive integers.")
        if any(task_id <= 0 for task_id in self.depends_on_existing_task_ids):
            raise ValueError("depends_on_existing_task_ids must contain only positive integers.")

        self.depends_on_new_task_ids = _dedupe_preserve_order(self.depends_on_new_task_ids)
        self.depends_on_existing_task_ids = _dedupe_preserve_order(
            self.depends_on_existing_task_ids
        )

        if self.task_id in self.depends_on_new_task_ids:
            raise ValueError("A task cannot depend on itself in depends_on_new_task_ids.")

        return self


class AssignmentClusterProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str = Field(..., min_length=1)
    task_ids_in_execution_order: list[int] = Field(..., min_length=1)

    impact_type: AssignmentImpactType
    grouped_execution_required: bool = True

    placement_relation: AssignmentPlacementRelation
    rationale: str = Field(..., min_length=10)

    @model_validator(mode="after")
    def normalize_fields(self) -> "AssignmentClusterProposal":
        self.cluster_id = _clean_required(self.cluster_id)
        self.rationale = _clean_required(self.rationale)

        if any(task_id <= 0 for task_id in self.task_ids_in_execution_order):
            raise ValueError(
                "task_ids_in_execution_order must contain only positive integers."
            )

        deduped = _dedupe_preserve_order(self.task_ids_in_execution_order)
        if len(deduped) != len(self.task_ids_in_execution_order):
            raise ValueError(
                "task_ids_in_execution_order cannot contain duplicate task ids."
            )
        self.task_ids_in_execution_order = deduped

        if self.impact_type == "structural_conflict":
            if self.placement_relation != "requires_replan":
                raise ValueError(
                    "impact_type='structural_conflict' requires "
                    "placement_relation='requires_replan'."
                )

        if self.placement_relation == "requires_replan":
            if self.impact_type != "structural_conflict":
                raise ValueError(
                    "placement_relation='requires_replan' is only valid for "
                    "impact_type='structural_conflict'."
                )

        if self.impact_type == "immediate_blocking":
            if self.placement_relation != "before_next_useful_progress":
                raise ValueError(
                    "impact_type='immediate_blocking' requires "
                    "placement_relation='before_next_useful_progress'."
                )

        if self.impact_type == "future_blocking":
            if self.placement_relation != "before_first_consumer_batch":
                raise ValueError(
                    "impact_type='future_blocking' requires "
                    "placement_relation='before_first_consumer_batch'."
                )

        if self.impact_type in {"additive_deferred", "corrective_local"}:
            if self.placement_relation not in {
                "before_first_consumer_batch",
                "after_current_tail",
            }:
                raise ValueError(
                    "impact_type='additive_deferred' or 'corrective_local' requires "
                    "placement_relation in {'before_first_consumer_batch', 'after_current_tail'}."
                )

        return self


class RecoveryAssignmentLLMOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal[
        "continue_with_assignment",
        "resequence_with_assignment",
        "requires_replan",
    ]

    task_assessments: list[AssignmentTaskAssessment] = Field(default_factory=list)
    clusters: list[AssignmentClusterProposal] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_output(self) -> "RecoveryAssignmentLLMOutput":
        self.notes = [item.strip() for item in self.notes if item and item.strip()]

        if self.strategy == "requires_replan":
            if not self.clusters:
                raise ValueError(
                    "strategy='requires_replan' still requires clusters explaining the conflict."
                )

        cluster_ids = [cluster.cluster_id for cluster in self.clusters]
        if len(cluster_ids) != len(set(cluster_ids)):
            raise ValueError("clusters cannot contain duplicate cluster_id values.")

        task_ids_from_assessments = [item.task_id for item in self.task_assessments]
        if len(task_ids_from_assessments) != len(set(task_ids_from_assessments)):
            raise ValueError("task_assessments cannot contain duplicate task_id values.")

        suggested_cluster_ids = {item.suggested_cluster_id for item in self.task_assessments}
        unknown_suggested = suggested_cluster_ids.difference(cluster_ids)
        if unknown_suggested:
            raise ValueError(
                "Every task_assessment.suggested_cluster_id must exist in clusters. "
                f"Unknown cluster ids: {sorted(unknown_suggested)}"
            )

        cluster_task_id_set: set[int] = set()
        for cluster in self.clusters:
            repeated = set(cluster.task_ids_in_execution_order).intersection(cluster_task_id_set)
            if repeated:
                raise ValueError(
                    "A task cannot appear in more than one cluster. "
                    f"Repeated task ids: {sorted(repeated)}"
                )
            cluster_task_id_set.update(cluster.task_ids_in_execution_order)

        assessment_task_id_set = set(task_ids_from_assessments)
        if cluster_task_id_set != assessment_task_id_set:
            missing_in_clusters = sorted(assessment_task_id_set.difference(cluster_task_id_set))
            missing_in_assessments = sorted(cluster_task_id_set.difference(assessment_task_id_set))
            raise ValueError(
                "task_assessments and clusters must cover exactly the same task ids. "
                f"missing_in_clusters={missing_in_clusters}, "
                f"missing_in_assessments={missing_in_assessments}"
            )

        assessment_by_task_id = {item.task_id: item for item in self.task_assessments}
        cluster_by_id = {cluster.cluster_id: cluster for cluster in self.clusters}

        for cluster in self.clusters:
            for task_id in cluster.task_ids_in_execution_order:
                assessment = assessment_by_task_id[task_id]
                if assessment.suggested_cluster_id != cluster.cluster_id:
                    raise ValueError(
                        f"Task {task_id} is in cluster '{cluster.cluster_id}' but its "
                        f"suggested_cluster_id is '{assessment.suggested_cluster_id}'."
                    )

                if assessment.impact_type != cluster.impact_type:
                    raise ValueError(
                        f"Task {task_id} has impact_type='{assessment.impact_type}' but "
                        f"cluster '{cluster.cluster_id}' has impact_type='{cluster.impact_type}'."
                    )

        for assessment in self.task_assessments:
            cluster = cluster_by_id[assessment.suggested_cluster_id]
            cluster_task_positions = {
                task_id: index for index, task_id in enumerate(cluster.task_ids_in_execution_order)
            }
            current_position = cluster_task_positions[assessment.task_id]

            for dependency_task_id in assessment.depends_on_new_task_ids:
                if dependency_task_id not in cluster_task_positions:
                    raise ValueError(
                        f"Task {assessment.task_id} depends on new task {dependency_task_id}, "
                        "but that dependency is not present in the same cluster. "
                        "Cross-cluster new-task dependencies are not allowed in the LLM contract."
                    )
                dependency_position = cluster_task_positions[dependency_task_id]
                if dependency_position >= current_position:
                    raise ValueError(
                        f"Task {assessment.task_id} depends on new task {dependency_task_id}, "
                        "but the cluster execution order does not place the dependency earlier."
                    )

        return self