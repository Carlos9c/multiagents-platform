from app.services.execution_plan_service import persist_execution_plan


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
