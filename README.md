# Agente Desarrollador

Sistema de orquestación multi-agente para la ejecución autónoma de tareas de desarrollo. Ejecuta tareas atómicas con validación estructurada, trazabilidad de evidencia y recuperación determinista.

**Stack:** FastAPI · SQLAlchemy 2.0 · PostgreSQL · OpenAI structured outputs · Redis

---

## Pipeline completo

```
ProjectWorkflowService.run_workflow()
  │
  ├── [1] Planner          → high_level tasks
  ├── [2] TechnicalRefiner → refined tasks        (opcional, toggle por proyecto)
  ├── [3] AtomicGenerator  → atomic tasks
  ├── [4] ExecutionPlan    → batches + checkpoints
  │
  └── [iteración por batch]
        ├── [5] TaskExecutionService × N tasks
        │     ├── ExecutionRun creado
        │     ├── OrchestratedExecutionEngine
        │     │     → Orchestrator loop (discovery → execution)
        │     │         → context_selection_agent
        │     │         → code_change_agent
        │     │         → command_runner_agent
        │     ├── Multi-validator → ValidationResult
        │     ├── Artifact creado
        │     ├── Task status actualizado
        │     └── Hierarchy reconciliation
        │
        └── [6] PostBatchService
              ├── EvaluationService     → StageEvaluationOutput
              ├── RecoveryService       → RecoveryDecision × tarea fallida
              ├── PostBatchDecisionSvc  → ResolvedPostBatchIntent
              └── LivePlanMutationSvc   → patched ExecutionPlan | replan | none
```

---

## Descomposición de tareas en tres niveles

### Nivel 1 — Planner (`app/services/planner.py`)

Convierte la descripción del proyecto en tareas de alto nivel (`planning_level="high_level"`). Llama al LLM con structured output y valida calidad antes de persistir:

- Bloquea títulos vagos: "crear backend", "implementar sistema", "configurar proyecto", etc.
- Requiere campos mínimos: `description`, `objective`, `acceptance_criteria`
- Produce objetos `Task` con `executor_type="pending_engine_routing"`

### Nivel 2 — Technical Task Refiner (`app/services/technical_task_refiner.py`)

Opcional (controlado por `Project.enable_technical_refinement`). Refina cada tarea `high_level` en una versión `refined` con:

- `proposed_solution` — enfoque técnico concreto
- `implementation_steps` — pasos ordenados
- `tests_required` — criterios de verificación
- `risk_level` — evaluación de riesgo

### Nivel 3 — Atomic Task Generator (`app/services/atomic_task_generator.py`)

Descompone tareas `high_level` o `refined` en tareas atómicas ejecutables:

- Límites: `MAX_ATOMIC_TASKS_PER_PARENT = 8`, `MAX_IMPLEMENTATION_STEPS_PER_ATOMIC = 20`
- Valida longitud mínima de campos: `implementation_notes` ≥ 60 chars, `acceptance_criteria` ≥ 30 chars
- Los padres válidos tienen `planning_level ∈ {high_level, refined}`
- Produce tareas con `planning_level="atomic"`, `executor_type="pending_engine_routing"`

### Sequencing — Execution Plan Service (`app/services/execution_plan_service.py`)

Construye un `ExecutionPlan` con batches secuenciados a partir de las tareas atómicas:

1. Construye `ProjectExecutionContext` (goal, summary, paths, estado actual)
2. Proyecta las tareas en `CandidateAtomicTask` con sus metadatos
3. Llama al LLM (`call_execution_sequencer_model`) para obtener el plan
4. Persiste el plan como artifact de tipo `execution_plan`

**Tipos clave en `app/schemas/execution_plan.py`:**

| Tipo | Descripción |
|---|---|
| `ExecutionPlan` | Plan completo: `execution_batches`, `checkpoints`, `ready_task_ids`, `blocked_task_ids`, `inferred_dependencies` |
| `ExecutionBatch` | Grupo de tareas: `batch_id`, `batch_index`, `task_ids`, `checkpoint_id`, `is_patch_batch`, `anchor_batch_index`, `patch_index` |
| `CheckpointDefinition` | Punto de evaluación: `checkpoint_id`, `after_batch_id`, `evaluation_focus` (lista de `CheckpointEvaluationFocus`) |
| `CheckpointEvaluationFocus` | Literal: `architecture_alignment`, `functional_coverage`, `artifact_consistency`, `task_completion_quality`, `dependency_validation`, `risk_control`, `stage_closure` |
| `ProjectExecutionContext` | Contexto del proyecto inyectado en el plan y en el LLM |
| `CandidateAtomicTask` | Snapshot de tarea atómica como input al secuenciador |
| `ExecutionStateSummary` | Estado de ejecución actual: tareas completadas, pendientes, fallidas |

