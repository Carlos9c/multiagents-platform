# Agente Desarrollador

Sistema de orquestación multi-agente para la ejecución autónoma de tareas de desarrollo. Ejecuta tareas atómicas con validación estructurada, trazabilidad de evidencia y recuperación determinista.

**Stack:** FastAPI · SQLAlchemy 2.0 · PostgreSQL · OpenAI structured outputs · Docker · React/Vite

---

## Estado del proyecto

### Lo que está implementado y funcionando

El sistema es capaz de gestionar un proyecto de software de extremo a extremo de forma autónoma, desde la conversación inicial con el usuario hasta la entrega de los artefactos generados:

1. **Pipeline de planificación completo** — descomposición en tres niveles: `high_level` → `refined` (opcional) → `atomic`, más generación del plan de ejecución secuenciado en batches con checkpoints.

2. **Motor de ejecución orquestado** — loop de decisión en dos fases (discovery → execution) con tres subagentes (`context_selection_agent`, `code_change_agent`, `command_runner_agent`), gestión de presupuesto (`max_steps`, `max_agent_calls`, `max_repair_attempts`) y trazabilidad completa de evidencia.

3. **Sistema de validación modular** — routing dinámico de validadores, validador de código con rendering especializado, y agregación determinista con prioridad `failed > manual_review > partial > completed`. Cada validador puede enrutarse a un modelo LLM independiente via `VALIDATOR_MODEL` / `VALIDATOR_PROVIDER`.

4. **Pipeline post-batch completo** — evaluación de stage con LLM, recovery (reatomize / insert_followup / manual_review), decisión de intención post-batch, compiler de asignación de recovery, y mutación viva del plan (patch, resequence, replan).

5. **Sistema de entorno Docker con catálogo de imágenes** — planificación del entorno con LLM, bootstrapping de contenedores, smoke test + repair, y gestión del ciclo de vida de sesiones. Catálogo de **11 imágenes especializadas** (Python, Node, Java, Rust, Go, .NET, Android, Flutter, React Native, fullstack:py-node, fullstack:java-node) con selección automática via LLM. Rebuild eager cuando el usuario proporciona clarificaciones que implican nuevas dependencias.

6. **Workflow completo por iteraciones** — el `ProjectWorkflowService` ejecuta iteraciones hasta cerrar el stage, con guards contra agotamiento del plan, reapertura de finalización, y límite de iteraciones configurable. Soporta pausa cooperativa y reanudación.

7. **Agente conversacional Aria — orquestador LLM con herramientas** — Aria recibe cada turno (mensaje de usuario o evento de sistema), decide en un loop ligero qué herramienta invocar (`requirements_agent`, `query_agent`, `review_agent`, `confirmation_agent`, `start_project`, `resumption_agent`) y sintetiza la respuesta final. La `phase` de la conversación pasa de router rígido a hint contextual para Aria. El Q&A es accesible en cualquier fase sin romper el flujo activo.

8. **Control de flujo del workflow** — pausa cooperativa vía `threading.Event`, reanudación desde pausa o crash, y recuperación automática al arrancar el servidor (re-encola workflows en estado `executing` con tareas pendientes).

9. **Frontend React + WebSocket** — interfaz completa con Vite + React (`frontend/`), comunicación en tiempo real via WebSocket, historial de conversación, panel de tareas con estado en vivo, y controles de pausa/reanudación. El input se bloquea en la UI durante la ejecución.

10. **Q&A de estado del proyecto** — en fases PAUSED y COMPLETED, el `ProjectQueryAgent` responde preguntas del usuario sobre el estado del proyecto con información real de la BD (tareas completadas, pendientes, fallidas).

11. **Sistema Supervisor — meta-evaluación de agentes** — capa de supervisión post-ejecución que evalúa retrospectivamente la calidad de cada agente del sistema. 22 evaluadores LLM independientes producen un veredicto por agente (`healthy` / `needs_attention` / `degraded` / `not_supervised`). El veredicto global se calcula como media ponderada (healthy=2, needs_attention=1, degraded=0) con umbrales 1.7/1.3. Si más del 30% de los agentes no pudieron evaluarse, el veredicto global es `not_evaluated`. Incluye un analizador agregado multi-proyecto que detecta patrones de degradación transversales entre 5–20 proyectos. Todos los resultados se persisten en `SupervisorReport` y `AgentEvaluation` en base de datos.

12. **Motor QA — aseguramiento de calidad adversarial autónomo** — sistema QA post-ejecución que analiza el proyecto completo usando un portafolio de 11 agentes especializados orquestados por un bucle LLM. Se activa cuando el proyecto alcanza `COMPLETED` y el usuario lo confirma a través de Aria. La arquitectura incluye: (a) **7 estrategias por tipo de producto** que definen los agentes permitidos para cada caso; (b) **QABootstrapper** para compilar artefactos antes del sondeo (APK Android); (c) **QAOrchestrator** con bucle de decisión LLM y 7 restricciones de presupuesto en tiempo real; (d) **5 agentes Fase 5** que ejecutan en Docker (functional_tester, security_scanner, contract_validator, performance_profiler, apk_installer); (e) **6 agentes Fase 7-9** de análisis estructurado puro (functional_qa_agent, boundary_qa_agent, adversarial_qa_agent, security_qa_agent, performance_qa_agent, regression_qa_agent); (f) **synthesis_agent** que sintetiza el veredicto final con tareas de remediación priorizadas; (g) **7 evaluadores del Supervisor** para los agentes QA (evidencia filtrada por `producer_agent`); (h) **QATool** integrado en Aria con flujo completo: oferta → aceptación → ejecución en fondo → presentación de hallazgos → creación de tareas de remediación. Hallazgos persistidos en `QAFinding` con atribución `producer_agent`, `ProbeRecord`s en `QASession.probes` como JSON.

### Cambios recientes significativos

- **Eliminación de tareas durante revisión manual con propagación en cascada (2026-06-07)**:

  Nueva capacidad del sistema de revisión: el usuario puede solicitar la cancelación de tareas pendientes durante un episodio de `AWAITING_REVIEW`. El `ImpactAssessmentAgent` evalúa el alcance de los cambios, y el `ResumptionService` ejecuta el pipeline completo de eliminación + reintegración del plan.

  **Pipeline de eliminación (Fases 1-8):**

  - **(Fase 1 — Contratos)** Nuevo estado `TASK_STATUS_CANCELLED = "cancelled"` añadido a `TERMINAL_TASK_STATUSES`. Nuevo bloque `TaskRevisionSpec` (modificaciones a tareas existentes) y `NewWorkBlock` (nuevo trabajo a añadir con `planning_level: "high_level" | "atomic"`) en los contratos del `ImpactAssessmentAgent`. La salida `ImpactAssessmentLLMOutput` incluye `tasks_to_eliminate: list[int]` (IDs de tareas a cancelar) y `new_work_blocks: list[NewWorkBlock]` (trabajo de reemplazo).

  - **(Fase 2 — ReviewEvaluator)** El evaluador de revisión recibe `full_task_tree` con todas las tareas no-terminales de la jerarquía, incluyendo `parent_task_id` y `depends_on_task_titles`, para que el LLM pueda razonar sobre qué eliminar sin perder el contexto de dependencias.

  - **(Fase 3 — ImpactAssessmentAgent)** El agente recibe el árbol completo de tareas pendientes (`full_task_tree: list[TaskContextForReview]`) junto con el resumen de tareas completadas (`completed_tasks`). Emite `tasks_to_eliminate`, `tasks_to_modify` y `new_work_blocks` en un único output estructurado.

  - **(Fase 4 — Cancelación en cascada)** `_cancel_tasks_and_cascade(db, project_id, task_ids_to_cancel)`: marca tareas como `CANCELLED` y recorre la jerarquía hacia arriba. Un padre sólo se cancela si **todos** sus hijos están en estado `CANCELLED` **y** el padre nunca produjo trabajo completado (`COMPLETED`/`PARTIAL`). Implementa el principio "anula lo que aún no se hizo; preserva lo que ya se entregó".

  - **(Fase 5 — Creación de nuevo trabajo)** `_create_work_from_blocks(db, project, new_work_blocks)`: dos paths distintos:
    - `planning_level="high_level"` → crea nueva tarea padre en BD y llama a `generate_atomic_tasks()` para descomponer (path idéntico al inicio de proyecto)
    - `planning_level="atomic"` → llama directamente a `call_atomic_task_generator_model()` con los parámetros del bloque (`call_type="scope_change"` para trazabilidad en el Supervisor)
    - `_build_sibling_atomic_summary()` previene que el atomizador genere títulos duplicados de tareas ya existentes.

  - **(Fase 6 — Reintegración del plan)** `_reintegrate_plan()` enruta según qué cambió:
    - Si hay nuevas tareas atómicas → `_resequence_with_sequencer()`: llama a `call_execution_sequencer_model(call_type="resequence")` con todos los pendientes como `CandidateAtomicTask`; aplica el nuevo orden via `sync_sequence_order_from_plan()`
    - Si sólo hay cancelaciones o cambios de dependencia → resequenciación mecánica (`_resequence_pending_tasks()`) + flush
    - Si nada cambió → no-op

  - **(Fase 7 — Tests de integración)** `tests/services/conversation/test_resumption_reintegration.py` con 17 tests cubriendo: routing de reintegración, integración con el secuenciador, construcción de candidatos, y pipelines end-to-end (cancel+create, cascade+create, solo-modificaciones, preservación de scope).

  - **(Fase 8 — Alineación supervisores)** Cuatro cambios en la capa de supervisión:
    1. **Artefacto `review_episode`**: nuevo campo `tasks_eliminated_count` en los payloads generados por `ResumptionTool._save_episode_artifact()` y el episodio abandonado en `orchestrator.py`. Ambos paths (confirmed, disruptive_restart, abandoned) son ahora homogéneos.
    2. **`review_episode_evaluator`**: descripción del prompt actualizada con semántica de los cuatro contadores (`tasks_modified`, `tasks_added`, `tasks_eliminated`, `tasks_superseded`) y reglas de calibración de scope.
    3. **`planner_evaluator`**: `_compute_task_stats()` añade `cancelled_count` por parent y calcula `completion_rate = completed / (total − cancelled)` — las tareas canceladas por decisión del usuario no penalizan la tasa de éxito del planificador.
    4. **`task_hierarchy_service`**: `_derive_parent_status_from_children()` rediseñado — la nueva lógica prioriza `FAILED` y `PARTIAL` como bloqueantes, y reconoce al padre como `COMPLETED` cuando tiene trabajo productivo (`completed`/`reatomized`/`followed_up`) aunque el resto de sus hijos sean `CANCELLED` o `SUPERSEDED`.

  **Frontend:** nuevo estado `cancelled` con badge "CANCELADO" en gris-slate (`--slate` con opacidad reducida) y título tachado en la fila de tarea — misma convención visual que `superseded` pero con color ligeramente más claro para distinguir "decisión del usuario" vs. "reemplazado por el sistema".

