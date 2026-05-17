# Agente Desarrollador

Sistema de orquestación multi-agente para la ejecución autónoma de tareas de desarrollo. Ejecuta tareas atómicas con validación estructurada, trazabilidad de evidencia y recuperación determinista.

**Stack:** FastAPI · SQLAlchemy 2.0 · PostgreSQL · OpenAI structured outputs · Docker

---

## Estado del proyecto

### Lo que está implementado y funcionando

El sistema es capaz de gestionar un proyecto de software de extremo a extremo de forma autónoma, desde la conversación inicial con el usuario hasta la entrega de los artefactos generados:

1. **Pipeline de planificación completo** — descomposición en tres niveles: `high_level` → `refined` (opcional) → `atomic`, más generación del plan de ejecución secuenciado en batches con checkpoints.

2. **Motor de ejecución orquestado** — loop de decisión en dos fases (discovery → execution) con tres subagentes (`context_selection_agent`, `code_change_agent`, `command_runner_agent`), gestión de presupuesto (`max_steps`, `max_agent_calls`, `max_repair_attempts`) y trazabilidad completa de evidencia.

3. **Sistema de validación modular** — routing dinámico de validadores, validador de código con rendering especializado, y agregación determinista con prioridad `failed > manual_review > partial > completed`. Cada validador puede enrutarse a un modelo LLM independiente via `VALIDATOR_MODEL` / `VALIDATOR_PROVIDER`.

4. **Pipeline post-batch completo** — evaluación de stage con LLM, recovery (reatomize / insert_followup / manual_review), decisión de intención post-batch, compiler de asignación de recovery, y mutación viva del plan (patch, resequence, replan).

5. **Sistema de entorno Docker** — planificación del entorno de ejecución con LLM, bootstrapping de contenedores Docker, validación del entorno (smoke test + repair), y gestión del ciclo de vida de sesiones. Soporta `python_slim`, con soporte declarado para `node_npm` y `rust_cargo`. Rebuild eager cuando el usuario proporciona clarificaciones que implican nuevas dependencias.

6. **Workflow completo por iteraciones** — el `ProjectWorkflowService` ejecuta iteraciones hasta cerrar el stage, con guards contra agotamiento del plan, reapertura de finalización, y límite de iteraciones configurable. Soporta pausa cooperativa y reanudación.

7. **Agente conversacional Aria** — máquina de estados de 6 fases (`gathering → ready → executing ⇄ awaiting_review → paused → completed`) con evaluadores LLM especializados por fase. La revisión manual no tiene límite de intentos: el proceso sólo se reanuda tras confirmación explícita del usuario sobre un plan concreto. El motivo del bloqueo real (del último `ExecutionRun`) se inyecta en la apertura del episodio.

8. **Control de flujo del workflow** — pausa cooperativa vía `threading.Event`, reanudación desde pausa o crash, y recuperación automática al arrancar el servidor (re-encola workflows en estado `executing` con tareas pendientes).

9. **Frontend React + WebSocket** — interfaz completa con Vite + React (`frontend/`), comunicación en tiempo real via WebSocket, historial de conversación, panel de tareas con estado en vivo, y controles de pausa/reanudación. El input se bloquea en la UI durante la ejecución.

10. **Q&A de estado del proyecto** — en fases PAUSED y COMPLETED, el `ProjectQueryAgent` responde preguntas del usuario sobre el estado del proyecto con información real de la BD (tareas completadas, pendientes, fallidas).

### Cambios recientes significativos