---

## Execution Engine

### Orchestrator (`app/execution_engine/orchestrator.py`)

Loop de decisión en dos fases fijas:

**Phase 1 — Discovery**
- `context_selection_agent` siempre se llama primero
- Selecciona tareas históricas completadas como contexto de referencia

**Phase 2 — Execution**
- Orquestador decide en cada step: `call_subagent | finish | reject | invalid`
- `invalid` = error del LLM; consume budget pero no rompe el flujo
- `reject` = salida válida (tarea rechazada estructuralmente)
- `finish` requiere evidencia acumulada para ser válido

**Presupuesto (`app/execution_engine/budget.py`):**

```python
@dataclass
class LoopBudget:
    max_steps: int            # pasos totales del loop
    max_agent_calls: int      # llamadas a subagentes
    max_repair_attempts: int  # intentos de reparación tras fallo
```

**Estado interno (`app/execution_engine/resolution_state.py`):**

`ResolutionState` mantiene a través del loop:
- `phase` — discovery | execution
- `evidence` — `ExecutionEvidence` acumulado (changed_files, commands, notes)
- `subagent_call_history` — secuencia de subagentes llamados
- `decision_history` — historial de decisiones del orquestador

**Contratos (`app/execution_engine/contracts.py`):**

| Tipo | Descripción |
|---|---|
| `ExecutionRequest` | Input al engine: task metadata, `ProjectExecutionContext`, `HistoricalExecutionContext` |
| `ExecutionResult` | Output: `decision` (completed\|partial\|failed\|rejected), `evidence`, `execution_agent_sequence` |
| `ExecutionEvidence` | Evidencia estructurada: `changed_files`, `commands_run`, `notes` |

**Excepciones (`app/execution_engine/base.py`):**

| Excepción | Significado |
|---|---|
| `ExecutionEngineError` | Error no recuperable |
| `ExecutionEngineTransientError` | Error transitorio (reintentable) |
| `ExecutionEngineRejectedError` | Tarea rechazada por el engine |

### Subagentes

| Subagente | Rol |
|---|---|
| `context_selection_agent` | Selecciona tareas históricas completadas relevantes como contexto |
| `code_change_agent` | Materializa cambios de código (create/modify files) |
| `command_runner_agent` | Ejecuta y verifica comandos shell en dos fases: selección de archivos → planificación del comando |

Cada subagente recibe `(ExecutionRequest, ExecutionStep, ResolutionState)` y retorna un `ResolutionState` actualizado.

---

## Validation Layer (`app/services/validation/`)

Independiente del engine. Corre después de que el engine retorna.

**Contratos (`app/services/validation/contracts.py`):**

| Tipo | Descripción |
|---|---|
| `TaskValidationInput` | Input unificado: `ExecutionRequest` + `ExecutionResult` + metadata de la tarea |
| `ValidationResult` | Output de un validador: `status`, `notes`, `confidence` |
| `ValidationStatus` | Literal: `completed`, `partial`, `failed`, `manual_review` |
| `ValidationServiceResult` | Output agregado: `final_status`, `results_by_validator`, `notes` |

**Flujo:**

1. **Selección** — elige validadores según qué subagentes corrieron (mapping 1:1)
2. **Ejecución** — cada validador evalúa `TaskValidationInput` de forma independiente
3. **Agregación** — merges con prioridad: `failed > manual_review > partial > completed`

Validadores activos: `code_change_agent_validator`, `command_runner_agent_validator`.

---

## Task Execution Service (`app/services/task_execution_service.py`)

Orquesta el ciclo completo por tarea:

1. Crea `ExecutionRun` en DB
2. Llama al engine → `ExecutionResult`
3. Crea `TaskValidationInput` y corre validation service
4. Persiste `Artifact` (fuente de verdad)
5. Actualiza status de la tarea
6. Promueve workspace → source si completada
7. Llama a `reconcile_task_hierarchy_after_changes`

**Servicios auxiliares:**

- `app/services/tasks.py` — helpers: `mark_task_running()`, `mark_task_awaiting_validation()`, `mark_task_completed()`, `mark_task_failed()`, `mark_task_partial()`
- `app/services/execution_runs.py` — helpers: `create_execution_run()`, `mark_execution_run_started()`, `mark_execution_run_succeeded()`, `mark_execution_run_failed()`

---

## Post-Batch Pipeline (`app/services/post_batch_service.py`)

Se ejecuta tras completar todos los tasks de un batch.

### 1. Evaluation Service (`app/services/evaluation_service.py`)

Llama al LLM (`call_stage_evaluation_model`) para evaluar el outcome del batch.

**Tipos clave en `app/schemas/evaluation.py`:**