- **Correcciones de bugs y endurecimiento de prompts (2026-06-01)**:

  *Bugs corregidos:*
  - **`test_builder_agent` invisible al Supervisor** — el agente no escribía entradas en `execution_trace.jsonl`. Añadida función `_write_test_builder_trace()` que emite una entrada por llamada con `call_type` (`"materialise"` | `"needs_dependency"`), `files_written[]` (path + operation + rationale), y `coverage_summary` completo (covered_cases, uncovered_cases, confidence, tested_against, potential_implementation_gaps). El Supervisor puede ahora evaluar el comportamiento real del agente.
  - **`test_builder_agent` recibía inventario plano y `target_paths` vacío** — el inventario del workspace se convierte ahora a un árbol de directorios ASCII con conectores `├──`/`└──` (función `_paths_to_tree()`), y el orquestador tiene la obligación explícita de poblar `target_paths` para toda llamada a `test_builder_agent`. Un `target_paths` vacío se clasifica como error de routing.
  - **Input de Aria bloqueado tras completar el proyecto con tareas fallidas** — `ChatPanel.jsx` tenía `inputDisabled = phase === 'completed'` que desactivaba el textarea en la fase `completed`, impidiendo el Q&A via `ProjectQueryAgent`. Corregido a `inputDisabled = false` con placeholder contextual por fase.
  - **Todos los evaluadores de validadores del Supervisor producían veredicto `null`** — los cuatro evaluadores (`code_change_agent_validator_evaluator`, `command_runner_agent_validator_evaluator`, `test_builder_agent_validator_evaluator`, `document_writer_agent_validator_evaluator`) tenían `_AGENT_NAME` apuntando al nombre del validador en lugar del nombre del ejecutor. `build_pair_evaluation_context()` filtra runs via `agent_participated_in_run(run, agent_name)`, que busca el nombre en `execution_agent_sequence` — los validadores nunca aparecen en esa secuencia, por lo que el contexto era siempre vacío y el evaluador devolvía `result=None`. Corregido: `_AGENT_NAME` apunta ahora al ejecutor correspondiente (`"code_change_agent"`, `"command_runner_agent"`, etc.).

  *Mejoras de prompts (actualización de versiones + alineación de supervisores):*
  - **`orchestrator.yaml` v1.2.0** — regla de convergencia de verificación: si `command_runner_agent` produce dos fallos consecutivos con la misma clase de error sin un agente de reparación entre ellos, el orquestador no debe llamarlo de nuevo; debe enrutar al agente de reparación correcto usando `fault_side`, o cerrar si el presupuesto está agotado. Supervisor `orchestrator_evaluator.yaml` v1.1.0 añade los "convergence loops" como patrón de degradación.
  - **`atomic_task_generator.yaml` v3.1.0** — (a) patrones de AC prohibidos: "importable from X", "coherente con Y", "preparado para Z" — estos patrones describen presencia estática en lugar de comportamiento verificable; (b) regla de split storage+dominio: persistencia y lógica de negocio deben separarse en tareas atómicas distintas porque sus tests requieren infraestructura diferente; (c) regla de scope de testing tasks: una tarea de test no debe abarcar múltiples unidades de implementación independientes con fronteras de fallo separadas. Supervisor `atomic_task_generator_evaluator.yaml` v1.3.0 añade evaluación de los tres criterios nuevos.
  - **`code_change_agent.yaml` v1.2.0** — restricción de scope en repair pass: cuando `materialization_attempt_count > 0` y la evidencia indica un fallo de verificación previo, el agente debe limitarse al cambio mínimo que cierra el gap identificado; no añadir abstracciones, no refactorizar módulos no relacionados. Supervisor `code_change_agent_evaluator.yaml` v1.1.0 añade evaluación de scope en rondas de reparación (`attempt_number > 1`).
  - **`command_runner_agent.yaml` v1.2.0** — regla de adaptación en retry: antes de planificar un comando, el agente examina `prior_commands` para clasificar el tipo de fallo previo (fallo de resolución de dependencias → `verification_not_applicable`; descubrimiento cero de tests → comando más explícito con rutas directas; fallo de aserción → `verification_not_applicable`, la reparación la hace el agente de código; mismo error class dos veces → `verification_not_applicable` estructural). Supervisor `command_runner_agent_evaluator.yaml` v1.1.0 amplía la dimensión "Retry adaptation" con estos patrones específicos.
  - **`recovery_planner.yaml` v1.2.0** — reglas de calibración de confianza: `high` requiere causa raíz nombrada con precisión + acción directamente vinculada + sin incertidumbre de entorno; `medium` cuando la diagnosis es probable pero la evidencia es ambigua; `low` cuando hay múltiples causas plausibles; `high + manual_review` es una combinación contradictoria (siempre degradar a `medium`); sin causa raíz concreta en la evidencia → `manual_review`, no `reatomize` con confianza inflada. Supervisor `recovery_planner_evaluator.yaml` v1.1.0 alinea la dimensión de calibración con estas definiciones.
  - **`test_builder_agent_evaluator.yaml` v1.1.0** — actualizado para consumir `test_builder_trace_entries[]` (5ª dimensión: "Coverage assessment self-awareness" — ¿identifica correctamente gaps de implementación vs. gaps de cobertura?).

- **Mejoras de generación de tareas atómicas (Fases 0–3 + 5B/5C)**: serie de mejoras al pipeline de planificación que incrementan la especificidad, coherencia y calidad de secuenciación de las tareas generadas:
  - **(Fase 0 — entorno primero)** `CatalogSelector` y `EnvironmentPlanner` se ejecutan *antes* del `Planner` y del `AtomicGenerator`, eliminando la dependencia circular: las tareas ya conocen el stack tecnológico cuando se generan.
  - **(Fase 1 — runtime context flow)** `RuntimeSpec` serializado se inyecta en `planner.py`, `technical_task_refiner.py` y `atomic_task_generator.py`. El generador atómico aplica la restricción más fuerte: `proposed_solution` y `implementation_steps` deben referenciar las tecnologías concretas del stack (FastAPI, SQLAlchemy, etc.), no abstracciones genéricas.
  - **(Fase 2 — sibling awareness)** El acumulador de `_run_atomic_generation_phase` pasa un resumen compacto `{title, task_type}` de las tareas atómicas ya generadas (`sibling_atomic_summary`) a cada llamada sucesiva del generador atómico. Evita duplicados y solapes entre ramas de distintos padres dentro del mismo plan.
  - **(Fase 3 — acceptance criteria estructurados)** `PlannedTask.acceptance_criteria` y `AtomicTaskOutput.acceptance_criteria` cambian de `str` a `list[str]` (BREAKING: bumps MAJOR en ambos YAMLs). Cada elemento es un criterio verificable independiente. La columna en BD es JSON. Validación post-generación: suma ≥ 30 chars para tareas de alto nivel; ≥ 15 chars por criterio para atómicas. `format_acceptance_criteria()` en `task.py` maneja listas nuevas y strings legacy de forma transparente.
  - **(Fase 5B — estimated_complexity)** Nuevo campo requerido `estimated_complexity: Literal["XS","S","M","L","XL"]` en `AtomicTaskOutput` y `CandidateAtomicTask`. El generador atómico asigna complejidad por tarea; el secuenciador la usa para balancear la carga de los batches (evita front-loading de múltiples tareas L/XL) sin necesidad de re-leer los pasos de implementación.
  - **(Fase 5C — depends_on_task_titles)** Nuevo campo requerido `depends_on_task_titles: list[str]` en `AtomicTaskOutput`. El generador atómico declara dependencias inter-tarea usando títulos exactos de `sibling_atomic_summary`. El secuenciador las trata como restricciones de ordenación de verdad absoluta (traduce títulos a IDs y las añade a `inferred_dependencies` con prioridad sobre sus propias inferencias). `CandidateAtomicTask` incluye el campo; `None` en BD se convierte a `[]` al construir el candidato.
  Bumps de versión: `planner.yaml` 2.0.0, `atomic_task_generator.yaml` 3.0.0, `execution_sequencer.yaml` 1.2.0. Supervisores de planner, atomic_task_generator y execution_sequencer actualizados con criterios de evaluación para los nuevos campos. Dos migraciones Alembic: `c4d5e6f7a8b9` (columna JSON para acceptance_criteria) y `d5e6f7a8b9c0` (columnas estimated_complexity y depends_on_task_titles).