- **Agente conversacional Aria (overhaul completo)**: flujo de revisión manual rediseñado sin límite de intentos; sub-fase de confirmación (`review_subphase: gathering → awaiting_confirmation`) que requiere aprobación explícita del usuario antes de reanudar; motivo de bloqueo real extraído del `ExecutionRun`; `ReviewEvaluator` enriquecido con progreso de tareas del proyecto como contexto.
- **Pausa y reanudación del workflow**: pausa cooperativa por proyecto via `threading.Event`, endpoints `POST /pause` y `POST /resume-workflow`, y recovery automático al arranque del servidor para proyectos que se quedaron en ejecución tras un crash.
- **Rebuild eager de entorno**: cuando la clarificación del usuario implica nuevas librerías, el `ImpactAssessmentAgent` las detecta y el `ResumptionService` hace teardown + bootstrap del contenedor Docker con el nuevo spec antes de reintentar.
- **Routing de modelo por capa**: validadores (`code_change_agent_validator`, `command_runner_agent_validator`) enrutables a un modelo independiente via `VALIDATOR_MODEL` / `VALIDATOR_PROVIDER` (ej. `gpt-5.2`).
- **Frontend React + WebSocket** (`frontend/`): UI completa con Vite + React, comunicación en tiempo real, historial de conversación, estado de tareas en vivo, y controles de pausa/reanudación.
- **Budget exhaustion → COMPLETED**: cuando el orquestador agota `max_steps`, el resultado se marca `completed` en lugar de `failed`, enviando el trabajo acumulado a validación para salvaguardar el trabajo parcial.
- **`StageEvaluationOutput` derivación determinista**: `recommended_next_action`, `plan_change_scope` y `remaining_plan_still_valid` se derivan automáticamente de los campos fuente de verdad, eliminando una clase entera de errores de validación por salidas contradictorias del LLM.
- **Docker stop 409 race condition**: el sistema maneja correctamente el caso donde Docker elimina el contenedor automáticamente (`--rm`) antes de que `stop_session` llame a `remove()`.
- **`EnvVar` model para variables de entorno**: reemplaza `dict[str, str]` en `RuntimeEnvironmentPlanOutput` para cumplir con el subconjunto estricto de JSON Schema de OpenAI Structured Outputs.
- **Redis eliminado**: la dependencia de Redis era legacy y nunca se usó en el código. Se eliminó de config y `.env`.

### Números actuales

- **469 tests unitarios** — todos passing
- **12 tests de integración** (Docker) — se ejecutan con `-m integration`
- **0 failures** en CI

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
        │     ├── EnvironmentBootstrapper → Docker session
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

## Agente Conversacional — Aria (`app/services/conversation/`)

Aria gestiona la interacción con el usuario a lo largo de todo el ciclo de vida del proyecto. Es una máquina de estados con 6 fases; el routing de mensajes es siempre unidireccional — ningún handler llama a otro.

### Fases y transiciones

```
GATHERING → READY → EXECUTING ⇄ AWAITING_REVIEW
                         ↕
                       PAUSED
                         ↕
                     COMPLETED
```

| Fase | Qué hace Aria |
|---|---|
| `gathering` | Hace preguntas via `RequirementsEvaluator` hasta tener suficiente contexto para el plan |
| `ready` | Cualquier mensaje del usuario (o botón Start) lanza `ProjectStartService` |
| `executing` | Input bloqueado en el frontend; backend devuelve mensaje estático |
| `awaiting_review` | Episodio de revisión manual con dos sub-fases (ver abajo) |
| `paused` | El usuario puede preguntar sobre el estado del proyecto (`ProjectQueryAgent`) |
| `completed` | Idem; Aria emite un mensaje de cierre con el recuento final de tareas |

### Sub-fases de revisión manual (`awaiting_review`)

El campo `review_subphase` en `Conversation` rastrea la posición dentro del episodio:

```
gathering  →  [ReviewEvaluator: ready_to_confirm]  →  awaiting_confirmation
    ↑                                                          |
    └──────── [ConfirmationEvaluator: confirmed=False] ←───────┘
                                                               |
                                              [confirmed=True] ↓
                                            ResumptionService → fase EXECUTING
```

- **Sin límite de intentos** — el episodio continúa indefinidamente hasta obtener confirmación o que el usuario abandone.
- **Notas de validación reales** — la apertura del episodio incluye `validation_notes + blockers_found + error_message` del último `ExecutionRun`, para que el usuario entienda el motivo del bloqueo.
- **Contexto de progreso** — `ReviewEvaluator` recibe un resumen de las tareas completadas/fallidas/pendientes para responder preguntas de estado dentro del episodio.
- **Rebuild eager de entorno** — si la clarificación implica nuevas dependencias, `ResumptionService` llama al `ImpactAssessmentAgent`, hace teardown del contenedor Docker, y lo reconstruye con el nuevo spec antes de reintentar.