| Tipo | Valores |
|---|---|
| `StageEvaluationDecision` | `stage_completed`, `stage_incomplete`, `manual_review_required` |
| `BatchOutcome` | `successful`, `partial`, `failed`, `blocked` |
| `PlanChangeScope` | `none`, `local_resequence`, `full_replan` |
| `RecommendedNextAction` | `continue`, `resequence`, `replan`, `close`, `manual_review` |
| `RecoveryStrategy` | Estrategia de recovery sugerida por el evaluador |

`StageEvaluationOutput` incluye: `decision`, `batch_outcome`, `plan_change_scope`, `recommended_next_action`, `recovery_strategy`, `new_recovery_tasks_blocking`, `notes`.

### 2. Recovery Service (`app/services/recovery_service.py`)

Materializa las decisiones de recovery sobre tareas fallidas/parciales.

**`RecoveryDecision` (`app/schemas/recovery.py`):**

| Campo | Descripción |
|---|---|
| `action` | `reatomize`, `insert_followup`, `manual_review` |
| `confidence` | `low`, `medium`, `high` |
| `still_blocks_progress` | Si el gap bloquea el avance del plan |
| `created_tasks` | Lista de `RecoveryTaskCreate` para materializar |
| `evaluation_guidance` | Hint para el evaluador del siguiente batch |

**Efectos por acción:**

| Acción | Status tarea fuente | `is_recovery_task` | Notas |
|---|---|---|---|
| `reatomize` | `reatomized` | `True` | Guard anti-cascada: no se puede reatomizar una tarea `is_recovery_task=True` |
| `insert_followup` | `followed_up` | `False` | Tarea fuente marcada terminal; scope residual delegado |
| `manual_review` | sin cambio (`partial`) | — | No crea tareas |

**Guards de recovery (`materialize_recovery_decision`):**

| Guard | Condición de disparo | Efecto |
|---|---|---|
| `recovery_anti_cascade` | `is_recovery_task=True` + acción `reatomize` | Fuerza `manual_review` |
| `recovery_followup_depth_cap` | `followup_depth >= 2` + acción `insert_followup` | Fuerza `manual_review` |
| `recovery_repeated_failure_cap` | ≥ 1 sibling recovery ya fallado/partial + acción `insert_followup` | Fuerza `manual_review` |

**`RecoveryContext`** acumula a través del pipeline: `recovery_decisions`, `recovery_created_tasks`, `open_issues`.

### 3. Post-Batch Decision Service (`app/services/post_batch_decision_service.py`)

Traduce las señales del evaluador y el contexto de recovery en un `ResolvedPostBatchIntent` canónico.

**`ResolvedPostBatchIntent` (`app/schemas/post_batch_intent.py`):**

| Campo | Tipo | Descripción |
|---|---|---|
| `intent_type` | `continue\|assign\|resequence\|replan\|manual_review\|close` | Acción a tomar |
| `mutation_scope` | `none\|assignment\|resequence\|replan` | Alcance de la mutación del plan |
| `remaining_plan_still_valid` | `bool` | Si el plan restante sigue siendo válido |
| `has_new_recovery_tasks` | `bool` | Si se crearon tareas de recovery |
| `requires_plan_mutation` | `bool` | Si el plan debe ser mutado |
| `requires_all_new_tasks_assigned` | `bool` | Si todas las tareas nuevas deben quedar asignadas |
| `can_continue_after_application` | `bool` | Si se puede continuar tras la mutación |
| `should_close_stage` | `bool` | Si el stage debe cerrarse |
| `reopened_finalization` | `bool` | Si se reabrió la finalización |
| `decision_signals` | `list[str]` | Señales que llevaron a esta decisión |

### 4. Recovery Assignment Compiler (`app/services/recovery_assignment_compiler_service.py`)

Cuando `intent_type="assign"`, compila una propuesta de asignación de clusters en el plan activo.

Llama al LLM (`call_recovery_assignment_model`) para obtener `RecoveryAssignmentOutput` y lo compila en `CompiledClusterAssignment`:

**Tipos clave en `app/schemas/recovery_assignment.py`:**

| Tipo | Valores |
|---|---|
| `AssignmentImpactType` | `isolated_gap`, `sequential_dependency`, `parallel_opportunity`, `blocking_dependency`, `optional_enhancement` |
| `AssignmentPlacementRelation` | `before_next_useful_progress`, `before_first_consumer_batch`, `after_anchor_batch`, `end_of_plan` |
| `BatchAssignmentMode` | `new_patch_batch`, `attach_to_existing_batch` |
| `IntrabatchPlacementMode` | `prepend`, `append`, `after_anchor_task`, `before_anchor_task` |