- **Motor QA completo — Fases 1 a 9 (11 agentes + Supervisor QA + integración Aria)**: nueva capa de aseguramiento de calidad adversarial autónomo en `app/services/qa/`. Incluye modelos de datos `QASession` y `QAFinding` con migración Alembic (`b2c3d4e5f6a7`); contratos unificados (`QARequest`, `QAFindingDetail`, `ProbeRecord`, `QAResult`, `RemediationTask`) en `contracts.py`; 7 estrategias por tipo de producto (`rest_api`, `graphql_api`, `web_app`, `cli_tool`, `library`, `mobile_android`, `desktop_app`) con listas `allowed_agents` exhaustivas incluyendo todos los agentes Fase 5 y Fase 7-9; `QABootstrapper` con detección LLM del comando de build y compilación APK en Docker (timeout 600 s); `QAOrchestrator` con bucle LLM de hasta 8 rondas con 5 validaciones post-decisión y 3 guardas de presupuesto pre-LLM; 5 agentes Fase 5 (functional_tester, security_scanner, contract_validator, performance_profiler, apk_installer) con degradación controlada sin Docker; 6 agentes Fase 7-9 (functional_qa_agent con análisis en dos pasadas, boundary_qa_agent con corpus determinista, adversarial_qa_agent, security_qa_agent con checklist OWASP completo A01-A10, performance_qa_agent con umbrales deterministas + análisis LLM, regression_qa_agent con comparación semántica entre sesiones); synthesis_agent con síntesis LLM y fallback determinístico; `producer_agent` en todos los `QAFindingDetail` y filas `QAFinding` para atribución correcta; `probes` JSON en `QASession` para historial completo de sondeos; 7 evaluadores del Supervisor (`functional_qa_agent_evaluator` … `qa_session_evaluator`) con evidencia filtrada por `producer_agent == agent_name`; `QATool` integrado en Aria (situaciones A/B/C/D, `_evaluate_yes_no` con LLM, creación de Tasks de remediación); API con endpoints `POST /qa/{id}/run`, `GET /qa/{id}/sessions`, `GET /qa/{id}/sessions/{sid}`, `POST /projects/{id}/conversations/notify-qa-completed`; documentación completa en `app/services/qa/QA.md`. **Bugs críticos corregidos**: los 6 agentes Fase 7-9 estaban ausentes de todas las listas `allowed_agents` (código muerto en producción), doble encoding JSON en `findings_summary`, desalineación de constantes de veredicto (`passed_with_warnings` → `partial`) y categorías (`crash` → `usability`, `accessibility`, etc.), `accessibility_checker` fantasma en `web_app_strategy`, y descripciones de agentes Fase 7-9 ausentes del catálogo del orquestador.

- **Sistema Supervisor completo (22 evaluadores + análisis agregado)**: nueva capa de supervisión en `app/services/supervisor/` que evalúa retrospectivamente la calidad de los 22 agentes del sistema. Cada evaluador lee los trace files del proyecto (`planning_trace.jsonl`, `execution_trace.jsonl`) y los runs de ejecución de la BD, llama al LLM con el historial completo del agente, y produce un `EvaluatorOutput` con veredicto y hallazgos. El veredicto global usa media ponderada con umbrales 1.7/1.3 (no worst-case). Incluye `supervisor_runner.py` (orquestador de los 22 evaluadores), `supervisor_synthesizer.py` (síntesis cross-agent en lenguaje natural), un analizador agregado (`aggregate_runner.py`) que detecta patrones de degradación en 5–20 proyectos, y trazado histórico de prompts via `git show` para evaluar agentes contra el prompt activo en el momento del run. Resultados persistidos en `SupervisorReport` y `AgentEvaluation`. API: `POST /supervisor/projects/{project_id}/run`, `POST /supervisor/aggregate`.

- **EnvironmentManager — cobertura completa de ecosistemas**: el `EnvironmentManager` ahora soporta instalación incremental de dependencias en todos los ecosistemas del catálogo. Nuevas estrategias: `GoStrategy` (`go get` + `go mod tidy`), `RustStrategy` (`cargo add` + `cargo fetch`), `DotnetStrategy` (`dotnet add package`). `JvmStrategy` completamente reescrita: edita `pom.xml` con `xml.etree.ElementTree`, edita `build.gradle` / `build.gradle.kts` con conteo de llaves (soporta Groovy DSL y Kotlin DSL), y detecta proyectos Flutter por `pubspec.yaml` (`flutter pub add`). Los archivos de manifiesto modificados en disco (`pom.xml`, `Cargo.toml`, `go.mod`, `.csproj`, `pubspec.yaml`) se propagan a `InstallResult.manifest_files_changed` → `EnvironmentManagerOutput.manifest_files_changed` → evidence del orquestador.

- **EnvironmentManagerAgent + ErrorDiagnosis (`fault_side`)**: nuevo subagente `environment_manager_agent` que el orquestador invoca cuando `error_diagnosis.fault_side == "environment"`. Extrae los paquetes a instalar via LLM con un prompt que conoce la convención de nomenclatura de cada ecosistema (coordenadas Maven, rutas de módulo Go, nombres de crate, NuGet IDs). Registra los archivos de manifiesto como `changed_files` en evidencia. `ErrorDiagnosis` incorpora los campos `fault_side` (`"code"` | `"environment"` | `"uncertain"`) y `confidence` con valores por defecto para compatibilidad con payloads legacy.

- **Catálogo de imágenes Docker v2 (11 imágenes, selector LLM)**: reemplaza la detección por keywords por una llamada LLM estructurada que selecciona la imagen más adecuada del catálogo. Imágenes cubren Python, Node, Java, Rust, Go, .NET, Android, Flutter, React Native, y dos fullstack (py+node, java+node). Todas las imágenes llevan labels OCI (`org.opencontainers.image.*`) y `agente.catalog.*`. Script `scripts/build-catalog-images.sh` para construir y smoke-testear el catálogo completo.

- **`verification_level` en tareas atómicas**: nuevo campo `"runtime"` | `"none"` (default `"runtime"`) en `Task`. Cuando `"none"`, el orquestador nunca invoca `command_runner_agent`, eliminando loops de verificación costosos para cambios puramente estructurales en proyectos compilados (Android, Flutter, .NET, etc.). Threaded a través de `AtomicTaskOutput` → `Task` → `ExecutionRequest` → orchestrator.

- **Aria refactorizado como orquestador LLM** (`app/services/conversation/aria/`): reemplaza el God Object `project_assistant.py` (~1100 líneas) con un orquestador LLM limpio. Aria recibe un `AriaInput` (mensaje de usuario o `SystemEvent`), ejecuta un loop de hasta `MAX_STEPS=4` pasos con constraint de no-repetición por herramienta, y sintetiza la respuesta final. La `phase` pasa de router a hint contextual. El Q&A es accesible en cualquier fase. Arquitectura: `contracts.py` (tipos), `context_builder.py` (snapshot DB sin LLM), `orchestrator.py` (loop + transiciones de fase), y 6 tools (`requirements_tool`, `query_tool`, `review_tool`, `confirmation_tool`, `start_project_tool`, `resumption_tool`).
- **ReviewContext como ADT discriminada**: reemplaza los campos separados `review_task_id` + `review_subphase` + `pending_clarification_summary` por una unión discriminada `TaskReviewContext | ProjectReviewContext` (campo `kind`) serializada como JSON en `conversation.review_context`. El campo `proposed_plan` (si presente) indica que se espera confirmación del usuario — no hay sub-fase explícita.
- **Eventos de sistema como inputs de primera clase**: el workflow notifica a Aria via `AriaInput(source="system", system_event=SystemEvent(...))`. Eventos soportados: `MANUAL_REVIEW_REQUIRED`, `WORKFLOW_ERROR`, `EXECUTION_STARTED`, `PROJECT_COMPLETED`, `CONFIRM_START`. Aria aplica las transiciones de DB antes del loop LLM y sintetiza el mensaje al usuario.
- **Agente conversacional Aria (overhaul previo)**: flujo de revisión manual rediseñado sin límite de intentos; motivo de bloqueo real extraído del `ExecutionRun`; `ReviewEvaluator` enriquecido con progreso de tareas del proyecto como contexto.
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

- **1773 tests unitarios** — todos passing
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

## Agente Conversacional — Aria (`app/services/conversation/aria/`)

Aria es un **orquestador LLM** que gestiona la interacción con el usuario a lo largo de todo el ciclo de vida del proyecto. Recibe inputs de dos fuentes (mensajes del usuario y eventos del sistema), decide en un loop ligero qué herramienta invocar, y sintetiza la respuesta final en lenguaje natural.

### Arquitectura del orquestador

```
AriaInput (user | system)
    │
    ├── apply_system_event_pre_loop()   ← transiciones DB inmediatas
    │       MANUAL_REVIEW_REQUIRED  → phase=reviewing, review_context=TaskReviewContext
    │       WORKFLOW_ERROR          → phase=reviewing, review_context=ProjectReviewContext
    │       EXECUTION_STARTED       → phase=executing
    │       PROJECT_COMPLETED       → phase=completed
    │
    └── _run_loop()
          │
          ├─ [step 1..MAX_STEPS=4]
          │     _call_aria_llm() → AriaStep{action, tool_name, tool_hint, reasoning}
          │         action="call_tool" → tool.execute(db, project_id, conversation, hint)
          │                              (no-repeat: mismo tool ≤ 1 vez por turno)
          │         action="respond"  → break
          │
          └─ _apply_phase_transitions() + persist → AriaResponse
```

**Archivos:**