### Evaluadores LLM

| Servicio | Input | Output | Usado en |
|---|---|---|---|
| `RequirementsEvaluator` | historial + draft actual | `needs_more` / `sufficient` | `gathering` |
| `ReviewEvaluator` | task context + episode history + task progress | `insufficient` / `ready_to_confirm` / `abandoned` | `awaiting_review` (gathering) |
| `ConfirmationEvaluator` | action_summary + user_response | `confirmed: bool` + `follow_up` | `awaiting_review` (awaiting_confirmation) |
| `ProjectQueryAgent` | task list (por estado) + user question | respuesta en lenguaje natural | `paused`, `completed` |
| `ImpactAssessmentAgent` | user clarification + project context | scope + `environment_changes` | `ResumptionService` |

### Control de flujo del workflow

```python
# Pausa cooperativa
POST /conversations/pause          → request_workflow_stop(project_id)
                                    # threading.Event → loop comprueba antes de cada tarea

# Reanudación
POST /conversations/resume-workflow → clear_workflow_stop(project_id)
                                     → conversation.phase = EXECUTING
                                     → _workflow_executor.submit(...)

# Recovery de crash (en _lifespan de FastAPI)
# Al arrancar: re-encola proyectos en EXECUTING con tareas PENDING
```

### API del agente conversacional

| Método | Ruta | Descripción |
|---|---|---|
| `WS` | `/ws/projects/{project_id}/chat` | Canal principal de chat en tiempo real |
| `GET` | `/projects/{project_id}/conversations/active` | Estado actual de la conversación + historial |
| `POST` | `/projects/{project_id}/conversations/notify-review` | Notifica inicio de revisión manual (desde capa de ejecución) |
| `POST` | `/projects/{project_id}/conversations/notify-task-event` | Broadcast de cambio de estado de tarea (desde workflow) |
| `POST` | `/projects/{project_id}/conversations/confirm-start` | Inicia el proyecto desde el botón Start de la UI |
| `POST` | `/projects/{project_id}/conversations/pause` | Solicita parada cooperativa del workflow |
| `POST` | `/projects/{project_id}/conversations/resume-workflow` | Reanuda workflow pausado o bloqueado por crash |

### Modelo de datos de conversación

```python
Conversation:
    phase: str                        # gathering | ready | executing | awaiting_review | paused | completed
    review_task_id: int | None        # tarea bloqueada actualmente en revisión
    review_episode_attempts: int      # contador informativo (sin límite funcional)
    review_subphase: str | None       # gathering | awaiting_confirmation
    pending_clarification_summary: str | None  # plan propuesto pendiente de confirmación
    requirements_draft: str | None    # borrador acumulado durante gathering
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
| `ProjectExecutionContext` | Contexto del proyecto inyectado en el plan y en el LLM |
| `CandidateAtomicTask` | Snapshot de tarea atómica como input al secuenciador |
| `ExecutionStateSummary` | Estado de ejecución actual: tareas completadas, pendientes, fallidas |

---

## Runtime Environment System (`app/services/environment/`)

Sistema de entorno de ejecución basado en Docker. Se activa para tareas que requieren ejecución real de código.

### Flujo

```
EnvironmentPlanner   → RuntimeSpec (image, dependencies, env vars)
EnvironmentBootstrapper
  → pull imagen Docker
  → arrancar contenedor
  → instalar dependencias
  → smoke test
  → (si falla) repair via LLM → retry