Si el compiler no puede asignar todos los clusters, emite `requires_replan=True` y el pipeline escala a replan.

### 5. Live Plan Mutation Service (`app/services/live_plan_mutation_service.py`)

Aplica la mutación final al plan basándose en el intent resuelto.

**`LivePlanMutationKind`:**

| Kind | Cuándo |
|---|---|
| `none` | Intent `continue`, `manual_review` o `close` |
| `assignment` | Tareas de recovery asignadas via compiler |
| `resequence_patch` | Patch batch insertado inmediatamente tras el anchor |
| `resequence_deferred` | Resequence sin recovery tasks — no se produce patch |
| `escalated_to_replan` | Plan completo inválido; se requiere replanning |

**Lógica de escalado defensivo:** si `resequence_deferred` llega con recovery tasks pendientes de asignar (`requires_all_new_tasks_assigned=True`), el sistema escala automáticamente a un intent `assign` en lugar de crashear. Previene la pérdida permanente de tareas de recovery.

---

## Execution Plan Patch Service (`app/services/execution_plan_patch_service.py`)

Inserta patch batches en el plan activo sin replanificar.

- `insert_patch_batch_after_batch()` — inserta un batch de recovery tras el anchor batch; usa `model_construct` para el plan provisional (evita validación Pydantic prematura durante la inserción)
- `normalize_execution_plan_terminal_invariants()` — garantiza `stage_closure` en el checkpoint final, reindexación correcta de batches, y unicidad de checkpoint IDs

**Invariante:** el checkpoint del batch final siempre incluye `stage_closure` en `evaluation_focus`.

---

## Project Workflow Service (`app/services/project_workflow_service.py`)

Orquesta el pipeline completo de un proyecto, iteración por iteración.

**Flujo de iteraciones:**

```
while batches pendientes:
    → ejecutar tasks del batch actual
    → post-batch pipeline
    → según intent resuelto:
        continue         → siguiente batch
        assign           → plan mutado, siguiente batch
        resequence       → plan parcheado, siguiente batch
        replan           → nuevo ExecutionPlan, reiniciar
        manual_review    → detener, esperar intervención
        close            → finalizar stage
```

**Guards:**

- **Plan exhaustion:** si no quedan batches pero el workflow no cerró, se detecta como agotamiento del plan y se escala
- **Finalization reopening:** si un batch de cierre produce recovery, la finalización se reabre con `reopened_finalization=True`
- **Iteration limit:** límite configurable de iteraciones por proyecto para prevenir loops infinitos

**Traza de iteraciones (`app/schemas/workflow.py`):**

| Tipo | Descripción |
|---|---|
| `WorkflowIterationSummary` | Resumen de una iteración: batch_id, intent, mutation_kind, decision_signals |
| `ProjectWorkflowResult` | Resultado final con todas las `WorkflowIterationSummary` y el estado del plan |
| `WorkflowIterationTrace` | Artefacto persistido por batch con el detalle de cada iteración |

---

## Project Memory Service (`app/services/project_memory_service.py`)

Construye el `ProjectOperationalContext` que se inyecta como contexto a los LLM calls.

**`app/schemas/project_memory.py`:**

| Tipo | Descripción |
|---|---|
| `ProjectOperationalContext` | Snapshot completo del estado del proyecto para el LLM |
| `ProjectMemoryTaskSummary` | Resumen de una tarea completada: title, outcome, evidence snapshot |
| `ProjectMemoryDecisionSignal` | Señal de decisión registrada: signal_type, batch_id, notes |
| `ProjectMemoryPathSignal` | Señal de path: rutas creadas/modificadas con su propósito inferido |

Este contexto alimenta a `context_selection_agent` y como parte del `ExecutionRequest`.

---

## Workspace Runtime (`app/services/local_workspace_runtime.py`)

Modelo de almacenamiento en tres capas:

```
project/
├── source/          # árbol canónico persistido
├── executions/<run_id>/
│   ├── workspace/   # overlay editable del run actual
│   └── run/         # árbol efímero (source + workspace fusionados); eliminado tras el run
```

La promoción fusiona el overlay workspace sobre source. El directorio `run` siempre se elimina al finalizar, independientemente del resultado.

**Project Storage Service (`app/services/project_storage.py`):**

Gestiona el layout de directorios bajo `AGENTS_PROJECTS_ROOT/{project_id}/`:

```
{project_id}/
├── source/          # código canónico del proyecto
├── artifacts/       # artifacts JSON por run
├── executions/      # workspaces por run
└── domain_data/     # datos de dominio del proyecto (input externo)
```

---

## LLM Integration (`app/services/llm/`)

Todos los llamados LLM usan **OpenAI structured outputs** (respuestas constreñidas por JSON schema estricto).

