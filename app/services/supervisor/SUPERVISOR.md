# Sistema Supervisor

El supervisor es una capa de meta-evaluación que se ejecuta **después** de que los proyectos terminan su ejecución. Audita cada agente involucrado en un proyecto — planificación, ejecución, validación, conversacional — y produce un informe de salud estructurado. También soporta análisis agregado entre proyectos para identificar patrones sistémicos.

---

## Índice

1. [Visión general de la arquitectura](#visión-general-de-la-arquitectura)
2. [Conceptos clave](#conceptos-clave)
3. [Supervisión de un proyecto individual](#supervisión-de-un-proyecto-individual)
   - [Modelos de datos](#modelos-de-datos)
   - [Runner](#runner)
   - [Contrato del evaluador](#contrato-del-evaluador)
   - [Taxonomía de evaluadores](#taxonomía-de-evaluadores)
   - [Síntesis](#síntesis)
   - [Endpoint de la API](#endpoint-de-la-api)
4. [Supervisión agregada](#supervisión-agregada)
   - [Modelo de datos agregado](#modelo-de-datos-agregado)
   - [Lógica de filtros](#lógica-de-filtros)
   - [Builder (tabla de frecuencias + corpus)](#builder-tabla-de-frecuencias--corpus)
   - [Síntesis agregada](#síntesis-agregada)
   - [Endpoint de la API agregada](#endpoint-de-la-api-agregada)
5. [Infraestructura compartida](#infraestructura-compartida)
   - [Ficheros de traza](#ficheros-de-traza)
   - [Resolución de prompts históricos](#resolución-de-prompts-históricos)
   - [Módulos de ayuda](#módulos-de-ayuda)
6. [Cómo añadir un nuevo evaluador](#cómo-añadir-un-nuevo-evaluador)
7. [Decisiones de diseño y restricciones conocidas](#decisiones-de-diseño-y-restricciones-conocidas)

---

## Visión general de la arquitectura

```
POST /supervisor/{project_id}/analyze
        │
        └─► supervisor_runner.run_supervisor()
                │
                ├─► [22 evaluadores ejecutados secuencialmente]
                │       cada uno devuelve EvaluatorOutput
                │       se almacenan como filas AgentEvaluation
                │
                ├─► supervisor_synthesizer.synthesize()
                │       1 llamada LLM → narrativa Markdown
                │
                └─► SupervisorReport (completed)


POST /supervisor/aggregate
        │
        └─► aggregate_runner.run_aggregate()
                │
                ├─► aggregate_filter.select_reports()
                │       carga N≤20 SupervisorReports
                │       aplica filtros de versión/fecha/proyecto/dirty
                │
                ├─► aggregate_builder.build_frequency_table()
                │       % de veredictos por agente en N proyectos
                │
                ├─► aggregate_builder.build_text_corpus()
                │       evidencia textual de agentes no saludables
                │
                ├─► aggregate_synthesizer.synthesize()
                │       1 llamada LLM → narrativa entre proyectos
                │
                └─► AggregateReport (completed o failed)
```

---

## Conceptos clave

### Veredictos

Cada evaluación produce uno de tres veredictos (o `None`):

| Veredicto | Significado |
|---|---|
| `healthy` | El agente funciona según lo esperado |
| `needs_attention` | Preocupaciones recurrentes que no son fallos críticos |
| `degraded` | Fallos sistemáticos que impactan los resultados del proyecto |
| `None` (null en BD) | El agente no participó en este proyecto (`not_supervised`) |

El `overall_verdict` del proyecto en `SupervisorReport` se calcula en dos fases:

**Fase 1 — Comprobación de cobertura mínima:**
Si más del 30% de los agentes son `not_supervised`, la muestra es demasiado incompleta para producir un veredicto significativo. El resultado es `not_evaluated`.

**Fase 2 — Media de puntuaciones** (solo si la cobertura es suficiente):

| Veredicto | Puntuación |
|---|---|
| `healthy` | 2 |
| `needs_attention` | 1 |
| `degraded` | 0 |
| `not_supervised` | excluido |

Los umbrales de la media determinan el veredicto global:

| Media | Veredicto global |
|---|---|
| ≥ 1.7 | `healthy` |
| ≥ 1.3 | `needs_attention` |
| < 1.3 | `degraded` |

Con 22 agentes y el resto saludables, esto implica que se necesitan **≥4 agentes degradados** para bajar a `needs_attention` y **≥8** para llegar a `degraded`. Un único agente con mal funcionamiento no penaliza el veredicto global.

### Wrapper `EvaluatorOutput`

El método `evaluate()` de cada evaluador devuelve:

```python
class EvaluatorOutput(BaseModel):
    result: AgentEvaluationOutput | None   # None → not_supervised
    execution_run_ids_analyzed: list[int]  # qué runs se leyeron
    system_versions_seen: list[str]        # hashes git de esos runs
```

`result=None` significa que el agente no participó en el proyecto (no se encontraron datos). El runner mapea esto a `verdict=NULL` en la base de datos.

### Proyectos sucios (*dirty*)

Un proyecto es "dirty" si **ninguno** de los valores en `AgentEvaluation.system_versions_seen` contiene un hash git limpio (7–40 caracteres hexadecimales en minúscula, sin sufijo `-dirty` ni otros sufijos).

Los proyectos dirty no se pueden situar de forma fiable en un análisis basado en versiones, porque fueron generados desde un estado de código no comprometido en git.

---

## Supervisión de un proyecto individual

### Modelos de datos

**`SupervisorReport`** (`app/models/supervisor_report.py`)

Una fila por ejecución de supervisión por proyecto.

| Columna | Tipo | Notas |
|---|---|---|
| `report_id` | `int` PK | Auto-generado |
| `project_id` | `int` FK → `projects.id` | Indexado |
| `status` | `str` | `pending` / `running` / `completed` / `failed` |
| `overall_verdict` | `str \| None` | Peor caso entre todos los agentes |
| `synthesis` | `text \| None` | Narrativa Markdown generada por el LLM |
| `analyzed_run_ids` | `JSON list[int]` | Unión de todos los run IDs de todos los evaluadores |
| `error_message` | `text \| None` | Solo se establece si el runner lanzó una excepción inesperada |
| `created_at` | `datetime` | Valor por defecto del servidor: `now()` |
| `completed_at` | `datetime \| None` | Se establece cuando el runner termina |

**`AgentEvaluation`** (`app/models/agent_evaluation.py`)

Una fila por `(report_id, agent_name)`. Clave primaria compuesta — sin ID sustituto.

| Columna | Tipo | Notas |
|---|---|---|
| `report_id` | `int` PK/FK → `supervisor_reports.report_id` | Cascade delete |
| `agent_name` | `str` PK | Debe ser único dentro del informe |
| `project_id` | `int` FK → `projects.id` | Indexado (redundante pero útil para consultas) |
| `verdict` | `str \| None` | `healthy` / `needs_attention` / `degraded` / `NULL` |
| `findings` | `text \| None` | Narrativa en prosa |
| `issues` | `JSON list[str] \| None` | Problemas concretos detectados |
| `suggestions` | `JSON list[str] \| None` | Sugerencias de mejora accionables |
| `execution_run_ids_analyzed` | `JSON list[int] \| None` | Runs consultados |
| `system_versions_seen` | `JSON list[str] \| None` | Hashes git de esos runs |

### Runner

`supervisor_runner.run_supervisor(*, db, project_id)` en [`supervisor_runner.py`](supervisor_runner.py).

**Secuencia:**

1. Cargar `Project` y `Conversation` (para obtener `requirements_draft`).
2. Determinar `system_version` — el `ExecutionRun.system_version` más reciente no nulo para este proyecto (requiere join a través de `Task` porque `ExecutionRun` no tiene FK directa a `project_id`).
3. Crear `SupervisorReport(status="running")` y hacer flush (genera `report_id`).
4. Ejecutar los 22 evaluadores secuencialmente. Para cada uno:
   - Éxito → guardar fila `AgentEvaluation` con el veredicto real.
   - Excepción → guardar fila con `verdict=None` y `findings="Evaluator failed with error: ..."`. El error nunca se propaga; el siguiente evaluador continúa.
5. Calcular `overall_verdict`: si >30% de agentes son `not_supervised` → `not_evaluated`; si no, media de puntuaciones (healthy=2, needs_attention=1, degraded=0) con umbrales ≥1.7 → healthy, ≥1.3 → needs_attention, <1.3 → degraded.
6. Llamar a `synthesize()` para producir la narrativa. Si la síntesis falla, el texto de síntesis contiene el mensaje de error pero el informe se marca igualmente como `COMPLETED` (las evaluaciones individuales siguen siendo válidas).
7. Commit. Si el commit falla → rollback, marcar como `FAILED`.

**Los 22 evaluadores** (lista `_EVALUATORS` a nivel de módulo, evaluados en este orden):

```
planner, atomic_task_generator, execution_sequencer,
environment_planner, catalog_selector,
orchestrator, context_selection_agent, environment_manager_agent,
code_change_agent, code_change_agent_validator,
command_runner_agent, command_runner_agent_validator,
test_builder_agent, test_builder_agent_validator,
document_writer_agent, document_writer_agent_validator,
stage_evaluator, recovery_planner, recovery_assignment,
requirements_evaluator, review_episode, aria_orchestrator
```

### Contrato del evaluador

Todo evaluador es una clase con:

```python
class MiAgentEvaluator:
    AGENT_NAME: str  # debe ser único entre los 22 evaluadores

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        ...
```

- Si el agente no produjo datos para este proyecto → devolver `EvaluatorOutput(result=None)`.
- En caso contrario → llamar al LLM, validar la respuesta como `AgentEvaluationOutput`, y devolver `EvaluatorOutput(result=output)`.
- El evaluador también debe rellenar `execution_run_ids_analyzed` y `system_versions_seen` en el `EvaluatorOutput` devuelto.

**Patrón de reintento**: Todos los evaluadores incluyen un reintento LLM en caso de `ValidationError`. Si la respuesta de la primera llamada falla la validación Pydantic, se realiza una segunda llamada con el error de validación en el prompt. Tras dos fallos, la excepción se propaga (capturada por el runner).

### Taxonomía de evaluadores

Los evaluadores se organizan en cinco grupos según su fuente de datos:

| Grupo | Evaluadores | Fuente de datos |
|---|---|---|
| **Capa de planificación** | `planner`, `atomic_task_generator`, `execution_sequencer` | `planning_trace.jsonl` + estadísticas de Task/ExecutionRun en BD |
| **Capa de entorno** | `environment_planner`, `catalog_selector`, `environment_manager_agent` | `planning_trace.jsonl` o `execution_trace.jsonl` |
| **Capa de ejecución** | `orchestrator`, `context_selection_agent` | `execution_trace.jsonl` |
| **Pares ejecutor/validador** | `code_change_agent` + `code_change_agent_validator`, `command_runner_agent` + `command_runner_agent_validator`, `test_builder_agent` + `test_builder_agent_validator`, `document_writer_agent` + `document_writer_agent_validator` | `execution_trace.jsonl` + Artifacts de tipo `validation_result` |
| **Capa de evaluación/recuperación/conversación** | `stage_evaluator`, `recovery_planner`, `recovery_assignment`, `requirements_evaluator`, `review_episode`, `aria_orchestrator` | Artifacts en BD + modelo Conversation |

**Evaluadores de pares** comparten la selección de tareas mediante `build_pair_evaluation_context()` de [`_pair_evaluation_helpers.py`](_pair_evaluation_helpers.py). Esta función es determinista — tanto el evaluador ejecutor como el evaluador validador la llaman de forma independiente y reciben las mismas tareas. La selección prioriza tareas fallidas/parciales (Nivel 1), luego tareas con budget agotado (Nivel 2), luego tareas con más reintentos (Nivel 3), con un límite de 10 tareas por evaluación.

**Importante**: los evaluadores ejecutores y sus evaluadores validadores correspondientes deben tener `AGENT_NAME` **distintos** porque la PK compuesta en `agent_evaluations` es `(report_id, agent_name)`. La convención de nombres es:
- Ejecutor: `code_change_agent`
- Validador: `code_change_agent_validator`

### Síntesis

`supervisor_synthesizer.synthesize(...)` en [`supervisor_synthesizer.py`](supervisor_synthesizer.py).

Una llamada LLM usando el prompt `supervisor_synthesis` (`app/prompts/supervisor/supervisor_synthesis.yaml`). Recibe la lista completa de los 22 resultados de agentes (veredicto, findings, issues, suggestions) y produce una narrativa Markdown con secciones:

- `## Veredicto General`
- `## Problemas Críticos` (solo agentes degraded)
- `## Requiere Atención` (agentes needs_attention)
- `## Patrones Transversales` (temas que abarcan varios agentes)
- `## Mejoras Recomendadas`
- `## Agentes Saludables`

Mínimo 150 caracteres. Validado mediante Pydantic `_SynthesisOutput(synthesis: str = Field(min_length=150))`.

### Endpoint de la API

```
POST /supervisor/{project_id}/analyze
```

- Devuelve `SupervisorReportRead` (incluye la lista completa de `agent_evaluations`).
- `404` si el proyecto no existe.
- `500` si el runner lanza una excepción inesperada (errores de BD, etc.).
- Los fallos de síntesis **no** producen un 500 — el informe se devuelve con `status="completed"` y el error incrustado en el campo `synthesis`.

---

## Supervisión agregada

El supervisor agregado analiza **múltiples proyectos a la vez** para encontrar patrones sistémicos — fallos que se repiten entre proyectos, no solo dentro de uno.

### Modelo de datos agregado

**`AggregateReport`** (`app/models/aggregate_report.py`)

Sin FK a `SupervisorReport` — si se eliminan informes individuales, la instantánea agregada sigue siendo válida.

| Columna | Tipo | Notas |
|---|---|---|
| `aggregate_report_id` | `int` PK | Auto-generado |
| `created_at` | `datetime` | UTC, establecido en la creación |
| `filter_params` | `JSON dict` | Los parámetros de filtro utilizados en bruto |
| `supervisor_report_ids` | `JSON list[int]` | Qué filas `SupervisorReport` se incluyeron |
| `project_ids_analyzed` | `JSON list[int]` | IDs de proyectos analizados |
| `dirty_projects_excluded` | `int` | Proyectos dirty eliminados por el filtro de suciedad |
| `agent_frequency_table` | `JSON list[dict]` | Porcentajes de veredicto por agente (ver más abajo) |
| `synthesis` | `text \| None` | Narrativa entre proyectos generada por el LLM |
| `status` | `str` | `completed` / `failed` |
| `error_message` | `text \| None` | Se establece si la síntesis falla |

### Lógica de filtros

`aggregate_filter.select_reports(db, ...)` en [`aggregate_filter.py`](aggregate_filter.py).

**Cadena de filtros** (aplicados en este orden):

1. Cargar todas las filas `SupervisorReport` con `status="completed"`, ordenadas por `created_at DESC`.
2. Conservar solo el **informe más reciente por proyecto** (deduplicar).
3. Aplicar filtro `project_ids` (lista explícita).
4. Aplicar filtro de rango `date_from` / `date_to` (normalizado a UTC naive para compatibilidad con SQLite).
5. Aplicar filtro `version` — conservar solo informes donde algún `AgentEvaluation.system_versions_seen` contenga el hash git exacto solicitado.
6. Aplicar filtro dirty (ver más abajo).
7. Limitar a `min(limit, 20)`. Límite por defecto: 20 (la constante `_MAX_PROJECTS`).
8. Lanzar `TooFewProjectsError` si quedan menos de 5 proyectos (la constante `_MIN_PROJECTS`).

Devuelve `(informes_seleccionados, dirty_excluded_count)`.

**Reglas del filtro dirty:**

| Filtro usado | `include_dirty` por defecto |
|---|---|
| Filtro `version` | `False` (proyectos dirty excluidos) |
| Solo fecha / solo límite | `True` (proyectos dirty incluidos) |
| `include_dirty` explícito | Sobreescribe el valor por defecto |

Nota: al filtrar por `version`, los proyectos dirty (sin hash limpio) nunca pueden coincidir con el filtro de versión de todas formas (se requiere coincidencia exacta), por lo que `dirty_excluded` siempre será `0` en consultas basadas en versión. El filtro dirty solo tiene impacto significativo en consultas basadas en fecha o límite.

**`TooFewProjectsError`** se lanza cuando menos de 5 proyectos coinciden tras todos los filtros. El manejador de la API mapea esto a HTTP 422.

### Builder (tabla de frecuencias + corpus)

`aggregate_builder` en [`aggregate_builder.py`](aggregate_builder.py) produce dos entradas para la llamada al LLM:

**Tabla de frecuencias** (`build_frequency_table(reports)`):

Lista de diccionarios, uno por agente, ordenados por tasa de degradación descendente:

```python
{
    "agent_name": "orchestrator",
    "total_projects": 12,
    "degraded_pct": 83,
    "needs_attention_pct": 8,
    "healthy_pct": 0,
    "not_supervised_pct": 9,
    "degraded_count": 10,
    "needs_attention_count": 1,
}
```

Usa la lista canónica `_AGENT_NAMES` (22 entradas) para garantizar que todos los agentes aparezcan aunque ninguno de los informes seleccionados los haya evaluado (reciben `not_supervised_pct=100`).

**Corpus de texto** (`build_text_corpus(reports)`):

Evidencia textual concatenada de evaluaciones no saludables, ordenada por severidad:
1. Evidencia de agentes **degraded** primero (de proyectos con veredicto general `degraded`).
2. Evidencia de agentes **needs_attention** en segundo lugar.

Cada bloque incluye la vista previa de `synthesis` del informe (primeros 800 caracteres) más `findings` por agente (primeros 400 caracteres) y hasta 5 issues. Truncado a `_MAX_CORPUS_CHARS = 60.000` caracteres.

### Síntesis agregada

`aggregate_synthesizer.synthesize(...)` en [`aggregate_synthesizer.py`](aggregate_synthesizer.py).

Una llamada LLM usando el prompt `aggregate_synthesizer` (`app/prompts/supervisor/aggregate_synthesizer.yaml`). Recibe:
- `project_count` — cuántos proyectos se analizaron
- `filter_description` — descripción legible de los filtros activos
- `frequency_table` — la lista de diccionarios por agente
- `text_corpus` — el texto de evidencia

Produce una narrativa entre proyectos con secciones:
- `## Patrones Críticos`
- `## Problemas Recurrentes`
- `## Propuestas de Mejora Sistémica`
- `## Correlaciones Entre Agentes`
- `## Señales Saludables`

Mínimo 200 caracteres.

Si la síntesis lanza una excepción, el `AggregateReport` se persiste con `status="failed"` y el error en `error_message`. A diferencia del runner individual, el runner agregado establece el estado como `failed` ante un error de síntesis (la tabla de frecuencias se almacena igualmente en `agent_frequency_table`).

### Endpoint de la API agregada

```
POST /supervisor/aggregate
Content-Type: application/json

{
    "version": "abc1234",                  // opcional: hash git exacto
    "date_from": "2026-01-01T00:00:00Z",  // opcional
    "date_to": "2026-06-01T00:00:00Z",    // opcional
    "project_ids": [1, 2, 3],             // opcional: lista explícita de IDs
    "limit": 15,                           // opcional: 5–20, por defecto 20
    "include_dirty": false                 // opcional: sobreescribe el valor por defecto de dirty
}
```

**Al menos un filtro debe estar presente** (incluido solo `limit`). Un cuerpo `{}` vacío devuelve 422.

- `200` con `AggregateReportRead` en caso de éxito.
- `422` con detalle del error si menos de 5 proyectos coincidieron (`TooFewProjectsError`).
- `422` con error de validación Pydantic si el cuerpo de la petición es inválido.
- `500` para errores inesperados.

**Semántica de `version`**: un único hash git exacto, no un rango. El filtro es una coincidencia exacta contra los valores de `system_versions_seen`. Para comparar comportamiento entre versiones, lanzar dos peticiones agregadas separadas con distintos hashes y comparar los resultados manualmente.

---

## Infraestructura compartida

### Ficheros de traza

El supervisor lee dos ficheros JSONL de solo-adición por proyecto:

- `{project_meta_dir}/planning_trace.jsonl` — escrito por los clientes de la cadena de planificación (planner, atomic_task_generator, execution_sequencer). Cada línea es un objeto JSON con al menos las claves `"agent"` y `"call_type"`.
- `{project_meta_dir}/execution_trace.jsonl` — escrito por los clientes de la capa de ejecución (orchestrator y subagentes). Cada línea tiene al menos las claves `"agent"` y `"task_id"`.

Los ficheros son escritos por `trace_writer.py` (`append_planning_trace`, `append_execution_trace`). Las escrituras nunca lanzan excepciones — los fallos se registran en WARNING y se ignoran silenciosamente.

Los evaluadores filtran por `entry.get("agent") == agent_name` para extraer las entradas relevantes.

### Resolución de prompts históricos

`prompt_resolver.resolve_system_prompt(agent_name, prompt_key, *, system_version)` en [`prompt_resolver.py`](prompt_resolver.py).

Cuando `system_version` es un hash de commit git resoluble (7+ caracteres hex, opcionalmente con sufijo `-dirty`), intenta ejecutar `git show <commit>:app/prompts/<ruta>.yaml` para recuperar el YAML del prompt **tal como existía en ese commit exacto**. Esto permite que los evaluadores valoren el comportamiento del agente frente a las instrucciones exactas que estaban activas durante la ejecución. Si git no está disponible, el commit no se encuentra o hay un error de parseo, se usa el prompt_loader actual como fallback.

`_AGENT_YAML_PATHS` mapea cada nombre de agente a la ruta relativa de su fichero de prompt. Si el nombre de un agente no está en este mapa, la resolución histórica se omite.

### Módulos de ayuda

| Fichero | Propósito |
|---|---|
| [`_execution_helpers.py`](_execution_helpers.py) | Carga entradas de traza de ejecución, construye índice de ficheros, serializa runs para los prompts de evaluadores. `_MAX_RUNS_PER_EVALUATION = 15` |
| [`_pair_evaluation_helpers.py`](_pair_evaluation_helpers.py) | Selección compartida de tareas para pares ejecutor/validador. `_MAX_TASKS_PER_EVALUATION = 10`. Niveles de prioridad: failed/partial → budget agotado → muchos reintentos |
| [`_recovery_evaluation_helpers.py`](_recovery_evaluation_helpers.py) | Carga artifacts de tipo `evaluation_decision`, `recovery_decision` y tríos de `recovery_assignment` desde la BD |
| [`_conversation_evaluation_helpers.py`](_conversation_evaluation_helpers.py) | Carga artifacts de `requirements_evaluation`, `review_episode`, `project_query` y el estado del modelo Conversation |

---

## Cómo añadir un nuevo evaluador

Seguir estos pasos en orden. Saltarse cualquiera rompe el runner o el análisis agregado.

### Paso 1 — Elegir un `AGENT_NAME` único

El nombre no debe colisionar con ninguna entrada existente en la lista `_EVALUATORS` de `supervisor_runner.py`. Consultar los 22 nombres actuales en [`aggregate_builder.py`](aggregate_builder.py) bajo `_AGENT_NAMES`.

Para pares ejecutor/validador, usar la convención:
- Ejecutor: `mi_agente`
- Validador: `mi_agente_validator`

### Paso 2 — Crear el fichero del evaluador

Crear `app/services/supervisor/evaluators/mi_agente_evaluator.py`:

```python
from sqlalchemy.orm import Session
from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput

MI_AGENTE_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get("mi_agente_evaluator")


class MiAgenteEvaluator:
    AGENT_NAME = "mi_agente"  # nombre único

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        # 1. Cargar datos del proyecto
        datos = _cargar_datos_mi_agente(project_id)

        # 2. Sin datos → not supervised
        if not datos:
            return EvaluatorOutput(result=None)

        # 3. Determinar qué run IDs y versiones se analizaron
        run_ids = [...]
        versiones = get_system_versions_for_runs(db, run_ids)

        # 4. Construir prompt y llamar al LLM
        provider = get_llm_provider()
        raw = provider.generate_structured(
            system_prompt=MI_AGENTE_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=_construir_user_prompt(...),
            schema_name="mi_agente_evaluator_output",
            json_schema=AgentEvaluationOutput.model_json_schema(),
        )

        # 5. Validar con un reintento en ValidationError
        try:
            result = AgentEvaluationOutput.model_validate(raw)
        except ValidationError as exc:
            raw_retry = provider.generate_structured(
                system_prompt=MI_AGENTE_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=_construir_retry_prompt(project_id=project_id, validation_error=str(exc)),
                schema_name="mi_agente_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        return EvaluatorOutput(
            result=result,
            execution_run_ids_analyzed=run_ids,
            system_versions_seen=versiones,
        )
```

### Paso 3 — Crear el YAML del prompt

Crear `app/prompts/supervisor/mi_agente_evaluator.yaml`:

```yaml
agent_name: mi_agente_evaluator
version: "1.0.0"
changelog:
  - version: "1.0.0"
    date: "AAAA-MM-DD"
    changes: "Prompt inicial del evaluador"
prompts:
  main:
    description: "Evalúa el rendimiento de mi_agente en un proyecto"
    user_prompt_inputs:
      - name: project_id
        required: true
      # ... otras entradas
    content: |-
      Eres el Supervisor — un meta-evaluador ...
      # Instrucciones de evaluación aquí
  retry:
    description: "Prompt de reintento ante error de validación"
    user_prompt_inputs:
      - name: project_id
        required: true
      - name: validation_error
        required: true
```

Si se usa `prompt_loader.validate_builder_inputs(...)`, todas las claves declaradas en `user_prompt_inputs` deben estar presentes en el diccionario que se pasa — incluso las entradas con `required: false`. Pasar `None` para los campos opcionales sin valor.

### Paso 4 — Registrar en el runner

En `supervisor_runner.py`:

```python
from app.services.supervisor.evaluators.mi_agente_evaluator import MiAgenteEvaluator

_EVALUATORS = [
    ...
    MiAgenteEvaluator(),  # añadir en el orden lógico
]
```

### Paso 5 — Registrar en el builder agregado

En `aggregate_builder.py`, añadir el nombre del agente a `_AGENT_NAMES` en la misma posición lógica:

```python
_AGENT_NAMES = [
    ...
    "mi_agente",  # nueva entrada
]
```

Esto garantiza que el agente aparezca en la tabla de frecuencias de los informes agregados, incluso si no tiene datos (`not_supervised_pct=100`).

### Paso 6 — Registrar en el resolución de prompts (opcional pero recomendado)

En `prompt_resolver.py`, añadir a `_AGENT_YAML_PATHS`:

```python
_AGENT_YAML_PATHS = {
    ...
    "mi_agente": "supervisor/mi_agente_evaluator.yaml",
}
```

Esto permite la resolución histórica de prompts cuando un proyecto tiene `system_version`.

### Paso 7 — Escribir tests

Crear `tests/services/supervisor/test_mi_agente_evaluator.py`. Usar los ficheros de test existentes como referencia. Patrones clave:
- Usar los fixtures `db_session` y `make_project` de `conftest.py`.
- Testear que el camino sin datos devuelve `EvaluatorOutput(result=None)`.
- Testear el camino feliz con LLM mockeado (`patch("app.services.supervisor.evaluators.mi_agente_evaluator.get_llm_provider")`).
- Acceder al veredicto con `resultado.result.verdict` (no `resultado.verdict` — el wrapper `EvaluatorOutput` está un nivel por encima).

---

## Decisiones de diseño y restricciones conocidas

### Evaluación secuencial — sin paralelismo

Los 22 evaluadores se ejecutan secuencialmente en el runner. Esto es intencional: manejo de errores más simple, estado de BD predecible, depuración más fácil. Esperar 23+ llamadas LLM por ejecución de supervisión de proyecto (22 evaluadores + 1 síntesis). Paralelizar es posible pero no está implementado.

### El fallo de síntesis no falla el informe (proyecto individual)

Si `synthesize()` lanza en `supervisor_runner`, el `SupervisorReport` se marca igualmente como `COMPLETED`. El campo `synthesis` contiene el mensaje de error como texto. Las filas `AgentEvaluation` individuales no se ven afectadas y siguen siendo válidas. Esto difiere del runner agregado, que establece `status="failed"` ante un error de síntesis.

### Sin endpoints GET para informes históricos

Actualmente no hay endpoints `GET /supervisor/{project_id}/reports` ni `GET /supervisor/aggregate` para listar o recuperar informes pasados. Solo `POST` crea nuevos.

### `AggregateRequest` requiere al menos un filtro

Un cuerpo `{}` vacío en `POST /supervisor/aggregate` devuelve 422. `limit` cuenta como un filtro válido (restringe a los N informes completados más recientes por proyecto).

### `version` es un hash único, no un rango

El endpoint de agregación acepta un único hash git exacto. El filtro es una coincidencia exacta contra `AgentEvaluation.system_versions_seen`. Para comparar comportamiento entre dos versiones del código, llamar al endpoint dos veces con hashes diferentes y comparar los resultados manualmente.

### Compatibilidad de datetime con SQLite

`SupervisorReport.created_at` se almacena como UTC naive en SQLite (el dialecto SQLite de SQLAlchemy elimina la información de zona horaria). El filtro agregado normaliza tanto el datetime del filtro como el almacenado a UTC naive antes de la comparación usando `_as_naive_utc()`.

### La detección de dirty usa solo `system_versions_seen` de `AgentEvaluation`

Un proyecto es dirty si **todas** sus evaluaciones no tienen ningún hash git limpio en `system_versions_seen`. Un hash limpio coincide con `^[0-9a-f]{7,40}$` (hex en minúscula, 7–40 caracteres, sin sufijo). Los proyectos mixtos (algunas evaluaciones con hashes limpios, otras sin ellos) se tratan como **limpios**.

### `AGENT_NAME` en los evaluadores validadores debe diferir del ejecutor

La PK compuesta en `agent_evaluations` es `(report_id, agent_name)`. Si ejecutor y validador comparten el mismo nombre, el segundo `db.add()` en el runner causará un `IntegrityError`. La convención de nombres hace cumplir nombres distintos:

```
code_change_agent           ← ejecutor
code_change_agent_validator ← validador (AGENT_NAME debe ser este)
```

La constante `_VALIDATOR_NAME` en cada fichero de evaluador validador ya tiene el nombre correcto. `AGENT_NAME` debe apuntar a `_VALIDATOR_NAME`, no al nombre del ejecutor.