| Archivo | Rol |
|---|---|
| `contracts.py` | `AriaStep`, `AriaInput`, `AriaResponse`, `ToolName`, `ToolResult`, `SystemEvent`, `ReviewContext` ADT |
| `context_builder.py` | `ProjectSnapshot` — snapshot plano de DB (sin LLM) inyectado en el prompt de Aria |
| `orchestrator.py` | Loop principal, transiciones de fase, llamada al LLM, registro de mensajes en DB |
| `tools/requirements_tool.py` | Wraps `evaluate_requirements()` |
| `tools/query_tool.py` | Wraps `answer_project_query()` |
| `tools/review_tool.py` | Wraps `evaluate_review()` — funciona para revisiones por tarea y por proyecto |
| `tools/confirmation_tool.py` | Wraps `evaluate_confirmation()` |
| `tools/start_project_tool.py` | Wraps `ProjectStartService.start()` |
| `tools/resumption_tool.py` | Wraps `resume_after_review()` |

### Herramientas disponibles

| Herramienta | `ToolName` | Cuándo la usa Aria |
|---|---|---|
| `RequirementsTool` | `requirements_agent` | Durante gathering para evaluar y enriquecer el borrador |
| `QueryTool` | `query_agent` | Preguntas del usuario sobre el estado del proyecto (cualquier fase) |
| `ReviewTool` | `review_agent` | Turno de clarificación en revisión manual (fase reviewing) |
| `ConfirmationTool` | `confirmation_agent` | Cuando hay un `proposed_plan` y el usuario responde |
| `StartProjectTool` | `start_project` | Al recibir el evento `CONFIRM_START` o decisión de usuario |
| `ResumptionTool` | `resumption_agent` | Tras confirmación positiva para reanudar la ejecución |

### Fases y transiciones

```
gathering → [requirements_ready=True] → (botón Start / CONFIRM_START)
                                              ↓
                                         executing ←──────────────────┐
                                              │                        │
                          MANUAL_REVIEW_REQUIRED / WORKFLOW_ERROR      │
                                              ↓                        │
                                          reviewing                    │
                                    [review → proposed_plan]           │
                                    [confirmation → confirmed=True]    │
                                    [resumption_tool] ─────────────────┘
                                              │
                                       PROJECT_COMPLETED
                                              ↓
                                          completed
                                              │
                                    (stop request del usuario)
                                              ↓
                                           paused
```

| Fase | Hint para Aria |
|---|---|
| `gathering` | Priorizar `requirements_agent`; el Q&A sigue disponible |
| `executing` | Input bloqueado en el frontend; Aria responde con estado |
| `reviewing` | Priorizar `review_agent` → `confirmation_agent` → `resumption_agent` |
| `paused` | Sólo `query_agent` es relevante |
| `completed` | Sólo `query_agent` es relevante |

### ReviewContext — unión discriminada

El campo `conversation.review_context` serializa una de dos estructuras según el origen del bloqueo:

```python
# Tarea específica bloqueada
TaskReviewContext(kind="task", task_id, task_title, task_description, validation_notes)

# Error a nivel de proyecto (sin tarea ancla)
ProjectReviewContext(kind="project", failure_type, failure_reason)
# failure_type: "bootstrap" | "plan_empty" | "iteration_limit" | "unknown"
```

El campo `conversation.proposed_plan` (si presente) indica que Aria ya tiene un plan propuesto y espera confirmación del usuario. Su ausencia indica que aún se está recopilando información.

### Evaluadores LLM (subagentes intactos)

| Servicio | Input | Output | Invocado por |
|---|---|---|---|
| `RequirementsEvaluator` | historial + draft actual | `needs_more` / `sufficient` + draft actualizado | `RequirementsTool` |
| `ReviewEvaluator` | task/project context + episode history + task progress | `insufficient` / `ready_to_confirm` / `abandoned` | `ReviewTool` |
| `ConfirmationEvaluator` | proposed_plan + user_response | `confirmed: bool` + `follow_up` | `ConfirmationTool` |
| `ProjectQueryAgent` | task list (por estado) + user question | respuesta en lenguaje natural | `QueryTool` |
| `ImpactAssessmentAgent` | user clarification + project context | scope + `environment_changes` | `ResumptionTool` |

### Control de flujo del workflow

```python
# Pausa cooperativa
POST /conversations/pause          → request_workflow_stop(project_id)
                                    # threading.Event → loop comprueba antes de cada tarea

# Reanudación
POST /conversations/resume-workflow → clear_workflow_stop(project_id)
                                     → conversation.phase = executing
                                     → _workflow_executor.submit(...)

# Recovery de crash (en _lifespan de FastAPI)
# Al arrancar: re-encola proyectos en executing con tareas PENDING
```

### API del agente conversacional

| Método | Ruta | Descripción |
|---|---|---|
| `WS` | `/ws/projects/{project_id}/chat` | Canal principal de chat en tiempo real |
| `GET` | `/projects/{project_id}/conversations/active` | Estado actual de la conversación + historial |
| `POST` | `/projects/{project_id}/conversations/confirm-start` | Inicia el proyecto desde el botón Start de la UI |
| `POST` | `/projects/{project_id}/conversations/notify-review` | Notifica inicio de revisión manual (desde workflow) |
| `POST` | `/projects/{project_id}/conversations/notify-task-event` | Broadcast de cambio de estado de tarea |
| `POST` | `/projects/{project_id}/conversations/pause` | Solicita parada cooperativa del workflow |
| `POST` | `/projects/{project_id}/conversations/resume-workflow` | Reanuda workflow pausado o tras crash |

### Modelo de datos de conversación

```python
Conversation:
    phase: str                   # gathering | executing | reviewing | paused | completed
    requirements_ready: bool     # True cuando RequirementsTool devuelve "sufficient"
    review_context: str | None   # JSON de TaskReviewContext | ProjectReviewContext
    proposed_plan: str | None    # plan pendiente de confirmación (si presente → awaiting confirmation)
    review_episode_attempts: int # contador informativo de turnos en el episodio
    requirements_draft: str | None  # borrador acumulado durante gathering
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
- Los padres válidos tienen `planning_level ∈ {high_level, refined}`
- Produce tareas con `planning_level="atomic"`, `executor_type="pending_engine_routing"`
- Asigna `verification_level` (`"runtime"` | `"none"`) por tarea: `"none"` para cambios puramente estructurales en proyectos compilados donde la verificación en contenedor no aportaría valor
- `acceptance_criteria` es `list[str]` — cada elemento es un criterio verificable independiente (≥ 15 chars cada uno); `format_acceptance_criteria()` serializa la lista como bullet points para inyección en prompts y maneja strings legacy
- `estimated_complexity` (`XS` | `S` | `M` | `L` | `XL`) — estimación de esfuerzo de ejecución asignada por el LLM y consumida por el secuenciador para balancear batches
- `depends_on_task_titles` (`list[str]`) — dependencias inter-tarea pre-declaradas usando títulos exactos de `sibling_atomic_summary`; el secuenciador las trata como restricciones de ordenación de verdad absoluta
- Recibe `sibling_atomic_summary` (acumulado por `_run_atomic_generation_phase`) para evitar duplicados entre ramas de distintos padres
- Recibe `runtime_context` para inyectar el stack tecnológico en las tareas generadas; `proposed_solution` e `implementation_steps` deben referenciar las tecnologías concretas del stack

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
| `CandidateAtomicTask` | Snapshot de tarea atómica como input al secuenciador; incluye `ordering_hint` (setup_first/depends_on_setup/standard), `estimated_complexity` (XS–XL) y `depends_on_task_titles` (dependencias pre-declaradas por el generador) |
| `ExecutionStateSummary` | Estado de ejecución actual: tareas completadas, pendientes, fallidas |

---

## Runtime Environment System (`app/services/environment/`)

Sistema de entorno de ejecución basado en Docker. Se activa para tareas que requieren ejecución real de código.

### Flujo

```
CatalogSelector (LLM)  → selecciona imagen del catálogo según tipo de proyecto
EnvironmentPlanner     → RuntimeSpec (image, dockerfile_path, dependencies, env vars)
EnvironmentBootstrapper
  → build/pull imagen Docker (desde catálogo local o registry)
  → arrancar contenedor
  → instalar dependencias
  → smoke test
  → (si falla) repair via LLM → retry