| Archivo | Rol |
|---|---|
| `base.py` | Interfaz abstracta `BaseLLMProvider` |
| `openai_provider.py` | Implementación con structured outputs via `response_format` |
| `factory.py` | Retorna el provider configurado según env vars |
| `schema_utils.py` | Convierte schemas Pydantic a JSON schema estricto compatible con OpenAI |

Modelo por defecto: `gpt-5.1` (configurable via `OPENAI_MODEL`).

Cada servicio que llama al LLM tiene su propio **client** (`*_client.py`) que encapsula el schema de input/output y la llamada al provider.

---

## Modelos de datos

### Project

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | int | PK |
| `name` | str | Nombre del proyecto |
| `description` | str | Descripción del goal |
| `enable_technical_refinement` | bool | Activa la fase de refinamiento técnico (nivel intermedio) |
| `plan_version` | int | Versión actual del plan de ejecución |

### Task

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | int | PK |
| `project_id` | int | FK a Project |
| `parent_task_id` | int \| None | FK a Task padre |
| `title` | str | Título de la tarea |
| `planning_level` | str | `high_level`, `refined`, `atomic` |
| `executor_type` | str | `pending_engine_routing`, etc. |
| `status` | str | Ver tabla de statuses |
| `is_recovery_task` | bool | Si fue creada por recovery (bloquea reatomize en cascada) |
| `followup_depth` | int | Profundidad en la cadena de followup (0 = tarea original) |
| `is_blocked` | bool | Bloqueo manual |
| `blocking_reason` | str \| None | Razón del bloqueo |
| `sequence_order` | int \| None | Orden de secuencia dentro del padre |
| `implementation_steps` | list | Pasos de implementación |
| `tests_required` | list | Criterios de verificación |
| `acceptance_criteria` | str | Criterios de aceptación |

### ExecutionRun

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | int | PK |
| `task_id` | int | FK a Task |
| `status` | str | `pending`, `running`, `succeeded`, `failed`, `partial` |
| `failure_type` | str \| None | Clasificación del fallo: `execution_error`, `validation_failed`, etc. |
| `failure_code` | str \| None | Código programático del fallo |
| `validation_notes` | str \| None | Feedback del validador |
| `completed_scope` | str \| None | Alcance completado (para runs parciales) |
| `remaining_scope` | str \| None | Alcance pendiente (para runs parciales) |

### Artifact

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | int | PK |
| `project_id` | int | FK a Project |
| `task_id` | int \| None | FK a Task (null para artifacts de plan) |
| `artifact_type` | str | Ver tabla de tipos |
| `content` | str | JSON serializado |
| `created_by` | str | Servicio creador |

**Statuses de tarea:**

```
pending → running → awaiting_validation → completed
                                        → partial       → (recovery)
                                        → failed        → (recovery)
                  → reatomized   (terminal: recovery con reatomize)
                  → followed_up  (terminal: recovery con insert_followup)
```

`TERMINAL_TASK_STATUSES = {partial, completed, failed, reatomized, followed_up}`

---

## Task Hierarchy Service (`app/services/task_hierarchy_service.py`)

Propagación determinista del status de padres a partir del status de sus hijos.

**Reglas de consolidación:**

| Condición en hijos | Status del padre |
|---|---|
| Todos ∈ `{completed, reatomized, followed_up}` | `completed` |
| Alguno en `failed`, sin hijos `pending` ni `partial` | `failed` |
| Alguno en `partial` | `partial` |
| Sin condición de cierre | sin cambio |

**Task Hierarchy Reconciliation Service (`app/services/task_hierarchy_reconciliation_service.py`):**

Dado un conjunto de `affected_task_ids` (hijos modificados), recolecta los IDs de padres únicos y propaga el status hacia arriba. Usa `db.rollback()` si falla algún paso para garantizar atomicidad.

---

## Tipos de artifacts

| Tipo | Creador | Descripción |
|---|---|---|
| `project_plan` | `planner` | Output del planner: lista de high-level tasks |
| `technical_refinement` | `technical_task_refiner` | Output del refinamiento técnico |
| `atomic_task_generation` | `atomic_task_generator` | Output de la generación atómica |
| `execution_plan` | `execution_plan_service` | Plan de ejecución secuenciado |
| `execution_plan_patch` | `execution_plan_patch_service` | Plan mutado con patch batch |
| `execution_engine_context` | engine | Contexto LLM enviado al engine |
| `execution_engine_result` | engine | Output crudo del engine |
| `validation_result` | validation service | Resultado de validación por validador y agregado |
| `recovery_decision` | `recovery_service` | Decisión de recovery tomada por tarea |
| `recovery_assignment_input` | `live_plan_mutation_service` | Input al assignment compiler |
| `recovery_assignment_output` | `live_plan_mutation_service` | Output del assignment LLM |
| `recovery_assignment_compiled_plan` | `live_plan_mutation_service` | Plan compilado de asignación |
| `evaluation_decision` | `evaluation_service` | Output del evaluador de stage |
| `post_batch_result` | `post_batch_service` | Resultado completo del post-batch pipeline |
| `workflow_batch_trace` | `project_workflow_service` | Traza de iteración por batch |

