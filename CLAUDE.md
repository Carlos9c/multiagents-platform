# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Agente Desarrollador** is an autonomous task execution system: a multi-agent orchestration platform that executes atomic development tasks with structured validation, evidence tracking, and deterministic recovery. Built with FastAPI + SQLAlchemy + OpenAI structured outputs + Docker.

## Commands

**Setup:**
```bash
poetry install --no-root
```

**Run all tests:**
```bash
poetry run pytest -q
```

**Run integration tests (requires Docker):**
```bash
poetry run pytest -m integration -v
```

**Run a single test file:**
```bash
poetry run pytest tests/services/test_task_execution_service.py -v
```

**Run a single test:**
```bash
poetry run pytest tests/services/test_task_execution_service.py::test_name -v
```

**Lint and format:**
```bash
poetry run ruff check .          # lint
poetry run ruff check . --fix    # auto-fix
poetry run black .               # format
poetry run black --check .       # check format
poetry run mypy app/             # type check
```

**Pre-commit (runs ruff + black):**
```bash
pre-commit run --all-files
```

CI runs `ruff check .` + `black --check .` + `pytest -q` on Python 3.12.

## Architecture

### Execution Flow

```
API request → task_execution_service.execute_task_sync()
  → ExecutionRun created in DB
  → EnvironmentBootstrapper → Docker session (if needed)
  → OrchestratedExecutionEngine.execute()
      → Orchestrator loop (discovery phase → execution phase)
          → subagents execute in sequence
  → Multi-validator system judges the result
  → Artifact created (source of truth)
  → Task status updated in DB
```

### Orchestration Layer (`app/execution_engine/`)

The **orchestrator** (`orchestrator.py`) drives a decision loop over two fixed phases:

1. **Discovery phase** — `context_selection_agent` is always called first to pull historical context.
2. **Execution phase** — orchestrator decides `call_subagent(name) | finish | reject` on each step.

The orchestrator does **not** judge quality — it only coordinates operationally necessary work. It enforces:
- No consecutive calls to the same subagent
- Budget limits (`max_steps`, `max_agent_calls`, `max_repair_attempts`) from `LoopBudget`
- `ResolutionState` tracks phase, evidence, subagent call history, and decision history across the loop
- **Budget exhaustion** → result is `COMPLETED` (not `FAILED`) so accumulated work is forwarded to validation

**Three subagents** (`app/execution_engine/subagents/`):
- `context_selection_agent` — selects historical completed tasks for execution context
- `code_change_agent` — materializes code changes (create/modify files)
- `command_runner_agent` — runs and verifies shell commands, records evidence

Each subagent receives `(ExecutionRequest, ExecutionStep, ResolutionState)` and returns an updated `ResolutionState`.

### Runtime Environment System (`app/services/environment/`)

Docker-based execution environment. Key components:
- `planner.py` — calls LLM to produce `RuntimeEnvironmentPlanOutput` → `RuntimeSpec`
- `bootstrapper.py` — pulls image, starts container, installs deps, smoke test, LLM repair on failure
- `docker_driver.py` — Docker SDK wrapper; handles 409 Conflict on concurrent container removal
- `session_store.py` — persists active `EnvironmentSession` per project
- `contracts.py` — `RuntimeSpec`, `EnvironmentSession`, `EnvironmentCommandResult`

Schema note: `RuntimeEnvironmentPlanOutput.environment_variables` is `list[EnvVar]` (not `dict[str, str]`) to comply with OpenAI strict JSON schema requirements.

### Validation Layer (`app/services/validation/`)

Independent from the execution layer. Runs after the engine returns:

1. **Selection** (`selection.py`) — picks validators based on which subagents ran (1:1 mapping)
2. **Execution** — each validator independently judges `TaskValidationInput` (request + result)
3. **Aggregation** — merges results with priority: `failed > manual_review > partial > completed`

Two validators: `code_change_agent_validator` and `command_runner_agent_validator`.

### Key Contracts (`app/execution_engine/contracts.py`)

