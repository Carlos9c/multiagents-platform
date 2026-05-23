from __future__ import annotations

from pydantic import ValidationError

from app.schemas.environment import RuntimeEnvironmentPlanOutput, RuntimeSpecRepairOutput
from app.services.llm.factory import get_llm_provider

ENVIRONMENT_PLANNER_SYSTEM_PROMPT = """
You are a runtime environment planning agent.

Your job is to analyze a software project and its atomic tasks and decide the minimal,
sufficient execution environment needed to implement and verify all of them.

Return ONLY JSON matching the provided schema.

Core mission:
- Determine ONE primary runtime for the project.
- Valid runtime_type values: python_venv, node_npm, rust_cargo, java_maven, java_gradle, android_gradle, react_native, dotnet, go.
- Choose an appropriate official Docker image as the base.
- List all packages required, both for implementation and for running tests.
- Assign exact pinned versions to every package (format: x.y.z). No ranges. No "latest".
- For build-system-managed runtimes (android_gradle, java_gradle, java_maven, rust_cargo, dotnet, go, react_native): return an empty dependencies list — packages are declared in project files by the code agents and resolved by the build tool at build time.

Runtime selection rules:
- Infer the runtime from the project description and task content.
- python_venv: Python backends, ML, data science, scripting (FastAPI, Django, Flask, pandas, etc.)
- node_npm: JavaScript/TypeScript web projects (React, Next.js, Vue, Angular, Express, NestJS)
- rust_cargo: Rust applications, CLIs, libraries, WebAssembly
- java_maven: Java projects using Maven (Spring Boot/Maven, legacy Maven projects)
- java_gradle: Java or Kotlin JVM projects using Gradle but NOT Android (Spring Boot/Gradle, Kotlin JVM apps)
- android_gradle: Native Android apps in Kotlin or Java (Jetpack, Room, Retrofit, Compose)
- react_native: React Native and Expo projects that build Android APKs (requires Node + Android SDK)
- dotnet: C# and F# applications (ASP.NET Core, Blazor, console apps, class libraries)
- go: Go applications, APIs, CLIs, microservices
- iOS projects require macOS/Xcode and cannot be built in Docker; note this limitation in planning_rationale.
- If multiple languages are involved, choose the runtime that drives the primary build pipeline.

Docker image selection rules (only applies when no pre-selected base image is provided):
- python_venv: "python:3.12-slim"
- node_npm: "node:22-slim"
- rust_cargo: "rust:1-slim-bookworm"
- java_maven: "public.ecr.aws/docker/library/maven:3.9-eclipse-temurin-21"
- java_gradle: "eclipse-temurin:21-jdk-noble"
- android_gradle: "mingc/android-build-box:1.26.0"
- react_native: "mingc/android-build-box:1.26.0" (Node must be installed separately)
- dotnet: "mcr.microsoft.com/dotnet/sdk:8.0"
- go: "golang:1.22-bookworm"
- Always prefer slim/official variants. Only deviate when task descriptions provide concrete evidence.

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
    catalog_hint: str | None = None,
) -> str:
    formatted_tasks = _format_atomic_tasks(atomic_tasks)
    hint_section = ""
    if catalog_hint:
        hint_section = f"""
Pre-selected base image: {catalog_hint}
The runtime_type and image fields must match this pre-selected environment.
Choose dependencies that are compatible with and installable in this image.
"""
    return f"""
Project name: {project_name}
Project description: {project_description}
{hint_section}
Atomic tasks to support:
{formatted_tasks}

Planning instructions:
- Determine the runtime (python_venv, node_npm, rust_cargo, java_maven, java_gradle, android_gradle, react_native, dotnet, or go) that can implement ALL tasks above.
- Choose an appropriate Docker image as the base.
- List every package required to implement and test all tasks.
- Pin every package to an exact version (x.y.z). No ranges.
- If any task explicitly names a library in its technical_constraints or proposed_solution, that library is mandatory.
- Include the test runner if any task has tests.
- Do not include packages already provided by the runtime standard library.
- For build-system-managed runtimes (android_gradle, java_gradle, java_maven, rust_cargo, dotnet, go): return an empty dependencies list — packages are declared in project files (build.gradle, pom.xml, Cargo.toml, go.mod, .csproj) by the code agents.
""".strip()


def build_environment_planner_retry_prompt(
    project_name: str,
    project_description: str,
    atomic_tasks: list[dict],
    validation_error: str,
    catalog_hint: str | None = None,
) -> str:
    base = build_environment_planner_user_prompt(
        project_name, project_description, atomic_tasks, catalog_hint=catalog_hint
    )
    return f"""Your previous output was invalid.

Validation error:
{validation_error}

You must correct your output and return valid JSON matching the schema.

Key rules:
- runtime_type must be exactly one of: python_venv, node_npm, rust_cargo, java_maven, java_gradle, android_gradle, react_native, dotnet, go.
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
    catalog_hint: str | None = None,
) -> RuntimeEnvironmentPlanOutput:
    provider = get_llm_provider()
    user_prompt = build_environment_planner_user_prompt(
        project_name=project_name,
        project_description=project_description,
        atomic_tasks=atomic_tasks,
        catalog_hint=catalog_hint,
    )

    raw = provider.generate_structured(
        system_prompt=ENVIRONMENT_PLANNER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="runtime_environment_plan",
        json_schema=RuntimeEnvironmentPlanOutput.model_json_schema(),
    )

    try:
        return RuntimeEnvironmentPlanOutput.model_validate(raw)
    except ValidationError as exc:
        retry_prompt = build_environment_planner_retry_prompt(
            project_name=project_name,
            project_description=project_description,
            atomic_tasks=atomic_tasks,
            validation_error=str(exc),
            catalog_hint=catalog_hint,
        )
        raw_retry = provider.generate_structured(
            system_prompt=ENVIRONMENT_PLANNER_SYSTEM_PROMPT,
            user_prompt=retry_prompt,
            schema_name="runtime_environment_plan",
            json_schema=RuntimeEnvironmentPlanOutput.model_json_schema(),
        )
        return RuntimeEnvironmentPlanOutput.model_validate(raw_retry)


def call_environment_repair(
    spec_dict: dict,
    smoke_test_command: str,
    error_output: str,
) -> RuntimeSpecRepairOutput:
    provider = get_llm_provider()
    user_prompt = build_environment_repair_user_prompt(
        spec_dict=spec_dict,
        smoke_test_command=smoke_test_command,
        error_output=error_output,
    )

    raw = provider.generate_structured(
        system_prompt=ENVIRONMENT_REPAIR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="runtime_spec_repair",
        json_schema=RuntimeSpecRepairOutput.model_json_schema(),
    )

    try:
        return RuntimeSpecRepairOutput.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Environment repair LLM returned invalid output: {exc}") from exc