---

## API (`app/api/`)

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/workflow/projects/{project_id}/run` | Ejecutar el workflow completo del proyecto |
| `POST` | `/projects/{project_id}/plan` | Ejecutar solo la fase de planning |
| `POST` | `/projects/{project_id}/technical_task_refiner` | Refinar una tarea high-level |
| `POST` | `/projects/{project_id}/atomic_task_generator` | Generar tareas atómicas para un padre |
| `POST` | `/tasks/{task_id}/execute` | Ejecutar una tarea atómica individualmente |
| `GET` | `/projects` | Listar proyectos |
| `GET` | `/projects/{project_id}` | Detalle de proyecto |
| `GET` | `/projects/{project_id}/tasks` | Tareas del proyecto |
| `GET` | `/projects/{project_id}/artifacts` | Artifacts del proyecto |
| `GET` | `/projects/{project_id}/execution_runs` | Runs de ejecución del proyecto |

Sesión por request via FastAPI dependency injection (`app/api/deps.py`). Migrations via Alembic (`alembic/`).

---

## Configuración

Variables requeridas en `.env`:

```
DATABASE_URL=postgresql+psycopg2://...
REDIS_URL=redis://...
OPENAI_API_KEY=...
AGENTS_PROJECTS_ROOT=...
```

Variables opcionales clave:

```
OPENAI_MODEL=gpt-5.1
EXECUTION_ENGINE_BACKEND=orchestrated
EXECUTION_ENGINE_MAX_STEPS=8
EXECUTION_ENGINE_MAX_AGENT_CALLS=6
EXECUTION_ENGINE_MAX_REPAIR_ATTEMPTS=2
```

Configuración cargada desde `.env` via Pydantic Settings (`app/core/config.py`).

---

## Comandos

```bash
# Setup
poetry install --no-root

# Tests
poetry run pytest -q                                          # todos
poetry run pytest tests/services/test_recovery_service.py -v # un archivo
poetry run pytest tests/services/test_recovery_service.py::test_name -v  # un test

# Lint y formato
poetry run ruff check .          # lint
poetry run ruff check . --fix    # auto-fix
poetry run black .               # formato
poetry run black --check .       # verificar formato
poetry run mypy app/             # type check