EnvironmentSession     → proxy para comandos en el contenedor
stop_session()         → detener y eliminar contenedor
```

### Catálogo de imágenes (`app/services/environment/catalog/`)

11 imágenes pre-construidas con toolchains completos, seleccionadas automáticamente por LLM según el tipo de proyecto:

| Imagen | Tag | Casos de uso |
|---|---|---|
| `agente-python:3.12` | Python 3.12-slim + libs científicas/web | Django, FastAPI, Flask, scripts, ML |
| `agente-node:22` | Node 22 LTS | Express, NestJS, Vite, herramientas JS/TS |
| `agente-java:21` | Eclipse Temurin 21 JDK + Maven | Spring Boot, APIs REST Java |
| `agente-rust:stable` | Rust stable + musl-tools | CLIs, sistemas, WebAssembly |
| `agente-go:1.22` | Go 1.22 | Microservicios, CLIs Go |
| `agente-dotnet:8` | .NET SDK 8.0 | ASP.NET Core, apps C# |
| `agente-android:sdk34` | Android SDK 34 + JDK 17 + Gradle | Apps nativas Android (Kotlin/Java) |
| `agente-flutter:3` | Flutter 3.22 + Dart + Android SDK 34 | Apps Flutter para Android |
| `agente-react-native:0.74` | Node 22 + Android SDK 34 + JDK 17 | Apps React Native / Expo (Android) |
| `agente-fullstack:py-node` | Python 3.12 + Node 22 | Monorepos backend Python + frontend JS/TS |
| `agente-fullstack:java-node` | Java 21 + Maven + Node 22 | Monorepos Spring + frontend JS/TS |

Todas las imágenes incluyen labels OCI (`org.opencontainers.image.*`) y `agente.catalog.*` para identificación. El script `scripts/build-catalog-images.sh` construye y smoke-testea el catálogo completo o imágenes individuales.

**Runtimes soportados:** `python_venv`, `node_npm`, `rust_cargo`, `go_modules`, `dotnet`, `java_maven`, `java_gradle`, `android_gradle`, `react_native`

**Ecosistemas build-system** (Gradle, Maven, Cargo, Go, .NET, Flutter): el bootstrapper no instala dependencias de la aplicación — se declaran en los archivos del proyecto y se resuelven en el primer build. El smoke test verifica el toolchain.

### EnvironmentManager — instalación incremental

El `EnvironmentManager` (`manager.py`) instala paquetes de forma incremental en un contenedor en ejecución, sin teardown. Se invoca desde:
- `EnvironmentManagerAgent`: cuando el orquestador diagnostica `fault_side == "environment"` durante una tarea
- `_apply_environment_delta()` en `ResumptionService`: cuando el `ImpactAssessmentAgent` detecta `environment_changes` en la clarificación del usuario

**Estrategias por ecosistema (`manager_strategies/`):**

| Estrategia | runtime_type | Mecanismo |
|---|---|---|
| `PythonStrategy` | `python_venv`, `fullstack_py_node` | `pip install` con fallback sin versión |
| `NodeStrategy` | `node_npm`, `react_native` | `npm install` con fallback sin versión |
| `GoStrategy` | `go` | `go get pkg@vX.Y.Z` + `go mod tidy`; requiere `go.mod` en workspace |
| `RustStrategy` | `rust_cargo` | `cargo add pkg@version` + `cargo fetch`; requiere `Cargo.toml` |
| `DotnetStrategy` | `dotnet` | `dotnet add "x.csproj" package Pkg --version V`; busca `.csproj`/`.fsproj` |
| `JvmStrategy` | `java_maven`, `java_gradle`, `android_gradle` | Maven: edita `pom.xml` con `xml.etree.ElementTree` → `mvn dependency:resolve`; Gradle: edita `build.gradle[.kts]` con conteo de llaves → `./gradlew dependencies`; Flutter: `flutter pub add` (detectado por `pubspec.yaml`) |
| `GenericStrategy` | (fallback) | `pip install` genérico |

Los archivos de manifiesto modificados en disco (`pom.xml`, `Cargo.toml`, `go.mod`, `.csproj`, `pubspec.yaml`) se registran en `InstallResult.manifest_files_changed` y propagados hasta `EnvironmentManagerOutput.manifest_files_changed`. El `EnvironmentManagerAgent` los añade a `evidence.changed_files` para que los validadores y el orquestador los detecten como cambios de repositorio.

**Políticas de versión:**
- `exact_only` — falla si la versión exacta no está disponible; escala a `needs_user_input`
- `preferred` — intenta la versión solicitada; instala la última disponible si falla
- `any_compatible` — instala directamente la última versión compatible (modo del orquestador)

### Componentes

| Archivo | Rol |
|---|---|
| `catalog/selector_client.py` | Llamada LLM que selecciona la imagen del catálogo más adecuada para el proyecto |
| `catalog/registry.py` | Registro de las 11 `CatalogEntry` con metadatos de imagen y Dockerfile |
| `planner.py` | Llama al selector de catálogo, luego al LLM planificador; fusiona el hint del catálogo en el `RuntimeSpec` |
| `planner_client.py` | Prompts del planificador; inyecta el hint del catálogo cuando hay match |
| `bootstrapper.py` | Arranca el contenedor, instala dependencias, valida con smoke test, repara si falla |
| `docker_driver.py` | Wrapper sobre la SDK de Docker: pull, build local, run, exec, stop/remove |
| `validator.py` | Ejecuta el smoke test y decide si el entorno está listo |
| `session_store.py` | Persiste y recupera `EnvironmentSession` activas por proyecto |
| `contracts.py` | Tipos compartidos: `RuntimeSpec`, `EnvironmentSession`, `EnvironmentCommandResult` |
| `manager.py` | `EnvironmentManager`: selección de estrategia, loop de instalación, rollback, actualización del `RuntimeSpec` en memoria |
| `manager_contracts.py` | `EnvironmentManagerRequest`, `EnvironmentManagerOutput`, `PackageRequest`, `PackageInstallation` |
| `manager_strategies/` | Estrategias por ecosistema: `python`, `node`, `go`, `rust`, `dotnet`, `jvm`, `generic` |

### Tipos clave

| Tipo | Descripción |
|---|---|
| `RuntimeSpec` | Especificación del entorno: `runtime_type`, `image`, `dockerfile_path`, `dependencies`, `environment_variables` |
| `EnvironmentSession` | Sesión activa: `project_id`, `container_id`, `project_root`, `runtime_type` |
| `RuntimeEnvironmentPlanOutput` | Output del LLM: imagen, dependencias, vars de entorno (lista de `EnvVar`) |
| `EnvVar` | Par `key`/`value` para variables de entorno (compatible con OpenAI Structured Outputs) |
| `CatalogEntry` | Entrada del catálogo: `image_name`, `runtime_type`, `description`, `dockerfile` |
| `CatalogSelectionOutput` | Output del selector LLM: `selected_image` (nullable) + `reasoning` |

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
| `context_selection_agent` | Selecciona tareas históricas completadas relevantes como contexto (fase discovery) |
| `code_change_agent` | Materializa cambios de código y de aplicación (create/modify files) |
| `command_runner_agent` | Ejecuta y verifica comandos shell en dos fases: selección de archivos → planificación del comando |
| `document_writer_agent` | Produce documentación y artefactos de diseño: Markdown, YAML/JSON (OpenAPI, AsyncAPI), RST, AsciiDoc, diagramas como código (PlantUML, Mermaid) |
| `test_builder_agent` | Escribe ficheros de test basándose en los `acceptance_criteria` de la tarea; evalúa la cobertura en una fase separada e informa de gaps |
| `environment_manager_agent` | Instala paquetes faltantes diagnosticados como `fault_side=="environment"`; invoca `EnvironmentManager`, persiste `RuntimeSpec`, registra archivos de manifiesto modificados en evidencia |

Cada subagente recibe `(ExecutionRequest, ExecutionStep, ResolutionState)` y retorna un `ResolutionState` actualizado.

**`environment_manager_agent`** es un agente de infraestructura, no un productor de entregables: está incluido en `IGNORED_VALIDATION_PRODUCERS` para que su evidencia no pase a los validadores. Si falla, el orquestador lo trata como terminal (`SubagentRejectedStepError` → `EXECUTION_DECISION_FAILED`) en lugar de entrar en un loop de reparación.

**`ErrorDiagnosis`** (`app/execution_engine/error_diagnosis.py`): los campos `fault_side` (`"code"` | `"environment"` | `"uncertain"`) y `confidence` (`"low"` | `"medium"` | `"high"`) guían la decisión del orquestador sobre si invocar `environment_manager_agent` o `code_change_agent`. Ambos campos tienen valores por defecto para compatibilidad con payloads sin estos campos.

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

## Sistema Supervisor (`app/services/supervisor/`)

Capa de meta-evaluación post-ejecución que analiza retrospectiamente la calidad de los 22 agentes del sistema para un proyecto dado. No interfiere con la ejecución; corre bajo demanda vía `POST /supervisor/projects/{project_id}/run`.

### Arquitectura

```
POST /supervisor/projects/{project_id}/run
    │
    └── supervisor_runner.run_supervisor()
          │
          ├── [por cada uno de los 22 evaluadores]
          │     evaluator.evaluate(db, project_id, project_name, …, system_version)
          │         → EvaluatorOutput(result, execution_run_ids_analyzed, system_versions_seen)
          │         result=None  → agente not_supervised (no fue llamado en este proyecto)
          │         result=...   → verdict + findings + issues + suggestions
          │
          ├── _overall_verdict(verdicts)
          │     ├── >30% not_supervised → "not_evaluated"
          │     └── media ponderada (healthy=2, needs_attention=1, degraded=0)
          │           ≥1.7 → "healthy" | ≥1.3 → "needs_attention" | <1.3 → "degraded"
          │
          ├── synthesize(...)  → texto narrativo cross-agent
          │
          └── SupervisorReport + 22 AgentEvaluation → persistidos en BD
```

**Análisis agregado** (`aggregate_runner.py`): `POST /supervisor/aggregate` analiza 5–20 proyectos con filtros opcionales (versión, fecha, IDs) y produce un `AggregateReport` con tabla de frecuencias de veredictos por agente y una síntesis de patrones transversales.

### Componentes

| Archivo | Rol |
|---|---|
| `supervisor_runner.py` | Orquesta los 22 evaluadores, calcula `overall_verdict`, llama al sintetizador |
| `supervisor_synthesizer.py` | Genera síntesis narrativa cross-agent via LLM |
| `contracts.py` | `EvaluatorOutput`, `AgentEvaluationOutput` |
| `prompt_resolver.py` | Resuelve el prompt histórico del agente via `git show` |
| `trace_writer.py` | Escribe entradas en `planning_trace.jsonl` / `execution_trace.jsonl` |
| `aggregate_filter.py` | Filtra `SupervisorReport`s por versión, fechas, IDs de proyecto |
| `aggregate_builder.py` | Construye tabla de frecuencias y corpus de texto para el LLM |
| `aggregate_runner.py` | Orquesta el análisis multi-proyecto |
| `aggregate_synthesizer.py` | Genera síntesis agregada via LLM |
| `evaluators/` | 22 evaluadores independientes, uno por agente del sistema |

---

## Motor QA (`app/services/qa/`)

Sistema de aseguramiento de calidad adversarial autónomo que se activa cuando el proyecto alcanza estado `COMPLETED`. Referencia completa en `app/services/qa/QA.md`.

### Arquitectura

```
QATool (Aria)  ──►  QARunner (hilo de fondo)
                        │
                        ▼ QARequest
                    QAEngine
                      ├─ select(product_type)  → QAStrategy
                      ├─ QABootstrapper        → compilar APK (si mobile_android)
                      ├─ QAOrchestrator        → bucle LLM de sondeo + síntesis
                      └─ _persist_result()     → QASession + QAFinding en BD
