from typing import Literal

from pydantic import BaseModel, Field, model_validator


RiskLevel = Literal["low", "medium", "high"]
PlanningScope = Literal["project_atomic_tasks", "refined_task_atomic_tasks"]
CheckpointEvaluationFocus = Literal[
    "architecture_alignment",
    "functional_coverage",
    "artifact_consistency",
    "task_completion_quality",
    "dependency_validation",
    "risk_control",
    "stage_closure",
]


class ProjectExecutionContext(BaseModel):
    project_id: int
    project_name: str
    project_goal: str
    project_summary: str | None = None
    current_execution_objective: str


class CandidateAtomicTask(BaseModel):
    task_id: int
    title: str
    description: str | None = None
    summary: str | None = None
    objective: str | None = None
    task_type: str
    priority: str
    planning_level: str
    executor_type: str
    status: str
    parent_task_id: int | None = None
    parent_refined_title: str | None = None
    parent_high_level_title: str | None = None
    implementation_steps: str | None = None
    acceptance_criteria: str | None = None
    tests_required: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None

    @model_validator(mode="after")
    def validate_atomic_level(self):
        if self.planning_level != "atomic":
            raise ValueError("Execution sequencer only accepts atomic tasks as candidates.")
        return self


class CompletedTaskSummary(BaseModel):
    task_id: int
    title: str
    status: str
    completed_scope: str | None = None
    artifacts_created: str | None = None
    validation_notes: str | None = None


class UnfinishedTaskSummary(BaseModel):
    task_id: int
    title: str
    task_status: str
    last_run_status: str | None = None
    failure_type: str | None = None
    failure_code: str | None = None
    completed_scope: str | None = None
    remaining_scope: str | None = None
    blockers_found: str | None = None


class RelevantArtifactSummary(BaseModel):
    artifact_id: int
    artifact_type: str
    task_id: int | None = None
    summary: str


class ExecutionStateSummary(BaseModel):
    completed_tasks: list[CompletedTaskSummary] = Field(default_factory=list)
    unfinished_tasks: list[UnfinishedTaskSummary] = Field(default_factory=list)
    relevant_artifacts: list[RelevantArtifactSummary] = Field(default_factory=list)


class ExecutionSequencingInstructions(BaseModel):
    goal: str
    requirements: list[str] = Field(default_factory=list)
    checkpoint_policy: str


class ExecutionBatch(BaseModel):
    batch_internal_id: str
    batch_id: str
    batch_index: int
    plan_version: int
    name: str
    goal: str
    task_ids: list[int] = Field(default_factory=list)
    entry_conditions: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    risk_level: RiskLevel
    checkpoint_after: bool = True
    checkpoint_id: str
    checkpoint_reason: str

    is_patch_batch: bool = False
    anchor_batch_index: int | None = None
    patch_index: int | None = None

    @model_validator(mode="after")
    def validate_batch(self):
        if self.batch_index < 1:
            raise ValueError("ExecutionBatch.batch_index must be >= 1.")
        if self.plan_version < 1:
            raise ValueError("ExecutionBatch.plan_version must be >= 1.")
        if not self.task_ids:
            raise ValueError("ExecutionBatch.task_ids cannot be empty.")
        if not self.checkpoint_after:
            raise ValueError("Every execution batch must end with a checkpoint.")
        if not self.batch_internal_id:
            raise ValueError("ExecutionBatch.batch_internal_id is required.")
        if not self.batch_id:
            raise ValueError("ExecutionBatch.batch_id is required.")
        if not self.checkpoint_id:
            raise ValueError("ExecutionBatch.checkpoint_id is required.")
        if not self.checkpoint_reason:
            raise ValueError("ExecutionBatch.checkpoint_reason is required.")

        if self.is_patch_batch:
            if self.anchor_batch_index is None or self.anchor_batch_index < 1:
                raise ValueError("Patch batches require anchor_batch_index >= 1.")
            if self.patch_index is None or self.patch_index < 1:
                raise ValueError("Patch batches require patch_index >= 1.")
        else:
            if self.anchor_batch_index is not None:
                raise ValueError("Non-patch batches cannot define anchor_batch_index.")
            if self.patch_index is not None:
                raise ValueError("Non-patch batches cannot define patch_index.")

        return self


class CheckpointDefinition(BaseModel):
    checkpoint_id: str
    name: str
    reason: str
    after_batch_id: str
    evaluation_goal: str
    evaluation_focus: list[CheckpointEvaluationFocus] = Field(default_factory=list)
    can_introduce_new_tasks: bool = True
    can_resequence_remaining_work: bool = True


class InferredDependency(BaseModel):
    task_id: int
    depends_on_task_id: int
    reason: str