- **`ExecutionRequest`** — input to the engine: task metadata, `ProjectExecutionContext` (source/workspace paths, relevant files, decisions), `HistoricalExecutionContext`
- **`ExecutionResult`** — output: `decision` (completed|partial|failed|rejected), `evidence` (`ExecutionEvidence`: changed_files, commands, notes), `execution_agent_sequence`
- **`ResolutionState`** — internal mutable state threaded through the orchestrator loop

### Evaluation Schema (`app/schemas/evaluation.py`)

`StageEvaluationOutput` derives `recommended_next_action`, `plan_change_scope`, and `remaining_plan_still_valid` deterministically from source-of-truth fields in `validate_output()`. The LLM cannot produce contradictory combinations — they are silently corrected. Do not add cross-field validation that conflicts with this derivation logic.

### OpenAI Strict JSON Schema (`app/services/llm/schema_utils.py`)

`to_openai_strict_json_schema()` converts Pydantic schemas for OpenAI Structured Outputs:
- Strips `"default"` keys (unsupported by OpenAI)
- Sets `additionalProperties: false` on all objects
- Makes all defined properties `required`

**Important:** do not add `"title"` to `_OPENAI_UNSUPPORTED_KEYS`. The walker runs on all dict nodes including the `properties` dict itself, so stripping `"title"` removes field keys named `title` from the schema.

**Important:** avoid `dict[str, str]` fields in LLM output schemas — OpenAI strict mode requires all object keys to be known at schema definition time. Use `list[SomeModel]` with explicit key/value fields instead (e.g. `list[EnvVar]`).

### Workspace Runtime (`app/services/local_workspace_runtime.py`)

Three-layer storage model:
- `source_dir` — persisted canonical project tree
- `workspace_dir` — editable overlay for the current run (not pre-hydrated)
- `run_dir` — ephemeral tree materialized from source + workspace for command execution, always removed after the run

Promotion merges the workspace overlay onto source. `ProjectStorageService` manages the directory structure under `AGENTS_PROJECTS_ROOT/{project_id}/`.

### Database Layer

SQLAlchemy 2.0 with mapped columns. Four core models in `app/models/`:
- `Project` — execution context container
- `Task` — atomic unit with `status`, `executor_type`, `planning_level`, `parent_task_id`
- `ExecutionRun` — per-attempt record tracking status, `failure_type`, recovery actions, evidence snapshots
- `Artifact` — immutable output created after successful validation (one per completed run)

Session-per-request via FastAPI dependency injection (`app/api/deps.py`). Migrations via Alembic (`alembic/`).

### LLM Integration (`app/services/llm/`)

All LLM calls use **OpenAI structured outputs** (JSON schema constrained responses). `BaseLLMProvider` interface with `openai_provider.py` implementation. The provider retries on `APITimeoutError` and `InternalServerError` (up to 2 retries, 2s/4s waits). Model defaults to `gpt-5.1`; configured via `OPENAI_MODEL` env var.

## Configuration

Loaded from `.env` via Pydantic Settings (`app/core/config.py`). Required variables:

```
DATABASE_URL=postgresql+psycopg2://...
OPENAI_API_KEY=...
AGENTS_PROJECTS_ROOT=...   # root dir for project workspaces
```

Key optional variables:
```
OPENAI_MODEL=gpt-5.1
EXECUTION_ENGINE_BACKEND=orchestrated
EXECUTION_ENGINE_MAX_STEPS=8
EXECUTION_ENGINE_MAX_AGENT_CALLS=8
EXECUTION_ENGINE_MAX_TOOL_CALLS=12
EXECUTION_ENGINE_MAX_COMMAND_RUNS=4
EXECUTION_ENGINE_MAX_REPAIR_ATTEMPTS=2
```

Note: Redis is **not used** — the `REDIS_URL` variable has been removed.

## Testing Conventions

Tests use SQLite in-memory (`:memory:`) via pytest fixtures in `tests/conftest.py`. Key fixtures: `db_session`, `make_project`, `make_task`, `make_execution_run`. The conftest sets `DATABASE_URL=sqlite+pysqlite:///:memory:` and `AGENTS_PROJECTS_ROOT=.pytest_agents_projects`. The execution engine is typically monkeypatched in service tests.

Integration tests (`tests/integration/`) require a live Docker daemon and are skipped by default. Run with `poetry run pytest -m integration`.
