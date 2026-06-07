from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.execution_engine.contracts import (
    OBSERVATION_TYPE_IMAGE_GENERATED,
    ExecutionRequest,
    ProjectExecutionContext,
)
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import SubagentRejectedStepError
from app.execution_engine.subagents.image_generation_agent import (
    ImageGenerationAgent,
)
from app.services.image_providers.contracts import ImageGenerationResult


def _make_png_bytes() -> bytes:
    try:
        from PIL import Image

        img = Image.new("RGB", (1024, 1024), (100, 149, 237))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def _make_request(tmp_path: Path) -> ExecutionRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    return ExecutionRequest(
        task_id=1,
        project_id=1,
        execution_run_id=1,
        task_title="Generate application icon",
        task_description="Create a flat-style app icon for the project",
        executor_type="execution_engine",
        context=ProjectExecutionContext(
            project_id=1,
            source_path=str(source),
            workspace_path=str(workspace),
        ),
    )


def _make_step() -> ExecutionStep:
    return ExecutionStep(
        id="image_generation_agent_0",
        subagent_name="image_generation_agent",
        title="Generate icon",
        instructions="Generate the application icon",
    )


def _make_engineering_output(tmp_path: Path, **overrides) -> dict:
    base = {
        "main_prompt": "A flat-style minimal application icon with a stylized letter A",
        "negative_prompt": "text, photorealism, complex backgrounds",
        "style_directive": "flat icon",
        "design_rationale": "Flat style for clarity at small sizes. Blue for brand recognition.",
        "intended_colors": ["brand blue #2563EB", "white #FFFFFF"],
        "output_format": "png",
        "target_width": 128,
        "target_height": 128,
        "generation_width": 1024,
        "generation_height": 1024,
        "needs_resize": True,
        "resize_mode": "fit",
        "output_path": "assets/icons/app-icon.png",
        "resize_variants": [],
    }
    base.update(overrides)
    return base


def _make_gen_result(image_bytes: bytes) -> ImageGenerationResult:
    return ImageGenerationResult(
        image_bytes=image_bytes,
        model_used="dall-e-3",
        seed_used=None,
        actual_width=1024,
        actual_height=1024,
        generation_duration_ms=1500,
    )


# ── Happy path: no resize ────────────────────────────────────────────────────


def test_execute_step_no_resize(tmp_path: Path):
    runtime = MagicMock()
    image_bytes = _make_png_bytes()
    runtime.generate_structured.return_value = _make_engineering_output(
        tmp_path,
        needs_resize=False,
        target_width=1024,
        target_height=1024,
    )

    request = _make_request(tmp_path)
    step = _make_step()
    state = ResolutionState(execution_request=_make_request(tmp_path))

    with patch(
        "app.execution_engine.subagents.image_generation_agent.generate_image",
        return_value=_make_gen_result(image_bytes),
    ):
        agent = ImageGenerationAgent(runtime=runtime)
        result_state = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    output_path = Path(request.context.workspace_path) / "assets/icons/app-icon.png"
    assert output_path.exists()
    assert output_path.read_bytes() == image_bytes

    changed = [cf.path for cf in result_state.evidence.changed_files]
    assert "assets/icons/app-icon.png" in changed


# ── Happy path: with resize ──────────────────────────────────────────────────


def test_execute_step_with_resize(tmp_path: Path):
    runtime = MagicMock()
    image_bytes = _make_png_bytes()
    runtime.generate_structured.return_value = _make_engineering_output(tmp_path)

    request = _make_request(tmp_path)
    step = _make_step()
    state = ResolutionState(execution_request=_make_request(tmp_path))

    with patch(
        "app.execution_engine.subagents.image_generation_agent.generate_image",
        return_value=_make_gen_result(image_bytes),
    ):
        agent = ImageGenerationAgent(runtime=runtime)
        result_state = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    # Primary output + source original
    changed = [cf.path for cf in result_state.evidence.changed_files]
    assert "assets/icons/app-icon.png" in changed
    assert "assets/icons/app-icon_source.png" in changed

    # Workspace file exists
    assert (Path(request.context.workspace_path) / "assets/icons/app-icon.png").exists()


# ── Happy path: resize variants ──────────────────────────────────────────────