EnvironmentSession   → proxy para comandos en el contenedor
stop_session()       → detener y eliminar contenedor
```

### Componentes

| Archivo | Rol |
|---|---|
| `planner.py` | Llama al LLM para producir `RuntimeEnvironmentPlanOutput` y lo convierte en `RuntimeSpec` |
| `bootstrapper.py` | Arranca el contenedor, instala dependencias, valida con smoke test, repara si falla |
| `docker_driver.py` | Wrapper sobre la SDK de Docker: pull, run, exec, stop/remove |
| `validator.py` | Ejecuta el smoke test y decide si el entorno está listo |
| `session_store.py` | Persiste y recupera `EnvironmentSession` activas por proyecto |
| `registry.py` | Registry de drivers por `RuntimeType` |
| `contracts.py` | Tipos compartidos: `RuntimeSpec`, `EnvironmentSession`, `EnvironmentCommandResult` |

### Tipos clave

| Tipo | Descripción |
|---|---|
| `RuntimeSpec` | Especificación del entorno: `runtime_type`, `image`, `dependencies`, `environment_variables` |
| `EnvironmentSession` | Sesión activa: `project_id`, `container_id`, `project_root`, `runtime_type` |
| `RuntimeEnvironmentPlanOutput` | Output del LLM: imagen, dependencias, vars de entorno (lista de `EnvVar`) |
| `EnvVar` | Par `key`/`value` para variables de entorno (compatible con OpenAI Structured Outputs) |

Runtimes soportados: `python_slim` (implementado y probado), `node_npm` y `rust_cargo` (declarados en tipos, drivers pendientes).

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
- **Budget exhaustion**: si se agota `max_steps`, el resultado se marca `COMPLETED` (no `FAILED`) para enviar el trabajo parcial a validación

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
| `RecommendedNextAction` | `close_stage`, `continue_current_plan`, `resequence_remaining_batches`, `replan_remaining_work`, `manual_review` |
| `PlanChangeScope` | `none`, `local_resequencing`, `remaining_plan_rebuild`, `high_level_replan` |
| `RecoveryStrategy` | `none`, `reatomize_failed_tasks`, `insert_followup_atomic_tasks`, `replan_from_high_level`, `manual_review` |

`StageEvaluationOutput` deriva automáticamente `recommended_next_action`, `plan_change_scope` y `remaining_plan_still_valid` desde los campos fuente de verdad del modelo. El LLM no puede producir combinaciones contradictorias.

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

| Acción | Status tarea fuente | `is_recovery_task` |
|---|---|---|
| `reatomize` | `reatomized` | `True` |
| `insert_followup` | `followed_up` | `False` |
| `manual_review` | sin cambio (`partial`) | — |

**Guards de recovery:**

| Guard | Condición | Efecto |
|---|---|---|
| `recovery_anti_cascade` | `is_recovery_task=True` + acción `reatomize` | Fuerza `manual_review` |
| `recovery_followup_depth_cap` | `followup_depth >= 2` + acción `insert_followup` | Fuerza `manual_review` |
| `recovery_repeated_failure_cap` | ≥ 1 sibling recovery fallado/partial + `insert_followup` | Fuerza `manual_review` |

### 3. Post-Batch Decision Service (`app/services/post_batch_decision_service.py`)

Traduce las señales del evaluador y el contexto de recovery en un `ResolvedPostBatchIntent` canónico.

| Campo | Tipo | Descripción |
|---|---|---|
| `intent_type` | `continue\|assign\|resequence\|replan\|manual_review\|close` | Acción a tomar |
| `mutation_scope` | `none\|assignment\|resequence\|replan` | Alcance de la mutación del plan |
| `remaining_plan_still_valid` | `bool` | Si el plan restante sigue siendo válido |
| `has_new_recovery_tasks` | `bool` | Si se crearon tareas de recovery |
| `requires_plan_mutation` | `bool` | Si el plan debe ser mutado |
| `can_continue_after_application` | `bool` | Si se puede continuar tras la mutación |
| `should_close_stage` | `bool` | Si el stage debe cerrarse |

### 4. Recovery Assignment Compiler (`app/services/recovery_assignment_compiler_service.py`)

Cuando `intent_type="assign"`, compila una propuesta de asignación de clusters en el plan activo vía LLM. Si no puede asignar todos los clusters emite `requires_replan=True` y el pipeline escala a replan.

### 5. Live Plan Mutation Service (`app/services/live_plan_mutation_service.py`)

Aplica la mutación final al plan basándose en el intent resuelto.

| Kind | Cuándo |
|---|---|
| `none` | Intent `continue`, `manual_review` o `close` |
| `assignment` | Tareas de recovery asignadas via compiler |
| `resequence_patch` | Patch batch insertado inmediatamente tras el anchor |
| `resequence_deferred` | Resequence sin recovery tasks |
| `escalated_to_replan` | Plan completo inválido; se requiere replanning |

**Escalado defensivo:** si `resequence_deferred` llega con recovery tasks pendientes de asignar, el sistema escala automáticamente a `assign` en lugar de crashear.

---

## Execution Plan Patch Service (`app/services/execution_plan_patch_service.py`)

Inserta patch batches en el plan activo sin replanificar.

- `insert_patch_batch_after_batch()` — usa `model_construct` para evitar validación Pydantic prematura durante la inserción
- `normalize_execution_plan_terminal_invariants()` — garantiza `stage_closure` en el checkpoint final, reindexación correcta de batches, y unicidad de checkpoint IDs

---

## Project Workflow Service (`app/services/project_workflow_service.py`)

Orquesta el pipeline completo de un proyecto, iteración por iteración.

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
- **Plan exhaustion:** si no quedan batches pero el workflow no cerró, se escala
- **Finalization reopening:** si un batch de cierre produce recovery, la finalización se reabre con `reopened_finalization=True`
- **Iteration limit:** límite configurable para prevenir loops infinitos

---

## Project Memory Service (`app/services/project_memory_service.py`)

Construye el `ProjectOperationalContext` que se inyecta como contexto a los LLM calls.

| Tipo | Descripción |
|---|---|
| `ProjectOperationalContext` | Snapshot completo del estado del proyecto para el LLM |
| `ProjectMemoryTaskSummary` | Resumen de una tarea completada: title, outcome, evidence snapshot |
| `ProjectMemoryDecisionSignal` | Señal de decisión registrada: signal_type, batch_id, notes |
| `ProjectMemoryPathSignal` | Señal de path: rutas creadas/modificadas con su propósito inferido |

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

La promoción fusiona el overlay workspace sobre source. El directorio `run` siempre se elimina al finalizar.

---

## LLM Integration (`app/services/llm/`)

Todos los llamados LLM usan **OpenAI structured outputs** (respuestas constreñidas por JSON schema estricto).

| Archivo | Rol |
|---|---|
| `base.py` | Interfaz abstracta `BaseLLMProvider` |
| `openai_provider.py` | Implementación con structured outputs via `response_format`; reintentos en timeout (2s, 4s) |
| `factory.py` | Retorna el provider configurado según env vars |
| `schema_utils.py` | Convierte schemas Pydantic al subconjunto estricto de JSON Schema de OpenAI: `additionalProperties: false`, elimina `"default"`, hace todos los campos `required` |

Modelo por defecto: `gpt-5.1` (configurable via `OPENAI_MODEL`).

---

## Modelos de datos

### Task statuses

```
pending → running → awaiting_validation → completed
                                        → partial       → (recovery)
                                        → failed        → (recovery)
                  → reatomized   (terminal: recovery con reatomize)
                  → followed_up  (terminal: recovery con insert_followup)