class ExecutionPlan(BaseModel):
    plan_version: int
    supersedes_plan_version: int | None = None
    planning_scope: PlanningScope
    global_goal: str
    execution_batches: list[ExecutionBatch] = Field(default_factory=list)
    checkpoints: list[CheckpointDefinition] = Field(default_factory=list)
    ready_task_ids: list[int] = Field(default_factory=list)
    blocked_task_ids: list[int] = Field(default_factory=list)
    inferred_dependencies: list[InferredDependency] = Field(default_factory=list)
    sequencing_rationale: str
    uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_plan(self):
        if self.plan_version < 1:
            raise ValueError("ExecutionPlan.plan_version must be >= 1.")

        if self.plan_version == 1:
            if self.supersedes_plan_version is not None:
                raise ValueError("ExecutionPlan.plan_version=1 cannot supersede a previous plan.")
        else:
            if self.supersedes_plan_version is None:
                raise ValueError(
                    "ExecutionPlan with plan_version > 1 must declare supersedes_plan_version."
                )
            if self.supersedes_plan_version != self.plan_version - 1:
                raise ValueError(
                    "ExecutionPlan.supersedes_plan_version must be exactly plan_version - 1."
                )

        if not self.execution_batches:
            raise ValueError("ExecutionPlan.execution_batches cannot be empty.")

        seen_batch_internal_ids: set[str] = set()
        seen_patch_keys: set[tuple[int, int]] = set()
        last_order_key: tuple[int, int] | None = None

        for batch in self.execution_batches:
            if batch.batch_internal_id in seen_batch_internal_ids:
                raise ValueError(
                    f"Duplicate batch_internal_id detected: '{batch.batch_internal_id}'."
                )
            seen_batch_internal_ids.add(batch.batch_internal_id)

            order_key = (
                batch.anchor_batch_index if batch.is_patch_batch else batch.batch_index,
                batch.patch_index if batch.is_patch_batch else 0,
            )

            if last_order_key is not None and order_key < last_order_key:
                raise ValueError(
                    "ExecutionPlan.execution_batches must be ordered by logical execution sequence."
                )
            last_order_key = order_key

            if batch.is_patch_batch:
                patch_key = (batch.anchor_batch_index, batch.patch_index)
                if patch_key in seen_patch_keys:
                    raise ValueError(
                        f"Duplicate patch batch detected for anchor_batch_index="
                        f"{batch.anchor_batch_index}, patch_index={batch.patch_index}."
                    )
                seen_patch_keys.add(patch_key)

        batch_ids = {batch.batch_id for batch in self.execution_batches}
        checkpoint_ids = {checkpoint.checkpoint_id for checkpoint in self.checkpoints}

        for batch in self.execution_batches:
            if batch.plan_version != self.plan_version:
                raise ValueError(
                    f"Batch '{batch.batch_id}' has plan_version={batch.plan_version}, "
                    f"but parent plan has plan_version={self.plan_version}."
                )

        for checkpoint in self.checkpoints:
            if checkpoint.after_batch_id not in batch_ids:
                raise ValueError(
                    f"Checkpoint '{checkpoint.checkpoint_id}' references unknown batch "
                    f"'{checkpoint.after_batch_id}'."
                )

        for batch in self.execution_batches:
            if batch.checkpoint_id not in checkpoint_ids:
                raise ValueError(
                    f"Batch '{batch.batch_id}' has checkpoint_id '{batch.checkpoint_id}' "
                    "but no matching CheckpointDefinition exists."
                )

        for checkpoint in self.checkpoints:
            batch = next(
                (
                    candidate
                    for candidate in self.execution_batches
                    if candidate.batch_id == checkpoint.after_batch_id
                ),
                None,
            )
            if batch is None:
                raise ValueError(
                    f"Checkpoint '{checkpoint.checkpoint_id}' references an unknown batch."
                )
            if batch.checkpoint_id != checkpoint.checkpoint_id:
                raise ValueError(
                    f"Checkpoint '{checkpoint.checkpoint_id}' is not aligned with batch "
                    f"'{batch.batch_id}'."
                )

        final_batch = self.execution_batches[-1]
        final_checkpoint = next(
            (
                checkpoint
                for checkpoint in self.checkpoints
                if checkpoint.checkpoint_id == final_batch.checkpoint_id
            ),
            None,
        )
        if final_checkpoint is None:
            raise ValueError("The final batch must have a valid final checkpoint.")
        if final_checkpoint.after_batch_id != final_batch.batch_id:
            raise ValueError("The final checkpoint must point to the final execution batch.")
        if "stage_closure" not in final_checkpoint.evaluation_focus:
            raise ValueError(
                "The final checkpoint must include 'stage_closure' in evaluation_focus."
            )

        return self


class ExecutionPlanGenerationInput(BaseModel):
    project_context: ProjectExecutionContext
    candidate_atomic_tasks: list[CandidateAtomicTask] = Field(default_factory=list)
    execution_state: ExecutionStateSummary
    instructions: ExecutionSequencingInstructions

    @model_validator(mode="after")
    def validate_input(self):
        if not self.candidate_atomic_tasks:
            raise ValueError("At least one candidate atomic task is required.")
        return self
