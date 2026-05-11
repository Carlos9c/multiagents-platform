from __future__ import annotations

from pydantic import ValidationError

from app.schemas.environment import RuntimeEnvironmentPlanOutput, RuntimeSpecRepairOutput
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema

ENVIRONMENT_PLANNER_SYSTEM_PROMPT = """
You are a runtime environment planning agent.

Your job is to analyze a software project and its atomic tasks and decide the minimal,
sufficient execution environment needed to implement and verify all of them.

Return ONLY JSON matching the provided schema.

Core mission:
- Determine ONE primary runtime for the project (python_venv, node_npm, rust_cargo, or java_maven).
- Choose an appropriate official Docker image as the base.
- List all packages required, both for implementation and for running tests.
- Assign exact pinned versions to every package (format: x.y.z). No ranges. No "latest".

Runtime selection rules:
- Infer the runtime from the project description and task content.
- If the project is primarily Python (ML, data, web with Flask/FastAPI, scripting), use python_venv.
- If the project is primarily JavaScript/TypeScript (Node apps, React, Express), use node_npm.
- If the project is primarily Rust, use rust_cargo.
- If the project is primarily Java (Spring Boot, Spring MVC, Maven-based, JPA, Hibernate), use java_maven.
- If multiple languages are involved, choose the primary runtime based on what the core logic uses.

Docker image selection rules:
- For python_venv: use "python:3.12-slim" unless the project explicitly requires a different version.
- For node_npm: use "node:20-slim" unless the project explicitly requires a different version.
- For rust_cargo: use "rust:1.78-slim" unless the project explicitly requires a different version.
- For java_maven: use "public.ecr.aws/docker/library/maven:3.9-eclipse-temurin-21" unless the project explicitly requires a different Java version. This image is sourced from Amazon ECR Public (AWS CloudFront CDN) to avoid Cloudflare R2 connectivity issues.
- Always prefer slim variants for smaller image size.
- Only deviate from defaults when there is concrete evidence in the task descriptions.

Dependency selection rules:
- Include ONLY packages that are directly needed by the implementation or test tasks.
- Do not include packages that are already bundled with the runtime (e.g., stdlib, built-ins).
- If a task specifies a concrete library (e.g., "use XGBoost", "implement with FastAPI"), it is mandatory.
- If a task does not constrain the library, choose a widely-used, production-quality package.
- For testing: include the appropriate test runner (pytest for Python, jest for Node).
- For Python: include packages commonly needed alongside main dependencies (e.g., numpy if pandas is used).

Version pinning rules:
- ALWAYS provide exact versions in x.y.z format.
- Use recent stable releases, not bleeding-edge.
- Ensure mutual compatibility between selected package versions.
- If you are uncertain about an exact version, choose a known-stable recent release.

Constraints:
- Do not include packages unrelated to the project's tasks.
- Do not include duplicate packages.
- Return between 0 and 30 dependencies.
- The environment must be sufficient to complete ALL listed atomic tasks.

Self-check before responding:
- Have I covered all libraries explicitly mentioned in task technical_constraints?
- Have I included the test runner if any task has tests_required = true?
- Are all versions in exact x.y.z format?
- Is the image a slim official variant?
- Would this environment allow all tasks to be implemented and verified without further installations?
""".strip()


def _format_atomic_tasks(atomic_tasks: list[dict]) -> str:
    if not atomic_tasks:
        return "[No atomic tasks provided]"

    lines = []
    for i, task in enumerate(atomic_tasks, start=1):
        lines.append(f"Task {i}: {task.get('title', '')}")
        if task.get("proposed_solution"):
            lines.append(f"  proposed_solution: {task['proposed_solution']}")
        if task.get("technical_constraints"):
            lines.append(f"  technical_constraints: {task['technical_constraints']}")
        if task.get("tests_required"):
            lines.append(f"  tests_required: {task['tests_required']}")
    return "\n".join(lines)


