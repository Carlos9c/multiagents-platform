from unittest.mock import MagicMock

from app.services.execution_plan_service import (
    _DEFAULT_PHASE_ORDER,
    _build_candidate_atomic_task,
    _compute_ordering_hints,
    _compute_phase_orders,
    persist_execution_plan,
)

# ---------------------------------------------------------------------------
# _compute_ordering_hints — Fix 1: deterministic task ordering
# ---------------------------------------------------------------------------


def _make_task_stub(task_id: int, task_type: str) -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.task_type = task_type
    return t


def test_configuration_tasks_get_setup_first_hint():
    tasks = [
        _make_task_stub(1, "configuration"),
        _make_task_stub(2, "implementation"),
        _make_task_stub(3, "testing"),
    ]
    hints = _compute_ordering_hints(tasks)
    assert hints[1] == "setup_first"


def test_implementation_and_testing_tasks_get_depends_on_setup_when_setup_present():
    tasks = [
        _make_task_stub(1, "configuration"),
        _make_task_stub(2, "implementation"),
        _make_task_stub(3, "testing"),
    ]
    hints = _compute_ordering_hints(tasks)
    assert hints[2] == "depends_on_setup"
    assert hints[3] == "depends_on_setup"


def test_no_setup_first_tasks_leaves_implementation_as_standard():
    tasks = [
        _make_task_stub(1, "implementation"),
        _make_task_stub(2, "testing"),
        _make_task_stub(3, "documentation"),
    ]
    hints = _compute_ordering_hints(tasks)
    assert hints[1] == "standard"
    assert hints[2] == "standard"
    assert hints[3] == "standard"


def test_non_build_dependent_types_stay_standard_even_with_setup_present():
    tasks = [
        _make_task_stub(1, "configuration"),
        _make_task_stub(2, "documentation"),
        _make_task_stub(3, "design"),
    ]
    hints = _compute_ordering_hints(tasks)
    assert hints[1] == "setup_first"
    assert hints[2] == "standard"
    assert hints[3] == "standard"


def test_multiple_configuration_tasks_all_get_setup_first():
    tasks = [
        _make_task_stub(1, "configuration"),
        _make_task_stub(2, "configuration"),
        _make_task_stub(3, "implementation"),
    ]
    hints = _compute_ordering_hints(tasks)
    assert hints[1] == "setup_first"
    assert hints[2] == "setup_first"
    assert hints[3] == "depends_on_setup"


def test_persist_execution_plan_updates_project_plan_version(
    db_session,
    make_project,
    make_execution_plan,
):
    project = make_project(plan_version=1)

    plan_v1 = make_execution_plan(
        plan_version=1,
        supersedes_plan_version=None,
        batches=[
            {
                "task_ids": [1],
            }
        ],
    )

    artifact = persist_execution_plan(
        db=db_session,
        project_id=project.id,
        plan=plan_v1,
        created_by="pytest",
    )

    db_session.refresh(project)

    assert artifact is not None
    assert artifact.project_id == project.id
    assert artifact.artifact_type == "execution_plan"
    assert project.plan_version == 1
    assert '"plan_version":1' in artifact.content.replace(" ", "")
    assert '"batch_internal_id":"1_1"' in artifact.content.replace(" ", "")


def test_second_persisted_plan_must_increment_plan_version(
    db_session,
    make_project,
    make_execution_plan,
):
    project = make_project(plan_version=1)

    plan_v1 = make_execution_plan(
        plan_version=1,
        supersedes_plan_version=None,
        batches=[
            {"task_ids": [1]},
        ],
    )
    persist_execution_plan(
        db=db_session,
        project_id=project.id,
        plan=plan_v1,
        created_by="pytest",
    )

    plan_v2 = make_execution_plan(
        plan_version=2,
        supersedes_plan_version=1,
        batches=[
            {"task_ids": [2]},
        ],
    )
    artifact_v2 = persist_execution_plan(
        db=db_session,
        project_id=project.id,
        plan=plan_v2,
        created_by="pytest",
    )

    db_session.refresh(project)

    assert project.plan_version == 2
    assert artifact_v2.project_id == project.id
    assert artifact_v2.artifact_type == "execution_plan"
    assert '"plan_version":2' in artifact_v2.content.replace(" ", "")
    assert '"supersedes_plan_version":1' in artifact_v2.content.replace(" ", "")