```

### Estrategias por tipo de producto (`app/services/qa/strategies/`)

Definen qué agentes están permitidos y si se requiere artefacto compilado:

| Estrategia | Agentes Fase 5 | Agentes Fase 7-9 |
|---|---|---|
| `rest_api` | functional_tester, security_scanner, contract_validator, performance_profiler | Los 6 |
| `graphql_api` | functional_tester, security_scanner, contract_validator | 5 (sin performance_qa_agent) |
| `web_app` | functional_tester, security_scanner, performance_profiler | Los 6 |
| `cli_tool` | functional_tester, security_scanner | 5 (sin security_qa_agent) |
| `library` | functional_tester, security_scanner, contract_validator | 4 (sin security_qa_agent, sin performance_qa_agent) |
| `mobile_android` | apk_installer, functional_tester, security_scanner | 5 (sin performance_qa_agent) |
| `desktop_app` | functional_tester, security_scanner | 4 (sin security_qa_agent, sin performance_qa_agent) |

### Agentes (`app/services/qa/agents/` y `app/services/qa/qa_agents/`)

**Fase 5 — nivel ejecución (requieren Docker; degradan a `skipped` si no disponible):**

| Agente | Tipo de sondeo | Qué hace |
|---|---|---|
| `functional_tester` | `functional_test` | Ejecuta la suite de tests en Docker; análisis LLM de corrección |
| `security_scanner` | `security_scan` | Análisis estático LLM libre contra OWASP Top 10 |
| `contract_validator` | `contract_validation` | Valida OpenAPI/GraphQL contra la implementación |
| `performance_profiler` | `performance_profile` | Comandos de temporización en Docker + análisis de cuellos de botella |
| `apk_installer` | `apk_install` | Instala y smoke-testea APK Android en emulador ADB |

**Fase 7-9 — análisis estructurado puro (no requieren Docker):**

| Agente | Tipo de sondeo | Diseño |
|---|---|---|
| `functional_qa_agent` | `functional_analysis` | Dos pasadas: generación de escenarios → evaluación por escenario |
| `boundary_qa_agent` | `boundary_analysis` | Corpus determinista de borde por tipo de producto; el LLM solo identifica fallos |
| `adversarial_qa_agent` | `adversarial_analysis` | Genera y evalúa ataques específicos al producto con evidencia de código |
| `security_qa_agent` | `owasp_checklist` | Checklist OWASP Top 10 (2021) A01-A10 en una sola llamada LLM batch |
| `performance_qa_agent` | `performance_analysis` | Umbrales deterministas en Docker + análisis LLM de bottlenecks en fuente |
| `regression_qa_agent` | `regression_analysis` | Comparación semántica con la sesión anterior; saltado en la primera ejecución |
| `synthesis_agent` | `synthesis` | Sintetiza veredicto final y lista de remediación; fallback determinístico |

### Presupuesto del orquestador

```
max_probe_rounds   = 8      # rondas LLM máximas en Fase 1
timeout_seconds    = 300    # timeout de pared para el sondeo
max_findings       = 20     # para automáticamente al acumular N hallazgos
max_probes_per_agent = 5    # llamadas máximas por agente individual
```

### Integración con Aria — flujo QA

```
COMPLETED → qa_offer_pending=True
  └─ usuario acepta → QATool A2 → QASession(running) + run_qa_background()
       └─ (fondo) QAEngine → hallazgos → _notify_aria()
            └─ pending_qa_report = QAResult JSON
            └─ evento QA_COMPLETED → Aria → WebSocket broadcast
  └─ usuario ve resumen → acepta remediación → Task rows creados
```

### Evaluadores del Supervisor para QA

7 evaluadores independientes en `app/services/supervisor/evaluators/`:
- `functional_qa_agent_evaluator`, `boundary_qa_agent_evaluator`, `adversarial_qa_agent_evaluator`
- `security_qa_agent_evaluator`, `performance_qa_agent_evaluator`, `regression_qa_agent_evaluator`
- `qa_session_evaluator` (calidad global de la sesión)

La evidencia de cada evaluador de agente está filtrada por `producer_agent == agent_name` en la consulta a BD. El `qa_session_evaluator` recibe todos los hallazgos y todos los probes sin filtro.

---

## Sistema de versionado

El sistema mantiene **dos dimensiones de versionado** ortogonales que trabajan conjuntamente para garantizar trazabilidad completa, en especial en el Supervisor.

### 1. Versión del sistema — `system_version` (por `ExecutionRun`)

Cada `ExecutionRun` registra el commit de git activo en el momento de su creación:

```python
# app/core/git_utils.py
get_system_version()  # → "<sha40>"  si el working tree está limpio
                       # → "<sha40>-dirty"  si hay cambios sin commitear
                       # → "unknown"  si git no está disponible
```

Llamado automáticamente en `create_execution_run()`. El valor se persiste en `ExecutionRun.system_version`.

**Formatos reconocidos por el Supervisor:**

| Valor | Descripción | Resoluble para `git show` |
|---|---|---|
| `"abc1234…"` (40 hex) | Hash limpio | ✅ Sí |
| `"abc1234…-dirty"` | Árbol con cambios; se elimina el sufijo | ✅ Sí (best-effort) |
| `"unknown"` | Git no disponible | ❌ No (usa prompt actual) |

**Flujo en el Supervisor:**

```
supervisor_runner._get_system_version(db, project_id)
    → ExecutionRun más reciente con system_version IS NOT NULL
    → pasa el hash a cada evaluator.evaluate(system_version=...)
    → cada evaluador llama a resolve_system_prompt(agent_name, system_version=...)
```

**Usos en el análisis agregado:**
- `_filter_by_version`: incluye sólo los `SupervisorReport` donde alguna `AgentEvaluation.system_versions_seen` contiene la versión solicitada.
- `_is_dirty_report`: un informe es "sucio" cuando ninguna evaluación tiene un hash limpio — los informes sucios se excluyen de las comparaciones por versión.

### 2. Versionado de prompts — `version` en los ficheros YAML

Cada fichero YAML de prompt tiene su propio número de versión semántico independiente del git commit:

```yaml
agent_name: mi_agente
version: "1.2.0"          # MAJOR.MINOR.PATCH — fuente de verdad del prompt
changelog:
  - version: "1.2.0"
    date: "2026-05-30"
    changes: "Añadido campo X al user prompt"
  - version: "1.1.0"
    date: "2026-05-20"
    changes: "Refinada descripción de criterios de evaluación"
  - version: "1.0.0"
    date: "2026-05-01"
    changes: "Versión inicial"
```

**Regla de incremento:**

| Cambio | Tipo |
|---|---|
| Editar el texto de `content:` | PATCH o MINOR según magnitud |
| Añadir / eliminar un `user_prompt_inputs` | MINOR |
| Reescritura completa o cambio de rol del agente | MAJOR |
| Sólo editar metadatos (`description:` a nivel de prompt) | No incrementar |

El `PromptLoader` expone `get_version("nombre_agente")` y `get_spec("nombre_agente")` para consultar la versión activa en proceso.

### 3. Resolución histórica de prompts — `prompt_resolver.py`

El Supervisor usa el `system_version` del `ExecutionRun` para evaluar al agente **contra el prompt que tenía activo en ese momento**, no contra el prompt actual. Esto garantiza equidad: si el prompt cambió después del run, el evaluador ve la guía que el agente realmente recibió.

```python
# app/services/supervisor/prompt_resolver.py
resolve_system_prompt(agent_name, prompt_key="main", system_version="abc1234…")
```

**Algoritmo:**
1. Si `system_version` es un hash resoluble → `git show <commit>:app/prompts/<path>` → parsea el YAML histórico → extrae `content` de la clave pedida.
2. Si git falla o la versión es `"unknown"` → `prompt_loader.get(agent_name, prompt_key)` (prompt actual, best-effort).

**Registro de agentes** (`_AGENT_YAML_PATHS`): el diccionario en `prompt_resolver.py` mapea cada `agent_name` a su ruta relativa dentro de `app/prompts/`. Todo agente nuevo **debe registrarse aquí** para que la resolución histórica funcione.

### Cómo interactúan las dos dimensiones

```
ExecutionRun creado en commit abc1234
    → system_version = "abc1234"
    → prompt planner.yaml version "1.1.0" activo en abc1234

[semanas después] prompt planner.yaml actualizado a "1.2.0"

Supervisor evaluates planner para ese proyecto:
    → resolve_system_prompt("planner", system_version="abc1234")
    → git show abc1234:app/prompts/planning/planner.yaml
    → obtiene version "1.1.0" content  ← el prompt que el agente recibió
    → evaluador juzga el comportamiento contra "1.1.0", no "1.2.0" ✓
```

Esto garantiza que un cambio de prompt no distorsione retroactivamente los informes del Supervisor para runs anteriores.

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
                  → superseded   (terminal: reemplazado por replan disruptivo)
pending → cancelled (terminal: eliminado por decisión del usuario durante revisión)
            └─ cascada hacia padres: padre → cancelled sólo si TODOS sus hijos
               son cancelled y el padre nunca produjo trabajo completado
```