```

`TERMINAL_TASK_STATUSES = {partial, completed, failed, reatomized, followed_up}`

### Tipos de artifacts

| Tipo | Creador |
|---|---|
| `project_plan` | `planner` |
| `technical_refinement` | `technical_task_refiner` |
| `atomic_task_generation` | `atomic_task_generator` |
| `execution_plan` | `execution_plan_service` |
| `execution_plan_patch` | `execution_plan_patch_service` |
| `validation_result` | validation service |
| `recovery_decision` | `recovery_service` |
| `evaluation_decision` | `evaluation_service` |
| `post_batch_result` | `post_batch_service` |
| `workflow_batch_trace` | `project_workflow_service` |

---

## API (`app/api/`)

### Proyectos y tareas

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/projects` | Listar proyectos |
| `GET` | `/projects/{project_id}` | Detalle de proyecto |
| `GET` | `/projects/{project_id}/tasks` | Tareas del proyecto |
| `GET` | `/projects/{project_id}/artifacts` | Artifacts del proyecto |
| `GET` | `/projects/{project_id}/execution_runs` | Runs de ejecución del proyecto |
| `POST` | `/tasks/{task_id}/execute` | Ejecutar una tarea atómica individualmente |

### Pipeline de planificación

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/workflow/projects/{project_id}/run` | Ejecutar el workflow completo del proyecto |
| `POST` | `/projects/{project_id}/plan` | Ejecutar solo la fase de planning |
| `POST` | `/projects/{project_id}/technical_task_refiner` | Refinar una tarea high-level |
| `POST` | `/projects/{project_id}/atomic_task_generator` | Generar tareas atómicas para un padre |

### Agente conversacional (Aria)

| Método | Ruta | Descripción |
|---|---|---|
| `WS` | `/ws/projects/{project_id}/chat` | Canal WebSocket principal de chat |
| `GET` | `/projects/{project_id}/conversations/active` | Estado y historial de la conversación activa |
| `POST` | `/projects/{project_id}/conversations/confirm-start` | Iniciar proyecto desde botón Start de la UI |
| `POST` | `/projects/{project_id}/conversations/notify-review` | Notificar inicio de revisión manual (desde workflow) |
| `POST` | `/projects/{project_id}/conversations/notify-task-event` | Broadcast de cambio de estado de tarea |
| `POST` | `/projects/{project_id}/conversations/pause` | Solicitar pausa cooperativa del workflow |
| `POST` | `/projects/{project_id}/conversations/resume-workflow` | Reanudar workflow pausado o tras crash |

---

## Configuración

Variables requeridas en `.env`:

```
DATABASE_URL=postgresql+psycopg2://...
OPENAI_API_KEY=...
AGENTS_PROJECTS_ROOT=...
```

Variables opcionales clave:

```
# Modelo base
OPENAI_MODEL=gpt-5.1
LLM_PROVIDER=openai              # openai | anthropic