def test_execution_plan_batches_have_stable_internal_identity_and_normalized_name(
    make_execution_plan,
):
    plan = make_execution_plan(
        plan_version=3,
        supersedes_plan_version=2,
        batches=[
            {"task_ids": [10]},
            {"task_ids": [20]},
        ],
    )

    first_batch = plan.execution_batches[0]
    second_batch = plan.execution_batches[1]

    assert first_batch.batch_internal_id == "3_1"
    assert second_batch.batch_internal_id == "3_2"
    assert first_batch.batch_index == 1
    assert second_batch.batch_index == 2
    assert first_batch.plan_version == 3
    assert second_batch.plan_version == 3
    assert first_batch.name == "Plan 3 · Batch 1"
    assert second_batch.name == "Plan 3 · Batch 2"


def test_batches_from_different_plan_versions_do_not_collide_in_internal_identity(
    make_execution_plan,
):
    plan_v1 = make_execution_plan(
        plan_version=1,
        supersedes_plan_version=None,
        batches=[
            {"task_ids": [1]},
        ],
    )
    plan_v2 = make_execution_plan(
        plan_version=2,
        supersedes_plan_version=1,
        batches=[
            {"task_ids": [1]},
        ],
    )

    batch_v1 = plan_v1.execution_batches[0]
    batch_v2 = plan_v2.execution_batches[0]

    assert batch_v1.batch_internal_id == "1_1"
    assert batch_v2.batch_internal_id == "2_1"
    assert batch_v1.batch_internal_id != batch_v2.batch_internal_id
    assert batch_v1.plan_version == 1
    assert batch_v2.plan_version == 2
    assert batch_v1.batch_index == 1
    assert batch_v2.batch_index == 1


# ---------------------------------------------------------------------------
# _compute_phase_orders — deterministic 4-phase classification
# ---------------------------------------------------------------------------


def test_phase_order_design_types():
    tasks = [
        _make_task_stub(1, "design"),
        _make_task_stub(2, "requirements"),
        _make_task_stub(3, "planning"),
    ]
    orders = _compute_phase_orders(tasks)
    assert orders[1] == 1
    assert orders[2] == 1
    assert orders[3] == 1


def test_phase_order_implementation_types():
    tasks = [
        _make_task_stub(1, "implementation"),
        _make_task_stub(2, "configuration"),
        _make_task_stub(3, "refactor"),
    ]
    orders = _compute_phase_orders(tasks)
    assert orders[1] == 2
    assert orders[2] == 2
    assert orders[3] == 2


def test_phase_order_testing_type():
    tasks = [_make_task_stub(1, "testing")]
    orders = _compute_phase_orders(tasks)
    assert orders[1] == 3


def test_phase_order_documentation_types():
    tasks = [
        _make_task_stub(1, "documentation"),
        _make_task_stub(2, "onboarding"),
        _make_task_stub(3, "review"),
    ]
    orders = _compute_phase_orders(tasks)
    assert orders[1] == 4
    assert orders[2] == 4
    assert orders[3] == 4


def test_phase_order_unknown_type_falls_back_to_default():
    tasks = [_make_task_stub(1, "unknown_future_type")]
    orders = _compute_phase_orders(tasks)
    assert orders[1] == _DEFAULT_PHASE_ORDER


def test_phase_order_none_task_type_falls_back_to_default():
    task = MagicMock()
    task.id = 1
    task.task_type = None
    orders = _compute_phase_orders([task])
    assert orders[1] == _DEFAULT_PHASE_ORDER


def test_phase_order_strictly_ordered_design_before_implementation_before_testing_before_docs():
    tasks = [
        _make_task_stub(1, "design"),
        _make_task_stub(2, "implementation"),
        _make_task_stub(3, "testing"),
        _make_task_stub(4, "documentation"),
    ]
    orders = _compute_phase_orders(tasks)
    assert orders[1] < orders[2] < orders[3] < orders[4]


def _make_atomic_task_stub(task_id: int, task_type: str) -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.task_type = task_type
    t.planning_level = "atomic"
    t.title = f"Task {task_id}"
    t.description = None
    t.summary = None
    t.objective = None
    t.priority = "medium"
    t.executor_type = "pending_engine_routing"
    t.status = "pending"
    t.parent_task_id = None
    t.parent_task = None
    t.implementation_steps = None
    t.acceptance_criteria = None
    t.estimated_complexity = None
    t.depends_on_task_titles = []
    t.tests_required = None
    t.technical_constraints = None
    t.out_of_scope = None
    return t


def test_build_candidate_atomic_task_propagates_phase_order():
    task = _make_atomic_task_stub(1, "design")
    candidate = _build_candidate_atomic_task(task, None, "standard", phase_order=1)
    assert candidate.phase_order == 1


def test_build_candidate_atomic_task_phase_order_defaults_to_default_constant():
    task = _make_atomic_task_stub(1, "implementation")
    candidate = _build_candidate_atomic_task(task, None)
    assert candidate.phase_order == _DEFAULT_PHASE_ORDER