`TERMINAL_TASK_STATUSES = {partial, completed, failed, reatomized, followed_up, superseded, cancelled}`

**Semántica de estados terminales:**

| Estado | Significado | `completion_rate` |
|---|---|---|
| `completed` | Tarea ejecutada y validada con éxito | ✅ Cuenta como éxito |
| `partial` | Tarea ejecutada con resultado incompleto | ❌ No cuenta |
| `failed` | Tarea ejecutada con fallo | ❌ No cuenta |
| `reatomized` | Tarea reemplazada por sub-tareas (recovery) | ✅ Cuenta como éxito |
| `followed_up` | Tarea seguida por tareas adicionales (recovery) | ✅ Cuenta como éxito |
| `superseded` | Tarea reemplazada por replan disruptivo completo | — Excluida del denominador |
| `cancelled` | Tarea eliminada por decisión del usuario | — Excluida del denominador |

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
| `review_episode` | `resumption_tool` / `aria_orchestrator` — episodio completo de revisión manual con scope, contadores de mutación y outcome |

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

### Motor QA

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/qa/{project_id}/run` | Inicia una ejecución QA en background; retorna `qa_session_id` inmediatamente |
| `GET` | `/qa/{project_id}/sessions` | Lista historial de sesiones QA del proyecto (últimas 10, más reciente primero) |
| `GET` | `/qa/{project_id}/sessions/{qa_session_id}` | Detalle de una sesión QA con todos sus `QAFinding` |
| `POST` | `/projects/{project_id}/conversations/notify-qa-completed` | Endpoint interno para runners externos: enruta evento QA_COMPLETED a Aria + WebSocket |

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
TEST_AGENT_PROVIDER=...          # test_builder_agent (si no se define, hereda openai_model)
TEST_AGENT_MODEL=...             # ej. gpt-5.2
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

**1773 tests unitarios + 12 tests de integración — todos passing.**

| Área | Archivo(s) |
|---|---|
| Task execution service | `test_task_execution_service.py`, `test_task_execution_invariants.py`, `test_task_execution_validation_flow.py` |
| Validation | `test_validation_service.py`, `test_code_change_agent_validator.py`, `test_command_runner_agent_validator.py`, `test_aggregation.py` |
| Orchestrator + engine | `test_execution_engine.py`, `test_command_runner_agent_subagent.py`, `test_command_tool.py` |
| ErrorDiagnosis + env_manager | `test_phase6_error_diagnosis.py` |
| Recovery | `test_recovery_service.py`, `test_task_hierarchy_service.py` |
| Post-batch | `test_post_batch_service.py`, `test_post_batch_service_problematic_outcomes.py`, `test_post_batch_decision_service.py` |
| Live plan mutation | `test_live_plan_mutation_service.py` |
| Execution plan patch | `test_execution_plan_patch_service.py` |
| Recovery assignment compiler | `test_recovery_assignment_compiler_service.py` |
| Evaluación | `test_evaluation_service.py`, `test_stage_evaluation_output.py`, `test_evaluation_schema.py` |
| Project workflow | `test_project_workflow_service.py` |
| Workspace runtime | `test_local_workspace_runtime.py` |
| Execution plan service | `test_execution_plan_service.py` |
| Aria — contratos y context builder | `aria/test_contracts.py`, `aria/test_context_builder.py` |
| Aria — orquestador | `aria/test_orchestrator.py` |
| Aria — tools | `aria/tools/test_requirements_tool.py`, `aria/tools/test_review_tool.py`, `aria/tools/test_confirmation_tool.py`, `aria/tools/test_query_tool.py`, `aria/tools/test_start_project_tool.py`, `aria/tools/test_resumption_tool.py` |
| Environment — manager | `test_environment_manager.py`, `test_manager_strategies.py` |
| Environment (integración) | `tests/integration/test_environment_integration.py` |
| API — proyectos | `test_projects.py` |
| API — WebSocket + REST Aria | `test_aria_ws.py` |
| Supervisor — runner y veredicto global | `supervisor/test_supervisor_runner.py` |
| Supervisor — PlannerEvaluator | `supervisor/test_planner_evaluator.py` |
| Supervisor — análisis agregado | `supervisor/test_aggregate_runner.py`, `supervisor/test_aggregate_filter.py`, `supervisor/test_aggregate_builder.py` |
| QA — orquestador y transiciones Aria | `qa/test_qa_orchestrator.py`, `services/conversation/aria/test_qa_orchestrator.py` |
| QA — runner y persistencia | `qa/test_qa_runner.py` |
| QA — engine | `qa/test_qa_engine.py` |
| QA — bootstrapper | `qa/test_qa_bootstrapper.py` |
| QA — estrategias | `qa/test_qa_strategies.py` |
| QA — agentes Fase 5 | `qa/test_qa_agents.py` |
| QA — agentes Fase 7-9 | `qa/test_new_qa_agents.py` |
| QA — end-to-end | `qa/test_qa_e2e.py` |
| Supervisor — evaluadores QA | `supervisor/test_qa_evaluators.py` |
| Mejoras de generación — acceptance criteria (Fase 3) | `test_acceptance_criteria_phase3.py` |
| Mejoras de generación — estimated_complexity + depends_on_task_titles (Fases 5B/5C) | `test_phase5bc_complexity_and_deps.py` |
| Mejoras de generación — sibling accumulator (Fase 2) | `test_sibling_atomic_accumulator.py` |
| Mejoras de generación — runtime context injection (Fase 1) | `test_runtime_context_injection.py` |
| Eliminación de tareas — cancelación en cascada | `conversation/test_resumption_service.py` (casos de cascada) |
| Eliminación de tareas — reintegración del plan (Fases 6-7) | `conversation/test_resumption_reintegration.py` |
| Task hierarchy — CANCELLED en consolidación de padres | `test_task_hierarchy_service.py` (casos de cancelled) |
| Supervisor — planner con cancelled_count | `supervisor/test_planner_evaluator.py` (casos de cancelled) |

---

## Próximos pasos propuestos

### Alta prioridad

**1. Mejoras de calidad al agente conversacional Aria**
Seis mejoras de prioridad media-alta ya diseñadas, ordenadas de menor a mayor riesgo:

- **(P1) Subir `MAX_STEPS` de 4 a 6** — el camino crítico REVIEW→CONFIRMATION→RESUMPTION→RESPOND consume 4 pasos; si el usuario lanza una pregunta a la vez, el loop se agota. Cambio de 1 línea en `orchestrator.py`.
- **(P2) `requirements_draft` en el snapshot + historial tiered** — el borrador de requisitos de GATHERING desaparece del prompt cuando el historial supera 40 mensajes. Los mensajes de sistema (review openings) nunca deben caer por truncado; sólo los mensajes user/assistant son candidatos al recorte.
- **(P3) `QueryAgent` con detalles de error en tareas fallidas** — cuando el usuario pregunta "¿por qué falló X?", Aria no puede dar más que "falló". `ExecutionRun` tiene `validation_notes`, `blockers_found` y `error_message`; basta pasarlos al `QueryAgent`.
- **(P4) `ConfirmationEvaluator` con historial del episodio** — si el usuario dice "sí, pero cambia lo del endpoint que mencionaste", el evaluador no puede resolver la referencia sin ver el historial del episodio de revisión.
- **(P5) Detección de idioma centralizada** — tres mecanismos dispares hoy: heurístico Python en `RequirementsEvaluator`, auto-detección en los demás evaluadores, "responde en el idioma del último mensaje" en Aria. Centralizar en `language_utils.py` y propagar a todos los evaluadores con `OUTPUT LANGUAGE: {language}`.
- **(P6) Fase transitoria `REPLANNING`** — durante el replan disruptivo, `conversation.phase` sigue siendo `reviewing`, por lo que un segundo mensaje concurrente puede re-ejecutar `review_agent` sobre un contexto stale. Añadir `CONVERSATION_PHASE_REPLANNING` y hacer pre-commit antes de invocar `ProjectStartService`.

**2. Frontend para el Motor QA — visualización de hallazgos**
Las sesiones QA y sus hallazgos se persisten en BD pero no hay ninguna vista en el frontend. Añadir una pestaña "Calidad" con: veredicto badge (passed/partial/failed/blocked), resumen por severidad, lista de hallazgos con filtros, y botón para nueva sesión. Los endpoints ya existen (`GET /qa/{id}/sessions`, `GET /qa/{id}/sessions/{sid}`).

**3. Frontend para el Supervisor — visualización de informes de salud**
Los informes del Supervisor se generan y persisten en BD pero no hay endpoint `GET` ni vista. Añadir: `GET /supervisor/projects/{project_id}/reports` y `GET /supervisor/reports/{report_id}`. Pestaña "Salud del sistema" con veredicto global, síntesis narrativa, y evaluaciones por agente (los `not_supervised` deben distinguirse visualmente de los `healthy`).

**4. Ejecución paralela de los evaluadores del Supervisor**
El `supervisor_runner.py` ejecuta los 22 evaluadores secuencialmente. Todos son independientes → `ThreadPoolExecutor` reduciría el tiempo de N × latencia_LLM a ~1 × latencia_LLM. El runner ya gestiona errores por evaluador de forma aislada.

**5. Soporte multi-stage**
El sistema ejecuta un único stage por proyecto. Para proyectos con múltiples stages secuenciales ("backend" → "frontend" → "integración"), el `ProjectWorkflowService` necesita un loop externo que cierre el stage actual, genere el siguiente y reutilice el contexto acumulado. Las bases ya están: `project_stage_closed`, `stage_goal` y `ProjectOperationalContext` son stage-aware.

**6. Validación E2E de cambios de prompt**
Los cambios de prompt de convergencia, AC prohibidos, repair pass, retry adaptation y calibración de confianza deben validarse empíricamente. Protocolo: ejecutar 3–5 proyectos equivalentes con prompts anteriores vs. nuevos, comparar informes del Supervisor, confirmar reducción de comportamientos degradados.

### Media prioridad

**7. Trigger automático del QA tras `PROJECT_COMPLETED`**
Para proyectos con `auto_qa=True`, el runner podría dispararse automáticamente al recibir el evento `PROJECT_COMPLETED`. Añadir el campo `auto_qa` al modelo `Project` y la lógica de disparo en el manejador de Aria.

**8. Remediación QA automática sin confirmación del usuario**
Cuando todos los hallazgos tienen `auto_remediable=True`, el `QATool` podría crear las Tasks directamente sin la oferta de confirmación. Cortocircuito en `_summarize_qa_result()` con `status="remediation_auto_confirmed"`.

**9. Pre-fetch de dependencias en el bootstrapper para ecosistemas compilados**
Cuando `code_change_agent` escribe manifests (`pom.xml`, `Cargo.toml`, `go.mod`, `.csproj`, `pubspec.yaml`), el bootstrapper del siguiente run no ejecuta pre-fetch. Añadir una fase opcional en `bootstrapper.py` para descargar dependencias antes del smoke test.

**10. Routing de modelo para Aria, evaluadores conversacionales y agentes QA**
Añadir variables `ARIA_MODEL`, `ARIA_PROVIDER`, `QA_AGENT_MODEL`, `QA_AGENT_PROVIDER` para enrutar capas a modelos distintos del motor de ejecución, igual que ya existe `VALIDATOR_MODEL`.

**11. Precisión del validador en decisiones parciales**
El `command_runner_agent_validator` puede declarar `partial` incluso cuando los tests pasan. Ampliar la lista de markers en `_task_looks_like_executable_implementation` o relajar el criterio de normalización para tareas con `exit_code=0`.

**12. Observabilidad estructurada del pipeline completo**
Estandarizar los campos de log (`batch_id`, `project_id`, `mutation_kind`, `intent_type`, `recovery_task_ids`, `followup_depth`, `guard_triggered`, `aria_tool_called`, `qa_verdict`, `qa_findings_count`) en todo el pipeline. Incluir telemetría del loop de Aria y del orquestador QA.

**13. Tests de integración end-to-end del flujo conversacional**
Tests que cubran el flujo completo con WebSocket real: gathering → Start → executing → revisión manual con eliminación de tareas → confirmación → reanudación. Los tests actuales mockean el LLM pero no prueban el ciclo completo con WebSocket y persistencia en BD.

### Baja prioridad

**14. Panel de complejidad del plan de ejecución en el frontend**
Mostrar `estimated_complexity` por tarea: badge de color (XS=verde, S=azul, M=amarillo, L=naranja, XL=rojo) junto al título de cada atómica. En la vista del batch actual, indicar la carga agregada (suma ponderada) para identificar visualmente batches sobrecargados. Los datos ya están en BD.

**15. Historial comparativo de sesiones QA en el frontend**
Evolución de hallazgos entre sesiones QA sucesivas: tendencia de `high`+`critical` en el tiempo, hallazgos nuevos vs. resueltos por sesión (via `regression_qa_agent`), duración por sesión.

**16. Nuevo executor type: generación de media**
Para proyectos con assets (sprites, iconos, sonidos), añadir `executor_type="image_generation"` / `"audio_generation"` que llame APIs generativas externas sin Docker. El `atomic_task_generator` emitiría estas tareas; el orquestador las delegaría a un `MediaGenerationAgent`.

**17. Métricas de ejecución por proyecto**
Tasa de recovery por acción, tasa de replan, distribución de `mutation_kind` por batch, frecuencia de episodios de revisión, tiempo medio hasta confirmación, tasa de tareas canceladas por proyecto. Datos para ajustar parámetros de orquestación y detectar proyectos problemáticos antes del iteration limit.

**18. Contexto estructural del repositorio en `context_selection_agent`**
El agente selecciona tareas históricas pero no tiene visibilidad del layout actual del repo. Añadir un snapshot ligero de la estructura del workspace para mejorar la relevancia del contexto histórico.

**19. Mantenimiento del catálogo de imágenes Docker**
Automatizar actualizaciones de versiones base (Node LTS, Python minor, Flutter stable), política de versionado en el catálogo, y estrategia de publicación en un registry privado para evitar builds locales en cada máquina.

---

## Invariantes del sistema

| Área | Invariante |
|---|---|
| Ejecución | `finish` requiere evidencia; `reject` es salida válida; budget exhaustion → `completed` para salvaguardar trabajo parcial; `verification_level="none"` cortocircuita `command_runner_agent` en el orquestador |
| Validación | Validadores independientes entre sí; agregación determinista |
| Persistencia | 1 run → 1 artifact; artifact contiene la verdad final |
| Workspace | Aislamiento total entre runs; `run/` siempre eliminado; promoción controlada |
| Plan | El checkpoint final siempre incluye `stage_closure` |
| Recovery | `is_recovery_task=True` bloquea `reatomize`; `followup_depth >= 2` bloquea `insert_followup`; ≥1 sibling recovery fallado bloquea nuevo followup |
| Jerarquía | Propagación determinista; rollback si falla algún paso; sin efectos parciales sobre padres; padre → `COMPLETED` si hay trabajo productivo aunque el resto de hijos sea `CANCELLED`/`SUPERSEDED`; padre → `CANCELLED` sólo cuando TODOS sus hijos son `CANCELLED` y el padre no tiene trabajo completado |
| Descomposición | `MAX_ATOMIC_TASKS_PER_PARENT = 8`; `MAX_IMPLEMENTATION_STEPS_PER_ATOMIC = 20` |
| Entorno | Smoke test obligatorio antes de usar el contenedor; repair automático con LLM ante fallo de bootstrap; selección de imagen via LLM con fallback a imagen libre si ninguna del catálogo encaja |
| EnvironmentManager | `environment_manager_agent` no produce entregables validables (en `IGNORED_VALIDATION_PRODUCERS`); su fallo es terminal (no entra en loop de reparación); `exact_only` + conflicto de versión → `needs_user_input`; archivos de manifiesto modificados en disco siempre se registran en evidencia independientemente del éxito de la instalación |
| Aria loop | `MAX_STEPS = 4`; misma herramienta ≤ 1 vez por turno (no-repeat); si el loop se agota sin `respond`, se fuerza una respuesta de fallback; los eventos de sistema aplican transiciones DB antes del loop LLM |
| ReviewContext | `conversation.proposed_plan != None` ↔ estado "esperando confirmación"; `conversation.review_context` serializa el ADT completo (`TaskReviewContext` \| `ProjectReviewContext`); se limpia al resolver la revisión |
| Supervisor | `result=None` (not_supervised) si el agente nunca fue llamado en el proyecto; los evaluadores de validador (`code_change_agent_validator_evaluator` et al.) DEBEN tener `_AGENT_NAME` igual al **ejecutor** (ej. `"code_change_agent"`), NO al validador — `build_pair_evaluation_context()` filtra por `execution_agent_sequence` donde los validadores nunca aparecen; `_VALIDATOR_NAME` sigue siendo el nombre del validador para extraer su resultado del artefacto; veredicto global = media ponderada, no worst-case; >30% not_supervised → `not_evaluated` antes de calcular la media; `planner_evaluator.completion_rate` excluye tareas `cancelled` del denominador — cancelaciones por decisión del usuario no distorsionan la métrica de calidad del planificador; artefacto `review_episode` incluye `tasks_eliminated_count` para que el evaluador calibre scope contra eliminaciones reales |
| test_builder_agent | escribe una entrada en `execution_trace.jsonl` por cada llamada materializada o bloqueada (`call_type="materialise"` \| `"needs_dependency"`); la entrada incluye `files_written[]`, `coverage_summary` completo, y `needs_dependency` cuando aplica; `target_paths` DEBE estar poblado por el orquestador en toda llamada — un `target_paths` vacío es un error de routing |
| Motor QA — orquestador | Guardas de presupuesto (timeout, max_rounds, max_findings) se evalúan ANTES de cada llamada LLM; tras la decisión, el orden de validación es fijo: synthesis_agent excluido → no repetición → estrategia → límite por agente → registrado; synthesis_agent nunca participa en el bucle de sondeo |
| Motor QA — agentes | `session.record_agent_call(self.name)` es obligatorio al inicio de cada `probe()`; `session.add_probe(ProbeRecord(...))` es obligatorio antes de retornar; los agentes nunca lanzan excepciones por fallos de sondeo esperados (retornan `outcome="skipped"`); `producer_agent=self.name` en todos los `QAFindingDetail` emitidos |
| Motor QA — persistencia | `findings_summary` se almacena como dict JSON (sin `json.dumps` adicional); veredicto `blocked` en `QAResult` → `QASession.status = BLOCKED`; cualquier otro veredicto → `status = COMPLETED`; `_persist_result` nunca lanza — sus errores se capturan con `logger.warning` |
| Motor QA — estrategias | Todo agente nuevo DEBE añadirse a `allowed_agents` de todas las estrategias relevantes o nunca será llamado por el orquestador (bug crítico histórico) |
| Motor QA — Supervisor QA | Los evaluadores de agente QA filtran hallazgos por `producer_agent == agent_name` en la consulta BD; el `qa_session_evaluator` recibe TODO sin filtro; guardia `not_supervised` (`result=None`) cuando `ctx["sessions"]` está vacío |