def build_environment_planner_user_prompt(
    project_name: str,
    project_description: str,
    atomic_tasks: list[dict],
) -> str:
    formatted_tasks = _format_atomic_tasks(atomic_tasks)
    return f"""
Project name: {project_name}
Project description: {project_description}

Atomic tasks to support:
{formatted_tasks}

Planning instructions:
- Determine the runtime (python_venv, node_npm, rust_cargo, or java_maven) that can implement ALL tasks above.
- Choose an appropriate Docker image as the base.
- List every package required to implement and test all tasks.
- Pin every package to an exact version (x.y.z). No ranges.
- If any task explicitly names a library in its technical_constraints or proposed_solution, that library is mandatory.
- Include the test runner if any task has tests.
- Do not include packages already provided by the runtime standard library.
""".strip()


def build_environment_planner_retry_prompt(
    project_name: str,
    project_description: str,
    atomic_tasks: list[dict],
    validation_error: str,
) -> str:
    base = build_environment_planner_user_prompt(project_name, project_description, atomic_tasks)
    return f"""Your previous output was invalid.

Validation error:
{validation_error}

You must correct your output and return valid JSON matching the schema.

Key rules:
- runtime_type must be exactly "python_venv", "node_npm", "rust_cargo", or "java_maven".
- image must be a valid Docker image name (e.g. "python:3.12-slim").
- Every dependency version must be an exact x.y.z string.
- planning_rationale must not be empty.

{base}""".strip()


def build_environment_repair_user_prompt(
    spec_dict: dict,
    smoke_test_command: str,
    error_output: str,
) -> str:
    return f"""The current runtime environment failed its smoke test.

Current spec:
{spec_dict}

Smoke test command that was run:
{smoke_test_command}

Error output:
{error_output}

Repair instructions:
- Identify the root cause of the failure from the error output.
- Common causes: wrong package name, incompatible version combination, missing package, wrong image.
- Return a corrected dependency list and image.
- Keep changes minimal — only fix what is broken.
- All versions must remain exact x.y.z format.
- If the image needs to change, provide the corrected image.
""".strip()


ENVIRONMENT_REPAIR_SYSTEM_PROMPT = """
You are a runtime environment repair agent.

A smoke test for an execution environment has failed. Your job is to analyze the error
and produce a minimal corrected dependency list and Docker image that will pass the smoke test.

Return ONLY JSON matching the provided schema.

Repair rules:
- Fix only what is broken. Preserve working dependencies unchanged.
- If a package is not found: check for alternative distribution names or version availability.
- If there is a version conflict: adjust versions to a compatible combination.
- If the image is wrong: provide the correct official image.
- All versions must be exact x.y.z format.
- Do not introduce new unrelated packages.
""".strip()


def call_environment_planner(
    project_name: str,
    project_description: str,
    atomic_tasks: list[dict],
) -> RuntimeEnvironmentPlanOutput:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(RuntimeEnvironmentPlanOutput.model_json_schema())

    user_prompt = build_environment_planner_user_prompt(
        project_name=project_name,
        project_description=project_description,
        atomic_tasks=atomic_tasks,
    )

    raw = provider.generate_structured(
        system_prompt=ENVIRONMENT_PLANNER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="runtime_environment_plan",
        json_schema=strict_schema,
    )

    try:
        return RuntimeEnvironmentPlanOutput.model_validate(raw)
    except ValidationError as exc:
        retry_prompt = build_environment_planner_retry_prompt(
            project_name=project_name,
            project_description=project_description,
            atomic_tasks=atomic_tasks,
            validation_error=str(exc),
        )
        raw_retry = provider.generate_structured(
            system_prompt=ENVIRONMENT_PLANNER_SYSTEM_PROMPT,
            user_prompt=retry_prompt,
            schema_name="runtime_environment_plan",
            json_schema=strict_schema,
        )
        return RuntimeEnvironmentPlanOutput.model_validate(raw_retry)


def call_environment_repair(
    spec_dict: dict,
    smoke_test_command: str,
    error_output: str,
) -> RuntimeSpecRepairOutput:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(RuntimeSpecRepairOutput.model_json_schema())

    user_prompt = build_environment_repair_user_prompt(
        spec_dict=spec_dict,
        smoke_test_command=smoke_test_command,
        error_output=error_output,
    )

    raw = provider.generate_structured(
        system_prompt=ENVIRONMENT_REPAIR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="runtime_spec_repair",
        json_schema=strict_schema,
    )

    try:
        return RuntimeSpecRepairOutput.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Environment repair LLM returned invalid output: {exc}") from exc