# Routing por capa (sobreescriben el modelo base cuando se definen)
EXECUTION_ENGINE_PROVIDER=...    # orchestrator + context_selection_agent
EXECUTION_ENGINE_MODEL=...
CODE_AGENT_PROVIDER=...          # code_change_agent
CODE_AGENT_MODEL=...
COMMAND_AGENT_PROVIDER=...       # command_runner_agent
COMMAND_AGENT_MODEL=...
VALIDATOR_PROVIDER=...           # ambos validadores (ej. openai)
VALIDATOR_MODEL=...              # ej. gpt-5.2

# Motor de ejecución
EXECUTION_ENGINE_BACKEND=orchestrated
EXECUTION_ENGINE_MAX_STEPS=8
EXECUTION_ENGINE_MAX_AGENT_CALLS=8
EXECUTION_ENGINE_MAX_TOOL_CALLS=12
EXECUTION_ENGINE_MAX_COMMAND_RUNS=4
EXECUTION_ENGINE_MAX_REPAIR_ATTEMPTS=2
```

---

## Comandos

```bash
# Setup
poetry install --no-root

# Tests unitarios
poetry run pytest -q

# Tests de integración (requiere Docker)
poetry run pytest -m integration -v

# Un archivo / un test
poetry run pytest tests/services/test_recovery_service.py -v
poetry run pytest tests/services/test_recovery_service.py::test_name -v

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

**469 tests unitarios + 12 tests de integración — todos passing.**

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
| Agente conversacional | `test_project_assistant.py` |
| Environment (integración) | `tests/integration/test_environment_integration.py` |
| API | `test_projects.py` |

---

## Próximos pasos propuestos

### Alta prioridad

**1. Soporte multi-stage**
El sistema ejecuta un único stage por proyecto. Para proyectos reales con múltiples stages secuenciales (ej. "backend" → "frontend" → "integración"), el `ProjectWorkflowService` necesita un loop externo que cierre el stage actual, genere el siguiente, y reutilice el contexto acumulado. Las bases ya están: `project_stage_closed`, `stage_goal`, y la memoria de proyecto (`ProjectOperationalContext`) son stage-aware.

**2. Drivers de entorno para Node.js y Rust**
`node_npm` y `rust_cargo` están declarados en los tipos pero sus drivers no están implementados. Añadir `NodeNpmDriver` y `RustCargoDriver` siguiendo el patrón de `DockerDriver`, con sus smoke tests y comandos de instalación correspondientes.