# Pre-commit
pre-commit run --all-files
```

CI: `ruff check .` + `black --check .` + `pytest -q` en Python 3.12.

---

## Tests

SQLite in-memory (`:memory:`) via fixtures en `tests/conftest.py`. Fixtures clave: `db_session`, `make_project`, `make_task`, `make_execution_run`, `make_recovery_decision`, `make_execution_plan`.

**239 tests — todos passing.**

| Área | Archivo(s) |
|---|---|
| Task execution service | `test_task_execution_service.py`, `test_task_execution_invariants.py`, `test_task_execution_validation_flow.py` |
| Validation | `test_validation_service.py`, `test_code_change_agent_validator.py`, `test_command_runner_agent_validator.py`, `test_aggregation.py` |
| Orchestrator + engine | `test_execution_engine.py`, `test_command_runner_agent_subagent.py`, `test_command_tool.py` |
| Recovery | `test_recovery_service.py`, `test_task_hierarchy_service.py` |
| Post-batch | `test_post_batch_service.py`, `test_post_batch_service_problematic_outcomes.py`, `test_post_batch_decision_service.py` |
| Live plan mutation | `test_live_plan_mutation_service.py` |
| Execution plan patch | `test_execution_plan_patch_service.py` |
| Recovery assignment compiler | `test_recovery_assignment_compiler_service.py` |
| Evaluación | `test_evaluation_service.py`, `test_stage_evaluation_output.py`, `test_evaluation_schema.py` |
| Project workflow | `test_project_workflow_service.py` |
| Workspace runtime | `test_local_workspace_runtime.py` |
| Execution plan service | `test_execution_plan_service.py` |
| API | `test_projects.py` |

---

## Cambios recientes

### Retry de timeouts LLM (`services/llm/openai_provider.py`)

`APITimeoutError` se incluye ahora en el loop de reintentos del provider, junto con `InternalServerError` (5xx). El sistema reintenta hasta 2 veces (esperas de 2s y 4s) antes de propagar el error. Antes, un timeout transitorio propagaba directamente como fallo de tarea, desencadenando recovery innecesaria y consumiendo iteraciones del workflow. Con este fix, los timeouts transitorios se absorben a nivel de infraestructura sin afectar al estado de la tarea.

### Contexto de archivos para tareas de recovery

Las tareas de recovery (`is_recovery_task=True` o `followup_depth > 0`) ejecutan en contexto degradado: el `context_selection_agent` tiende a seleccionar las mismas tareas históricas que fallaron, sin acceso directo a los archivos que deberían editar.

**Tres mejoras implementadas:**

1. **Sibling file hints** (`request_adapter.py`): `_load_recovery_source_file_hints` recopila los `files_read` de los runs hermanos fallados/parciales y los inyecta como `preloaded_dependency_files`. Los archivos de las dependencias de los runs fallados llegan al `code_change_agent` sin pasar por el selector de contexto.

2. **Enriquecimiento del modelo de recovery** (`recovery_service.py`, `recovery_client.py`): el prompt de `call_recovery_model` incluye el contenido de hasta 3 archivos fuente no-test leídos durante el run fallado. El decisor tiene visibilidad de la API real antes de proponer qué tareas crear.

3. **Anti-repetición cap** (`recovery_service.py`): `recovery_repeated_failure_cap` escala a `manual_review` cuando hay ≥ 1 sibling recovery ya fallado/parcial (antes el umbral era ≥ 2). La cadena de followups queda acotada a: tarea original → un followup → `manual_review`.

### Campo `followup_depth` en Task

Nuevo campo `followup_depth: int` en el modelo `Task` (migración Alembic `d4e5f6a7b8c9`). Registra la profundidad de la tarea en su cadena de followup (0 = tarea original, 1 = primer followup, etc.).

`recovery_followup_depth_cap`: si `followup_depth >= 2` y la acción propuesta es `insert_followup`, el sistema fuerza `manual_review`. El cap evita cadenas de micro-followups que agotan el presupuesto de iteraciones del workflow.

### Reparación de ceguera contextual en `code_change_agent` (`subagents/code_change_agent.py`)

En los pases de reparación (repair passes), el agente solo tenía acceso a los archivos del contexto histórico seleccionado, no a los archivos que él mismo había escrito en intentos anteriores del mismo run. El resultado era que el agente reconstruía el contenido de los archivos de memoria, acumulando drift respecto al estado real del workspace.

**Fix:** nuevo section "Current workspace state" en el prompt de `code_change_agent`. Antes de construir el related file context, se cargan los contenidos actuales de todos los archivos presentes en `state.evidence.changed_files` desde el overlay del workspace. El parámetro `exclude_paths` evita duplicar ese contenido en la sección "Related file content". Los pases de reparación ahora parten del estado real, no de una reconstrucción especulativa.

### Corrección de robustez en live plan mutation

**Bug en producción:** el post-batch lanzaba HTTP 400 con `"Post-batch intent required assignment of all new recovery tasks, but no patched execution plan was produced"`.

**Causa raíz:** `_should_run_immediate_resequence_patch` solo producía un patch cuando `new_recovery_tasks_blocking=True`. Las otras condiciones que disparan un intent `resequence` caían en `resequence_deferred` con `patched_execution_plan=None`, haciendo explotar el guard de "all tasks must be assigned".

**Fix en dos capas:**

1. **Eliminación del gate** (`live_plan_mutation_service.py`): `_should_run_immediate_resequence_patch` ahora siempre produce un patch inmediato cuando existen recovery tasks bajo un intent `resequence`. El flag `new_recovery_tasks_blocking` es advisory, no un gate de obligación.

2. **Escalado defensivo** (`post_batch_service.py`): si el path llega a `resequence_deferred` con recovery tasks pendientes de asignar, el sistema escala automáticamente a un intent `assign` en lugar de crashear. Self-healing que previene la pérdida permanente de tareas de recovery.

### Corrección de validación prematura en patch de plan

`insert_patch_batch_after_batch` construía el plan provisional via `ExecutionPlan(...)` con validación Pydantic completa. En planes de un solo batch, el patch batch insertado se convierte en el batch final sin `stage_closure`, disparando el validator antes de que la normalización pueda corregirlo.

Fix: `model_construct` para el plan provisional. La normalización produce el plan final válido con `stage_closure` garantizado.

### Status `followed_up` — estado terminal de followup

La acción `insert_followup` del sistema de recovery transiciona la tarea fuente al status `followed_up` en lugar de dejarla en `partial`. Cierra el path que dejaba tareas en estado ambiguo indefinidamente.

- `TASK_STATUS_FOLLOWED_UP = "followed_up"` añadido a `TERMINAL_TASK_STATUSES` y `VALID_TASK_STATUSES`
- Lógica de cierre de padre: padre → `completed` cuando todos los hijos ∈ `{completed, reatomized, followed_up}`

---

## Próximos pasos propuestos

### Alta prioridad

**1. Precisión del validador en decisiones parciales**
El `command_runner_agent_validator` puede declarar `partial` incluso cuando el terminal test tiene exit_code=0, si el LLM detecta scope no cubierto en los criterios de aceptación. Esta sobre-generación de decisiones parciales alimenta cadenas de followups que terminan en `recovery_followup_depth_cap`. El fix en `_normalize_llm_output_for_terminal_success` ya eleva a `completed` cuando el terminal test es limpio y la tarea parece implementación ejecutable, pero la lista de markers de `_task_looks_like_executable_implementation` es incompleta (no cubre persistence, storage, data). Ampliar esa lista o relajar el criterio de normalización para tareas con tests pasando.

**2. Test de integración del escalado defensivo `resequence_deferred → assign`**
El path de fallback introducido en el fix de robustez no tiene cobertura de test directa. Añadir un test en `test_post_batch_service.py` que monkeypatchee `mutate_live_plan` para devolver `resequence_deferred` con recovery tasks y verifique que el escalado a `assign` se ejecuta y produce un plan válido.

**3. Observabilidad estructurada del pipeline completo**
El sistema ya emite `logger.warning` en cada guard de recovery y en el escalado defensivo. Estandarizar los campos de log (`batch_id`, `project_id`, `mutation_kind`, `intent_type`, `recovery_task_ids`, `followup_depth`, `guard_triggered`) de forma consistente en todo el pipeline para facilitar correlación en producción sin necesidad de leer artifacts.

### Media prioridad

**4. Optimización del tamaño de prompt en `code_change_agent`**
La inyección del workspace state puede generar prompts de 70-80k caracteres en proyectos con historial extenso, aumentando la probabilidad de timeout. Añadir un presupuesto de caracteres configurable para las secciones de contenido de archivos (workspace state + related file context), truncando por tamaño antes de insertar en el prompt.

**5. Enriquecimiento de `StageEvaluationInput` con datos de run**
El `evaluation_service` juzga el outcome de un batch pero no tiene acceso al estado de los runs individuales. Añadir un resumen de run-level a la entrada del evaluador para producir evaluaciones más precisas en batches con mezcla de éxito parcial y fallo.

**6. Test explícito del caso single-batch con patch**
El fix de `model_construct` cubre un escenario real: plan de un solo batch donde el patch batch se convierte en el final. Añadir test en `test_execution_plan_patch_service.py` que ejercite este path directamente.

**7. Telemetría estructurada en recovery assignment compiler**
`compile_recovery_assignment_plan` escala a `requires_replan=True` sin exponer exactamente qué cluster falló y por qué. Añadir notas estructuradas (cluster_id, razón del fallo) antes del escalado para simplificar el debugging de replans inesperados.

### Baja prioridad

**8. Contexto estructural del repositorio en `context_selection_agent`**
El agente selecciona tareas históricas pero no tiene visibilidad del layout actual del repo. Añadir un snapshot ligero de la estructura del workspace como input adicional para mejorar la relevancia del contexto seleccionado.

**9. Métricas de ejecución por proyecto**
Tasa de recovery por acción, tasa de replan, distribución de `mutation_kind` por batch, frecuencia de timeout retries. Datos útiles para ajustar parámetros de orquestación y detectar proyectos problemáticos antes de que agoten el iteration limit.

**10. Tests end-to-end con engine real**
Los tests actuales cubren servicios individuales con mocks. Añadir tests de integración que ejerciten el flujo completo desde `execute_task_sync` hasta la reconciliación de jerarquía, con un engine real pero LLM mockeado a nivel de provider.

---

## Invariantes del sistema

| Área | Invariante |
|---|---|
| Ejecución | `finish` requiere evidencia; `reject` es salida válida; loops solo si hay gap real |
| Validación | Validadores independientes entre sí; agregación determinista |
| Persistencia | 1 run → 1 artifact; artifact contiene la verdad final |
| Workspace | Aislamiento total entre runs; `run/` siempre eliminado; promoción controlada |
| Plan | El checkpoint final siempre incluye `stage_closure` |
| Recovery | `is_recovery_task=True` bloquea `reatomize`; `followup_depth >= 2` bloquea `insert_followup`; ≥1 sibling recovery fallado bloquea nuevo followup |
| Jerarquía | Propagación determinista; rollback si falla algún paso; sin efectos parciales sobre padres |
| Descomposición | `MAX_ATOMIC_TASKS_PER_PARENT = 8`; `MAX_IMPLEMENTATION_STEPS_PER_ATOMIC = 20` |
