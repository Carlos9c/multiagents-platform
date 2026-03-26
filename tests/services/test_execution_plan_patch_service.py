from app.services.execution_plan_patch_service import insert_patch_batch_after_batch


def test_insert_patch_batch_after_batch_creates_plan_local_patch(make_execution_plan):
    plan = make_execution_plan(
        plan_version=3,
        supersedes_plan_version=2,
        batches=[
            {
                "batch_id": "plan_3_batch_1",
                "batch_internal_id": "3_1",
                "batch_index": 1,
                "plan_version": 3,
                "task_ids": [10],
            },
            {
                "batch_id": "plan_3_batch_2",
                "batch_internal_id": "3_2",
                "batch_index": 2,
                "plan_version": 3,
                "task_ids": [20],
            },
        ],
    )

    patched = insert_patch_batch_after_batch(
        plan=plan,
        anchor_batch_id="plan_3_batch_1",
        task_ids=[101, 102],
        goal="Execute local recovery patch before continuing.",
        checkpoint_reason="Validate patch batch.",
    )

    assert len(patched.execution_batches) == 3

    patch_batch = patched.execution_batches[1]
    assert patch_batch.is_patch_batch is True
    assert patch_batch.anchor_batch_index == 1
    assert patch_batch.patch_index == 1
    assert patch_batch.batch_internal_id == "3_1_p1"
    assert patch_batch.batch_id == "plan_3_batch_1_patch_1"
    assert patch_batch.name == "Plan 3 · Batch 1.1"
    assert patch_batch.task_ids == [101, 102]

    assert patched.execution_batches[0].batch_internal_id == "3_1"
    assert patched.execution_batches[2].batch_internal_id == "3_2"