**3. Tests de integración end-to-end del flujo conversacional**
Los tests de `project_assistant` mockeando el WebSocket y el workflow en background para verificar el flujo completo: inicio de conversación → gathering → proyecto iniciado → revisión manual → confirmación → reanudación. Actualmente los tests del agente conversacional usan DB real pero mockean todos los evaluadores LLM.

### Media prioridad

**4. Precisión del validador en decisiones parciales**
El `command_runner_agent_validator` puede declarar `partial` incluso cuando los tests pasan, si detecta scope no cubierto en los criterios de aceptación. Ampliar la lista de markers en `_task_looks_like_executable_implementation` (persistence, storage, data) o relajar el criterio de normalización para tareas con exit_code=0.

**5. Optimización del tamaño de prompt en `code_change_agent`**
La inyección del workspace state puede generar prompts de 70-80k caracteres en proyectos con historial extenso. Añadir un presupuesto de caracteres configurable para las secciones de contenido de archivos, truncando por tamaño antes de insertar en el prompt.

**6. Historial persistido de Q&A del proyecto**
Las respuestas del `ProjectQueryAgent` se guardan en el historial de la conversación, pero el agente no ve conversaciones anteriores al formular la respuesta. Pasar el historial reciente como contexto al agente para que sus respuestas sean coherentes con lo dicho antes.

**7. Observabilidad estructurada del pipeline completo**
Estandarizar los campos de log (`batch_id`, `project_id`, `mutation_kind`, `intent_type`, `recovery_task_ids`, `followup_depth`, `guard_triggered`, `review_subphase`) en todo el pipeline para facilitar correlación en producción sin necesidad de leer artifacts.

**8. Enriquecimiento de `StageEvaluationInput` con datos de run**
El `evaluation_service` juzga el outcome de un batch pero no tiene acceso al estado de los runs individuales. Añadir un resumen de run-level a la entrada del evaluador para producir evaluaciones más precisas en batches con mezcla de éxito parcial y fallo.

### Baja prioridad

**9. Routing de modelo por evaluador conversacional**
Los evaluadores de Aria (`RequirementsEvaluator`, `ReviewEvaluator`, `ConfirmationEvaluator`, `ProjectQueryAgent`) usan el provider por defecto. Añadir configuración granular similar a `VALIDATOR_MODEL` para poder enrutar evaluadores conversacionales a modelos más económicos.

**10. Métricas de ejecución por proyecto**
Tasa de recovery por acción, tasa de replan, distribución de `mutation_kind` por batch, frecuencia de episodios de revisión por tarea, tiempo medio hasta confirmación en revisión manual. Datos útiles para ajustar parámetros de orquestación y detectar proyectos problemáticos antes de que agoten el iteration limit.

**11. Contexto estructural del repositorio en `context_selection_agent`**
El agente selecciona tareas históricas pero no tiene visibilidad del layout actual del repo. Añadir un snapshot ligero de la estructura del workspace como input adicional para mejorar la relevancia del contexto seleccionado.

---

## Invariantes del sistema

| Área | Invariante |
|---|---|
| Ejecución | `finish` requiere evidencia; `reject` es salida válida; budget exhaustion → `completed` para salvaguardar trabajo parcial |
| Validación | Validadores independientes entre sí; agregación determinista |
| Persistencia | 1 run → 1 artifact; artifact contiene la verdad final |
| Workspace | Aislamiento total entre runs; `run/` siempre eliminado; promoción controlada |
| Plan | El checkpoint final siempre incluye `stage_closure` |
| Recovery | `is_recovery_task=True` bloquea `reatomize`; `followup_depth >= 2` bloquea `insert_followup`; ≥1 sibling recovery fallado bloquea nuevo followup |
| Jerarquía | Propagación determinista; rollback si falla algún paso; sin efectos parciales sobre padres |
| Descomposición | `MAX_ATOMIC_TASKS_PER_PARENT = 8`; `MAX_IMPLEMENTATION_STEPS_PER_ATOMIC = 20` |
| Entorno | Smoke test obligatorio antes de usar el contenedor; repair automático con LLM ante fallo de bootstrap |