def test_execute_step_with_variants(tmp_path: Path):
    runtime = MagicMock()
    image_bytes = _make_png_bytes()
    variants = [
        {
            "output_path": "assets/icons/app-icon-64.png",
            "width": 64,
            "height": 64,
            "resize_mode": "fit",
        },
        {
            "output_path": "assets/icons/app-icon-32.png",
            "width": 32,
            "height": 32,
            "resize_mode": "fit",
        },
    ]
    runtime.generate_structured.return_value = _make_engineering_output(
        tmp_path, resize_variants=variants
    )

    request = _make_request(tmp_path)
    step = _make_step()
    state = ResolutionState(execution_request=_make_request(tmp_path))

    with patch(
        "app.execution_engine.subagents.image_generation_agent.generate_image",
        return_value=_make_gen_result(image_bytes),
    ):
        agent = ImageGenerationAgent(runtime=runtime)
        result_state = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    changed = [cf.path for cf in result_state.evidence.changed_files]
    assert "assets/icons/app-icon-64.png" in changed
    assert "assets/icons/app-icon-32.png" in changed


# ── Evidence: observation payload ────────────────────────────────────────────


def test_execute_step_adds_image_generated_observation(tmp_path: Path):
    runtime = MagicMock()
    image_bytes = _make_png_bytes()
    runtime.generate_structured.return_value = _make_engineering_output(
        tmp_path, needs_resize=False, target_width=1024, target_height=1024
    )

    request = _make_request(tmp_path)
    step = _make_step()
    state = ResolutionState(execution_request=_make_request(tmp_path))

    with patch(
        "app.execution_engine.subagents.image_generation_agent.generate_image",
        return_value=_make_gen_result(image_bytes),
    ):
        agent = ImageGenerationAgent(runtime=runtime)
        result_state = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    obs = [
        o
        for o in result_state.evidence.observations
        if o.evidence_type == OBSERVATION_TYPE_IMAGE_GENERATED
    ]
    assert len(obs) == 1
    payload = obs[0].payload
    assert "prompt_engineering" in payload
    assert "generation" in payload
    assert "output" in payload
    assert payload["prompt_engineering"]["style_directive"] == "flat icon"


# ── Error: wrong step name ────────────────────────────────────────────────────


def test_execute_step_rejects_wrong_subagent(tmp_path: Path):
    runtime = MagicMock()
    step = ExecutionStep(
        id="code_change_agent_0",
        subagent_name="code_change_agent",
        title="wrong",
        instructions="wrong",
    )
    request = _make_request(tmp_path)
    state = ResolutionState(execution_request=request)

    agent = ImageGenerationAgent(runtime=runtime)
    with pytest.raises(SubagentRejectedStepError):
        agent.execute_step(db=MagicMock(), request=request, step=step, state=state)


# ── Error: generation API failure ────────────────────────────────────────────


def test_execute_step_rejects_on_api_failure(tmp_path: Path):
    runtime = MagicMock()
    runtime.generate_structured.return_value = _make_engineering_output(tmp_path)

    request = _make_request(tmp_path)
    step = _make_step()
    state = ResolutionState(execution_request=request)

    with patch(
        "app.execution_engine.subagents.image_generation_agent.generate_image",
        side_effect=RuntimeError("API down"),
    ):
        agent = ImageGenerationAgent(runtime=runtime)
        with pytest.raises(SubagentRejectedStepError, match="API call failed"):
            agent.execute_step(db=MagicMock(), request=request, step=step, state=state)


# ── Evidence: file documentation ────────────────────────────────────────────


def test_execute_step_adds_file_documentation(tmp_path: Path):
    runtime = MagicMock()
    image_bytes = _make_png_bytes()
    runtime.generate_structured.return_value = _make_engineering_output(
        tmp_path, needs_resize=False, target_width=1024, target_height=1024
    )

    request = _make_request(tmp_path)
    step = _make_step()
    state = ResolutionState(execution_request=_make_request(tmp_path))

    with patch(
        "app.execution_engine.subagents.image_generation_agent.generate_image",
        return_value=_make_gen_result(image_bytes),
    ):
        agent = ImageGenerationAgent(runtime=runtime)
        result_state = agent.execute_step(db=MagicMock(), request=request, step=step, state=state)

    docs = {d.path: d for d in result_state.evidence.file_documentations}
    assert "assets/icons/app-icon.png" in docs
    doc = docs["assets/icons/app-icon.png"]
    assert "flat icon" in doc.documentation
    assert doc.agent == "image_generation_agent"
