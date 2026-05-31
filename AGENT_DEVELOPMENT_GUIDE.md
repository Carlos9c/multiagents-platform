# Guía de Desarrollo de Agentes

Referencia para añadir nuevos agentes al sistema Agente Desarrollador.  
Todo paso marcado como **[OBLIGATORIO]** debe completarse para cada nuevo agente, sin excepciones.

---

## Índice

1. [Categorías de agentes](#1-categorías-de-agentes)
2. [Paso 1 — Crear el fichero YAML de prompts (OBLIGATORIO)](#2-paso-1--crear-el-fichero-yaml-de-prompts-obligatorio)
3. [Paso 2 — Escribir el módulo Python (OBLIGATORIO)](#3-paso-2--escribir-el-módulo-python-obligatorio)
4. [Paso 3 — Integración como subagente de ejecución (si aplica)](#4-paso-3--integración-como-subagente-de-ejecución-si-aplica)
5. [Paso 4 — Validador (si aplica)](#5-paso-4--validador-si-aplica)
6. [Paso 5 — Tests (OBLIGATORIO)](#6-paso-5--tests-obligatorio)
7. [Paso 6 — Supervisor (OBLIGATORIO)](#7-paso-6--supervisor-obligatorio)
8. [Convención de versiones (OBLIGATORIO)](#8-convención-de-versiones-obligatorio)
9. [Lista de verificación rápida](#9-lista-de-verificación-rápida)

---

## 1. Categorías de agentes

Antes de empezar, decide a qué categoría pertenece el agente:

| Categoría | Directorio | Ejemplos |
|---|---|---|
| Subagente de ejecución | `app/execution_engine/subagents/` | `code_change_agent`, `test_builder_agent`, `command_runner_agent` |
| Herramienta de ejecución | `app/execution_engine/tools/` | `error_diagnostic_tool` |
| Servicio de planificación | `app/services/` | `planner_client`, `atomic_task_generator_client` |
| Servicio de recuperación | `app/services/` | `recovery_client`, `evaluation_client` |
| Evaluador conversacional | `app/services/conversation/` | `requirements_evaluator`, `review_evaluator` |
| Servicio de entorno | `app/services/environment/` | `planner_client`, `catalog/selector_client` |
| Servicio de análisis | `app/services/analysis/` | `service.py`, `analyzers/text_analyzer.py` |
| Validador | `app/services/validation/validators/` | `code_change_agent_validator` |
| **QA agent** | `app/services/qa/qa_agents/` | `smoke_qa_agent`, `functional_qa_agent` |
| **QA bootstrapper** | `app/services/qa/strategies/` | `MobileAndroidBootstrapStrategy` |

La categoría determina el subdirectorio YAML a usar y qué pasos de integración adicionales aplican.

---

## 2. Paso 1 — Crear el fichero YAML de prompts [OBLIGATORIO]

### 2.1 Elegir el directorio correcto

Los ficheros YAML se alojan bajo `app/prompts/` en el subdirectorio que corresponde a la capa del agente:

```
app/prompts/
  execution/        ← subagentes y herramientas del motor de ejecución
  planning/         ← planner, refiner de tareas, sequencer, generador de tareas atómicas
  recovery/         ← recovery_planner, stage_evaluator, recovery_assignment
  conversation/     ← aria_orchestrator, requirements_evaluator, review_evaluator, …
  validation/       ← agentes *_validator
  environment/      ← environment_planner, catalog_selector
  analysis/         ← codebase_analyzer, file_analyzer
  qa/               ← qa_context_agent, smoke_qa_agent, functional_qa_agent, …
```

### 2.2 Formato del fichero YAML

```yaml
agent_name: mi_nuevo_agente     # debe coincidir exactamente con lo que se pasa a prompt_loader.get()
version: "1.0.0"                # empezar en 1.0.0 para agentes nuevos
changelog:
  - version: "1.0.0"
    date: "AAAA-MM-DD"
    changes: "Versión inicial"
prompts:
  main:                         # clave por defecto; usar nombre descriptivo en agentes multi-prompt
    description: "Una frase que explica qué hace este agente"
    user_prompt_inputs:
      - name: task
        description: "Definición completa de la tarea enviada al user prompt del LLM"
        required: true
      - name: contexto_opcional
        description: "Contexto extra inyectado solo cuando está disponible"
        required: false
    content: |-
      Eres un ...
      (texto completo y verbatim del system prompt — sin placeholders aquí)
```

**Reglas:**
- `agent_name` debe coincidir exactamente con el nombre del fichero sin `.yaml` (ej. `mi_nuevo_agente.yaml` → `agent_name: mi_nuevo_agente`).
- Usar el bloque escalar `|-` para `content` — elimina el salto de línea final y preserva todos los internos.
- Empezar siempre en `version: "1.0.0"` para ficheros nuevos.
- `user_prompt_inputs` debe listar **cada** clave que el builder Python del user prompt inyecte. Ver §7 para cuándo incrementar la versión.

### 2.3 Agentes multi-prompt

Algunos agentes exponen múltiples system prompts (ej. `test_builder_agent` tiene `main` y `coverage_assessment`; `command_runner_agent` tiene `file_selection` y `main`). Añadir una entrada por clave bajo `prompts:`, cada una con su propio `description`, `user_prompt_inputs` y `content: |-`.

**Casos especiales en la base de código existente:**
- `orchestrator.yaml` → la clave es `"decision"` (no `"main"`)
- `environment_manager_agent.yaml` → la clave es `"package_extraction"` (no `"main"`)

### 2.4 Builders de retry (sin system prompt propio)

Todo builder que construya un prompt de reintento (cuando la primera llamada LLM falla validación de schema) también debe estar declarado en el YAML y validado. Los retries reutilizan el mismo system prompt que la llamada principal — por tanto **no tienen `content:`** en el YAML; solo `description:` y `user_prompt_inputs:`.

```yaml
prompts:
  main:
    description: "Prompt principal"
    user_prompt_inputs:
      - name: project_name
        required: true
      - name: project_description
        required: true
    content: |-
      ...
  retry:
    description: "Retry cuando el output principal falló validación de schema"
    user_prompt_inputs:
      - name: project_name
        description: "Nombre del proyecto"
        required: true
      - name: project_description
        description: "Descripción del proyecto"
        required: true
      - name: validation_error
        description: "Mensaje de error de validación Pydantic del intento fallido"
        required: true
    # sin content: — el system prompt es el mismo que en main
```

El campo `content:` es opcional en `PromptEntry`. `prompt_loader.get()` lanza `KeyError` si se intenta cargar el system prompt de una clave sin `content:`, lo cual es correcto: los retries cargan el system prompt desde la clave principal y declaran sus inputs en su propia clave.

Convención de nombres de claves de retry:
- `retry` — retry genérico por error de validación
- `file_selection_retry` / `main_retry` / `main_constraint_retry` — cuando el agente tiene múltiples prompts principales
- `evolutionary` — variante con contexto de codebase existente (ej. planner evolutivo)

---

## 3. Paso 2 — Escribir el módulo Python [OBLIGATORIO]

### 3.1 Constante del system prompt

A nivel de módulo, definir el system prompt como alias de constante a través de `prompt_loader`:

```python
from app.services.prompt_loader import prompt_loader

MI_NUEVO_AGENTE_SYSTEM_PROMPT = prompt_loader.get("mi_nuevo_agente")
# Para una clave no predeterminada:
MI_PROMPT_SECUNDARIO = prompt_loader.get("mi_nuevo_agente", "segunda_pasada")
```

Esta constante se carga una vez en el momento de la importación (lazy en el primer uso). Preserva nombres compatibles hacia atrás para cualquier test que importe la constante directamente.

### 3.2 Builder del user prompt con `validate_builder_inputs`

Toda función que construya un user prompt — incluyendo **builders de retry** — **debe** llamar a `validate_builder_inputs` antes de componer el string. La validación es de **coincidencia exacta**: el dict `inputs` debe tener exactamente las mismas claves que el YAML, ni una más ni una menos.

```python
def _build_user_prompt(task: Task, context: ProjectContext) -> str:
    prompt_loader.validate_builder_inputs(
        "mi_nuevo_agente",
        "main",              # debe coincidir con la clave en el YAML
        {
            "task_id": task.id,             # required — debe estar presente
            "task_title": task.title,       # required
            "optional_context": context,    # required=false — incluir aunque sea None
        },
    )
    return f"""
Tarea: {task.title}
Contexto: {context or 'N/A'}
""".strip()
```

`validate_builder_inputs` lanza `ValueError` si:
1. El diccionario `inputs` contiene una clave no declarada en el YAML.
2. Cualquier clave declarada en el YAML está ausente del diccionario `inputs` (incluso las `required: false` — el valor puede ser `None`, pero la clave debe estar).

**Regla para builders de retry:** el dict debe incluir todos los inputs de la clave de retry declarados en el YAML (unión de los inputs del prompt base + el campo de error). Para wrappers que llaman al builder base internamente, computar los valores intermedios antes de llamar al base:

```python
def _build_retry_prompt(*, request, step, state, validation_error):
    # 1. computar valores intermedios
    inventory_text = ...
    changed_files = ...
    # 2. validar con la clave retry
    prompt_loader.validate_builder_inputs("mi_agente", "retry", {
        "task_id": request.task_id,
        ...,
        "validation_error": validation_error,
    })
    # 3. llamar al builder base (que valida con su propia clave)
    base = _build_main_prompt(request=request, ...)
    return f"Error anterior:\n{validation_error}\n\n{base}".strip()
```

**Regla de importación circular:** los módulos bajo `app/services/conversation/`, `app/services/analysis/` y `app/services/environment/` deben usar una **importación local** dentro de la función builder para evitar dependencias circulares:

```python
def _build_user_prompt(inp: MiInput) -> str:
    from app.services.prompt_loader import prompt_loader   # ← importación local únicamente
    prompt_loader.validate_builder_inputs("mi_nuevo_agente", "main", {
        "campo_a": inp.campo_a,
    })
    ...
```

El resto de módulos (`app/execution_engine/`, `app/services/planner_client.py`, validadores, etc.) usan importación a nivel de módulo.

---

## 4. Paso 3 — Integración como subagente de ejecución [si aplica]

Aplicar esta sección únicamente si el nuevo agente es un **subagente de ejecución** (se ejecuta dentro del bucle del orquestador, modifica el estado del workspace, o produce evidencia de ejecución).

### 4.1 Implementar `BaseSubagent`

```python
# app/execution_engine/subagents/mi_nuevo_agente.py
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError

class MiNuevoAgente(BaseSubagent):
    name = "mi_nuevo_agente"     # debe coincidir con el agent_name del YAML

    def __init__(self, runtime) -> None:
        self._runtime = runtime

    def execute_step(
        self,
        *,
        db,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
    ) -> ResolutionState:
        # ... construir user prompt, llamar al LLM, parsear respuesta, actualizar state ...
        return state
```

El atributo de clase `name` es la clave que usa el orquestador para el despacho y debe coincidir exactamente con lo registrado en todos los demás lugares.

### 4.2 Registrar en `SubagentRegistry` — `orchestrated_engine.py`

Abrir `app/execution_engine/engines/orchestrated_engine.py` y:

1. Importar la nueva clase del agente.
2. Instanciarla con el `StructuredLLMRuntime` apropiado.
3. Añadirla a la lista `SubagentRegistry(subagents=[...])`.

```python
# app/execution_engine/engines/orchestrated_engine.py

from app.execution_engine.subagents.mi_nuevo_agente import MiNuevoAgente

# Dentro de OrchestratedExecutionEngine.execute():
mi_agente_runtime = StructuredLLMRuntime(
    model=settings.mi_nuevo_agente_model,        # añadir a config.py + .env
    provider=settings.mi_nuevo_agente_provider,
)

registry = SubagentRegistry(
    subagents=[
        ...
        MiNuevoAgente(runtime=mi_agente_runtime),   # ← añadir aquí
    ]
)
```

Si el agente comparte runtime con uno existente (ej. usa el runtime del orquestador como `environment_manager_agent`), reutilizar la instancia `StructuredLLMRuntime` existente.

### 4.3 Declarar capacidades — `capabilities.py`

Abrir `app/execution_engine/capabilities.py` y añadir una entrada `SubagentCapability` a la lista `subagents` dentro de `get_execution_engine_capabilities()`. El orquestador la lee en tiempo de ejecución para describir los subagentes disponibles:

```python
SubagentCapability(
    name="mi_nuevo_agente",
    role=(
        "Una frase clara describiendo qué hace este agente y cuándo es apropiado usarlo."
    ),
    uses_tools=[
        "list_workspace_files",
        "write_text_file",
        # ...
    ],
    strengths=[
        "Puede hacer X.",
        "Gestiona los casos límite Y.",
    ],
    limits=[
        "No hace Z.",
        "No debe llamarse cuando ...",
    ],
    usage_guidance=[
        "Usar cuando la tarea requiera ...",
        "No usar cuando ...",
    ],
),
```

El orquestador inyecta el bloque completo de capacidades en el system prompt del paso de decisión. Entradas bien redactadas en `role`, `strengths`, `limits` y `usage_guidance` mejoran directamente la calidad de las decisiones del orquestador.

### 4.4 Añadir al enrutamiento de fases del orquestador

Abrir `app/execution_engine/orchestrator.py` y actualizar `_allowed_subagents_for_phase()`:

```python
def _allowed_subagents_for_phase(phase: str) -> list[str]:
    if phase == "discovery":
        return ["context_selection_agent"]   # mantener salvo que sea realmente un agente de discovery

    if phase == "execution":
        return [
            "context_selection_agent",
            "code_change_agent",
            "command_runner_agent",
            "document_writer_agent",
            "test_builder_agent",
            "environment_manager_agent",
            "mi_nuevo_agente",               # ← añadir aquí
        ]
    return []
```

Actualizar también `_last_attempted_subagent_name()` para que el orquestador pueda rastrear llamadas consecutivas:

```python
def _last_attempted_subagent_name(runtime_state: ExecutionState) -> str | None:
    for agent_name in reversed(runtime_state.visited_agents):
        if agent_name in {
            "context_selection_agent",
            "code_change_agent",
            "command_runner_agent",
            "document_writer_agent",
            "test_builder_agent",
            "environment_manager_agent",
            "mi_nuevo_agente",               # ← añadir aquí
        }:
            return agent_name
    return None
```

### 4.5 Decidir sobre la omisión de validación (`IGNORED_VALIDATION_PRODUCERS`)

Si el nuevo agente es exclusivamente de infraestructura (ej. repara el entorno o enriquece el contexto pero no produce ficheros de repositorio entregables), añadirlo a `IGNORED_VALIDATION_PRODUCERS` en `app/services/validation/selection.py`:

```python
IGNORED_VALIDATION_PRODUCERS = {
    "context_selection_agent",
    "execution_orchestrator",
    "environment_manager_agent",
    "mi_nuevo_agente",    # ← si no produce evidencia entregable
}
```

Si el agente produce ficheros de repositorio o evidencia de tarea primaria, **no** añadirlo aquí — escribir un validador en su lugar (ver §5).

---

## 5. Paso 4 — Validador [si aplica]

Escribir un validador si el nuevo agente produce evidencia entregable primaria (ficheros modificados, comandos, artefactos) que un LLM deba evaluar en cuanto a calidad y completitud.

### 5.1 Implementar `BaseTaskValidator`

```python
# app/services/validation/validators/mi_nuevo_agente_validator.py
from app.services.validation.base import BaseTaskValidator
from app.services.validation.contracts import TaskValidationInput, ValidationResult
from app.services.prompt_loader import prompt_loader

VALIDATOR_KEY = "mi_nuevo_agente_validator"
PRODUCER_KEY = "mi_nuevo_agente"   # debe coincidir con BaseSubagent.name

MI_NUEVO_AGENTE_VALIDATOR_SYSTEM_PROMPT = prompt_loader.get("mi_nuevo_agente_validator")


class MiNuevoAgenteValidator(BaseTaskValidator):
    validator_key = VALIDATOR_KEY
    producer_key = PRODUCER_KEY

    def validate(self, validation_input: TaskValidationInput) -> ValidationResult:
        # llamar al LLM, parsear respuesta, devolver ValidationResult
        ...
```

### 5.2 Registrar en `ValidationRegistry`

Abrir `app/services/validation/registry.py`:

```python
from app.services.validation.validators.mi_nuevo_agente_validator import MiNuevoAgenteValidator

class ValidationRegistry:
    def __init__(self, validators: list[BaseTaskValidator] | None = None) -> None:
        resolved_validators = validators or [
            CodeChangeAgentValidator(),
            CommandRunnerAgentValidator(),
            DocumentWriterAgentValidator(),
            TestBuilderAgentValidator(),
            MiNuevoAgenteValidator(),      # ← añadir aquí
        ]
```

### 5.3 Crear el fichero YAML del validador

Añadir `app/prompts/validation/mi_nuevo_agente_validator.yaml` siguiendo el formato estándar (§2.2). El prompt del validador recibe `TaskValidationInput` y devuelve una decisión estructurada.

---

## 6. Paso 5 — Tests [OBLIGATORIO]

Todo nuevo agente debe tener tests. El requisito mínimo depende de la categoría:

### Agentes de servicio (planificación / recuperación / conversación / entorno / análisis)

```
tests/services/test_mi_nuevo_agente.py       # o tests/services/conversation/, etc.
```

Cubrir:
- La función builder devuelve la estructura de string esperada.
- La llamada a `validate_builder_inputs` rechaza claves no declaradas (opcional pero recomendado).
- Las llamadas al LLM están mockeadas — nunca hacer llamadas reales a la API en tests unitarios.

### Subagentes de ejecución

```
tests/execution_engine/subagents/test_mi_nuevo_agente.py
```

Cubrir:
- El camino feliz de `execute_step()` actualiza `ResolutionState` correctamente.
- `SubagentRejectedStepError` se lanza ante fallos irrecuperables.
- La llamada al LLM está mockeada.

### Validadores

```
tests/services/validation/test_mi_nuevo_agente_validator.py
```

Cubrir:
- `validate()` mapea `ValidationResult.status` correctamente para las decisiones del LLM `completed`, `partial`, `failed`, `manual_review`.
- `ValidationResult` contiene los campos esperados.
- La llamada al LLM está mockeada.

### Ejecutar la suite

```bash
poetry run pytest -q
```

Todos los tests (1060+) deben seguir pasando. Los tests de integración (Docker) están excluidos por defecto; ejecutar con `-m integration` cuando sea necesario.

---

## 7. Paso 6 — Supervisor [OBLIGATORIO]

**Todo agente nuevo debe tener un evaluador en el Supervisor.** El Supervisor es la capa de meta-evaluación que analiza retrospectivamente la calidad de cada agente después de que el proyecto ha terminado de ejecutarse. Sin un evaluador, el agente queda fuera de la supervisión del sistema y sus fallos silenciosos nunca se detectan.

### 7.1 Contrato del evaluador — `EvaluatorOutput`

Cada evaluador devuelve un `EvaluatorOutput` (definido en `app/services/supervisor/contracts.py`):

```python
@dataclass
class EvaluatorOutput:
    result: AgentEvaluationOutput | None       # None = not_supervised
    execution_run_ids_analyzed: list[int] = field(default_factory=list)
    system_versions_seen: list[str] = field(default_factory=list)
```

- `result=None` — el agente no fue invocado en este proyecto; el Supervisor lo excluye del cálculo de veredicto.
- `result=AgentEvaluationOutput(...)` — el evaluador produjo una evaluación con `verdict`, `findings`, `issues` y `suggestions`.

### 7.2 Patrón de retorno anticipado (not_supervised)

**El guard de retorno anticipado es el primer bloque del método `evaluate()` y es obligatorio.** Si el agente no produjo ningún output rastreable en el proyecto (ni entradas en trace files ni execution runs), devuelve `EvaluatorOutput(result=None)` sin llamar al LLM:

```python
class MiNuevoAgenteEvaluator:
    AGENT_NAME = "mi_nuevo_agente"   # ← debe ser único en todo el sistema

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:

        # 1. Cargar datos del agente para este proyecto
        runs = _load_runs_for_agent(db, project_id, agent_name=self.AGENT_NAME)

        # 2. Guard: si no hay datos, el agente no fue llamado → not_supervised
        if not runs:
            return EvaluatorOutput(result=None)

        # 3. Construir user prompt y llamar al LLM
        ...
```

La fuente de datos varía según el tipo de evaluador:
- **Planificadores / secuenciadores** — `planning_trace.jsonl` (filtrado por `agent` field).
- **Subagentes de ejecución y validadores** — `ExecutionRun` rows en BD, filtrados por `agent_name` o similares; helpers en `_execution_helpers.py` y `_pair_evaluation_helpers.py`.
- **Evaluadores conversacionales** — episodios y mensajes en BD; helpers en `_conversation_evaluation_helpers.py`.
- **Recovery** — `RecoveryDecision` artifacts; helpers en `_recovery_evaluation_helpers.py`.

### 7.3 Convención de nombres — `AGENT_NAME`

**`AGENT_NAME` debe ser único en todo el sistema.** La tabla `agent_evaluations` tiene clave primaria compuesta `(report_id, agent_name)`. Un nombre duplicado provoca `IntegrityError` en producción.

Convención de nombres:
- Ejecutor: `"mi_nuevo_agente"` — coincide con `BaseSubagent.name`.
- Validador del ejecutor: `"mi_nuevo_agente_validator"` — **no** `"mi_nuevo_agente"`.

Si el nuevo agente tiene un validador asociado (§5), el evaluador del validador **debe** tener un `AGENT_NAME` distinto al del evaluador del ejecutor.

### 7.4 Pasos para crear el evaluador

**Paso 1 — Crear el fichero del evaluador**

```
app/services/supervisor/evaluators/mi_nuevo_agente_evaluator.py
```

Estructura mínima:

```python
from __future__ import annotations

import logging
from sqlalchemy.orm import Session

from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput
from app.services.supervisor.prompt_resolver import resolve_system_prompt
from app.services.llm.factory import get_llm_provider

logger = logging.getLogger(__name__)
_AGENT_NAME = "mi_nuevo_agente"


class MiNuevoAgenteEvaluator:
    AGENT_NAME = _AGENT_NAME

    def evaluate(self, *, db: Session, project_id: int, project_name: str = "",
                 project_description: str = "", system_version: str | None = None) -> EvaluatorOutput:
        # Cargar datos
        data = _load_data(db, project_id)
        if not data:
            return EvaluatorOutput(result=None)

        # Llamar al LLM
        system_prompt = resolve_system_prompt(_AGENT_NAME, system_version)
        user_prompt = _build_user_prompt(project_id, project_name, project_description, data)
        provider = get_llm_provider()
        raw = provider.generate_structured(system_prompt, user_prompt, schema=_OUTPUT_SCHEMA)

        result = _parse_output(raw)
        return EvaluatorOutput(result=result, execution_run_ids_analyzed=[...])
```

**Paso 2 — Crear el fichero YAML del evaluador**

```
app/prompts/supervisor/mi_nuevo_agente_evaluator.yaml
```

El prompt del evaluador recibe el contexto completo del agente y produce un `AgentEvaluationOutput`:
- `verdict`: `"healthy"` | `"needs_attention"` | `"degraded"`
- `findings`: resumen narrativo de los hallazgos
- `issues`: lista de problemas concretos detectados
- `suggestions`: lista de mejoras sugeridas

**Paso 3 — Registrar en `prompt_resolver.py`**

Abrir `app/services/supervisor/prompt_resolver.py` y añadir el nombre del agente al mapeo de resolución histórica de prompts. Esto permite al Supervisor evaluar el agente usando el prompt que estaba activo en el momento del run (trazabilidad histórica via `git show`).

**Paso 4 — Registrar en `supervisor_runner.py`**

Abrir `app/services/supervisor/supervisor_runner.py`:

1. Importar el nuevo evaluador:
   ```python
   from app.services.supervisor.evaluators.mi_nuevo_agente_evaluator import MiNuevoAgenteEvaluator
   ```

2. Añadirlo a la lista `_EVALUATORS`:
   ```python
   _EVALUATORS = [
       PlannerEvaluator(),
       ...
       MiNuevoAgenteEvaluator(),   # ← añadir aquí
   ]
   ```

**Paso 5 — Registrar en `aggregate_builder.py`** (si aplica)

Si el nuevo agente es relevante para el análisis agregado multi-proyecto, añadir su `AGENT_NAME` al mapeo en `app/services/supervisor/aggregate_builder.py`. Esto hace que el análisis agregado incluya los hallazgos del nuevo agente en la tabla de frecuencias y en el corpus de texto enviado al LLM.

**Paso 6 — Escribir tests**

```
tests/services/supervisor/test_mi_nuevo_agente_evaluator.py
```

Cubrir obligatoriamente:
- Retorna `EvaluatorOutput(result=None)` cuando no hay datos (not_supervised).
- Llama al LLM y mapea correctamente el output cuando hay datos.
- Reintenta en caso de output inválido (si el evaluador implementa retry).

Ver `tests/services/supervisor/test_planner_evaluator.py` como referencia de patrón completo.

**Paso 7 — Ejecutar la suite completa**

```bash
poetry run pytest -q
```

Todos los tests (1060+) deben seguir pasando.

### 7.5 Evaluadores para pares ejecutor-validador

Si el nuevo agente tiene un validador asociado (§5), se necesitan **dos evaluadores independientes**: uno para el ejecutor y otro para el validador.

| Evaluador | `AGENT_NAME` | Fuente de datos |
|---|---|---|
| `MiNuevoAgenteEvaluator` | `"mi_nuevo_agente"` | `ExecutionRun` rows donde el ejecutor corrió |
| `MiNuevoAgenteValidatorEvaluator` | `"mi_nuevo_agente_validator"` | Pares de runs con artefactos de validación |

Los helpers en `_pair_evaluation_helpers.py` facilitan la carga de pares ejecutor-validador desde la BD. Los evaluadores de pares existentes (`code_change_agent_evaluator.py` + `code_change_agent_validator_evaluator.py`) son la referencia canónica.

Ambos evaluadores se registran por separado en `_EVALUATORS` en `supervisor_runner.py`.

---

## 8. Convención de versiones [OBLIGATORIO]

La versión del YAML es **la fuente de verdad para el versionado de prompts**. Incrementarla siempre que cambie el input efectivo al LLM.

### Cuándo incrementar

| Cambio | ¿Incrementar? |
|---|---|
| Editar cualquier texto en `content:` | **Sí** |
| Añadir una entrada a `user_prompt_inputs:` | **Sí** |
| Eliminar una entrada de `user_prompt_inputs:` | **Sí** |
| Cambiar `required:` en un input existente | **Sí** |
| Cambiar `description:` en un input existente | **Sí** |
| Renombrar una clave de input | **Sí** |
| Editar solo `description:` a nivel de prompt (metadatos) | No |
| Editar lógica del builder Python sin cambiar el contenido inyectado | No |

### Cómo incrementar

Usar versionado semántico. El formato es siempre un string `"MAJOR.MINOR.PATCH"`:

- **PATCH** (`1.0.0` → `1.0.1`): corrección de errata, ajuste de redacción, mejora menor del prompt.
- **MINOR** (`1.0.0` → `1.1.0`): nuevo input añadido, descripción de input existente modificada, refinamiento de comportamiento.
- **MAJOR** (`1.0.0` → `2.0.0`): reescritura completa del prompt, rol del agente redefinido, cambio incompatible en el esquema de inputs.

Añadir siempre una entrada en `changelog`:

```yaml
version: "1.1.0"
changelog:
  - version: "1.1.0"
    date: "AAAA-MM-DD"
    changes: "Añadido input contexto_opcional; aclarada descripción de la definición de tarea"
  - version: "1.0.0"
    date: "2026-05-28"
    changes: "Versión inicial"
```

### Aplicación del contrato

`validate_builder_inputs` se llama en tiempo de ejecución dentro de cada función builder. Si se añade un nuevo input al builder sin actualizar antes el YAML, la aplicación lanza `ValueError` en la primera llamada. El mensaje de error indica exactamente qué hay que añadir al YAML.

---

## 9. Lista de verificación rápida

Usar esta lista para cada nuevo agente. Marcar **cada** ítem antes de dar el trabajo por terminado. La sección de supervisor es obligatoria para todos los agentes sin excepción.

### Todo agente (todas las categorías)

- [ ] Fichero YAML creado en el directorio correcto `app/prompts/<categoría>/`
- [ ] `agent_name` coincide con el nombre del fichero (sin `.yaml`)
- [ ] `version` empieza en `"1.0.0"`
- [ ] `changelog` tiene al menos una entrada
- [ ] Todas las claves de prompt principal tienen `description`, `user_prompt_inputs` y `content: |-`
- [ ] Cada builder de retry tiene su propia clave en el YAML con `description` y `user_prompt_inputs` (sin `content:`)
- [ ] Constante del system prompt definida vía `prompt_loader.get()` (no un string literal)
- [ ] **Todo** builder de user prompt (principal y retry) llama a `validate_builder_inputs` con un dict explícito
- [ ] El dict `inputs` tiene **exactamente** las mismas claves que el YAML — ni una más, ni una menos
- [ ] Los inputs `required: false` también están presentes en el dict (su valor puede ser `None`)
- [ ] Regla de importación circular aplicada si el módulo está bajo `conversation/`, `analysis/` o `environment/`
- [ ] Tests escritos y pasando
- [ ] `poetry run pytest -q` → 0 fallos

### Adicional: subagente de ejecución

- [ ] Implementa `BaseSubagent` con el atributo de clase `name`
- [ ] Registrado en `SubagentRegistry` en `orchestrated_engine.py`
- [ ] `SubagentCapability` añadida a `capabilities.py` con `role`, `strengths`, `limits`, `usage_guidance`
- [ ] Nombre añadido a `_allowed_subagents_for_phase("execution")` en `orchestrator.py`
- [ ] Nombre añadido al conjunto en `_last_attempted_subagent_name()` en `orchestrator.py`
- [ ] Decisión tomada: añadir a `IGNORED_VALIDATION_PRODUCERS` (infraestructura) O escribir un validador (entregable)

### Adicional: validador

- [ ] Implementa `BaseTaskValidator` con `validator_key` y `producer_key`
- [ ] `producer_key` coincide con el `name` del subagente
- [ ] Registrado en `ValidationRegistry` en `registry.py`
- [ ] Fichero YAML del validador creado en `app/prompts/validation/`

### Adicional: supervisor (todo agente — sin excepciones)

- [ ] Fichero de evaluador creado en `app/services/supervisor/evaluators/mi_nuevo_agente_evaluator.py`
- [ ] `AGENT_NAME` es único en todo el sistema (comprobar que no existe ya en `supervisor_runner.py`)
- [ ] Si el agente tiene validador: `AGENT_NAME` del evaluador del validador es `"<agente>_validator"`, no `"<agente>"`
- [ ] Guard de retorno anticipado implementado: `if not data: return EvaluatorOutput(result=None)`
- [ ] Fichero YAML del evaluador creado en `app/prompts/supervisor/mi_nuevo_agente_evaluator.yaml`
- [ ] Nombre del agente registrado en `prompt_resolver.py`
- [ ] Evaluador añadido a `_EVALUATORS` en `supervisor_runner.py`
- [ ] Evaluador añadido a `aggregate_builder.py` si es relevante para análisis multi-proyecto
- [ ] Test de not_supervised (sin datos → `result=None`) escrito
- [ ] Test de llamada LLM y mapeo de output escrito
- [ ] `poetry run pytest -q` → 0 fallos

---

## 10. QA Agents — categoría especial

Los QA agents son agentes post-ejecución que sondean el producto para encontrar fallos. No son subagentes de ejecución ni evaluadores conversacionales — tienen su propio motor (`QAOrchestrator`) y su propio registro.

### Diferencias respecto a subagentes de ejecución

| Aspecto | Subagente de ejecución | QA agent |
|---|---|---|
| Clase base | `BaseSubagent` | `BaseQAAgent` |
| Registro | `SubagentRegistry` en `orchestrated_engine.py` | `QAAgentRegistry` en `app/services/qa/strategies/registry.py` |
| Output | `ResolutionState` con `ExecutionEvidence` | `QASession` con `QAEvidence` y `QAFinding` |
| Directorio | `app/execution_engine/subagents/` | `app/services/qa/qa_agents/` |
| Prompts YAML | `app/prompts/execution/` | `app/prompts/qa/` |

### Pasos obligatorios para un nuevo QA agent

1. **YAML** en `app/prompts/qa/<nombre>.yaml` — mismo formato estándar (§2)
2. **Módulo Python** en `app/services/qa/qa_agents/<nombre>.py` — implementa `BaseQAAgent`
3. **Sin registro en SubagentRegistry** — el QA agent se registra en su propia estrategia
4. **Tests** en `tests/services/qa/test_<nombre>.py`
5. **Evaluador Supervisor** en `app/services/supervisor/evaluators/<nombre>_evaluator.py`
   - Fuente de datos: `qa_sessions` + `qa_findings` (no `execution_runs`)
   - Guard de retorno anticipado obligatorio: `if not data: return EvaluatorOutput(result=None)`

### `BaseQAAgent` — interfaz mínima

```python
class BaseQAAgent:
    name: str  # debe coincidir con agent_name del YAML

    def run(
        self,
        *,
        qa_session: QASession,
        strategy: QAStrategy,
    ) -> QASession:
        """Ejecuta el sondeo y devuelve el estado actualizado."""
        ...
```

Todos los pasos del checklist §9 aplican íntegramente, con las diferencias de directorio y clase base indicadas arriba.

---

## Apéndice A — API de PromptLoader

```python
from app.services.prompt_loader import prompt_loader

# Obtener contenido del system prompt
prompt = prompt_loader.get("nombre_agente")               # clave por defecto "main"
prompt = prompt_loader.get("nombre_agente", "otra_clave") # clave explícita

# Obtener versión actual
version = prompt_loader.get_version("nombre_agente")

# Obtener el PromptSpec completo (para el Supervisor o introspección)
spec = prompt_loader.get_spec("nombre_agente")
all_specs = prompt_loader.all_specs()

# Aplicar contrato de inputs del builder contra la declaración YAML
prompt_loader.validate_builder_inputs(
    "nombre_agente",
    "clave_prompt",
    {"nombre_input": valor, ...}
)
```

`PromptLoader` es un singleton lazy. Todos los ficheros YAML se cargan en la primera llamada y se cachean en proceso durante toda la vida de la aplicación.

---

## Apéndice B — Referencia de ubicación de ficheros

```
app/
  prompts/
    execution/              ← YAMLs de subagentes y herramientas de ejecución
    planning/               ← YAMLs de planner, refiner, sequencer
    recovery/               ← YAMLs de servicios de recuperación
    conversation/           ← YAMLs de aria y evaluadores
    validation/             ← YAMLs de validadores
    environment/            ← YAMLs de servicios de entorno
    analysis/               ← YAMLs de servicios de análisis
  execution_engine/
    subagents/
      base.py               ← BaseSubagent, SubagentRejectedStepError
      mi_nuevo_agente.py    ← implementación del nuevo subagente
    engines/
      orchestrated_engine.py ← instanciación del SubagentRegistry
    capabilities.py         ← declaraciones SubagentCapability
    orchestrator.py         ← enrutamiento de fases, _allowed_subagents_for_phase
    subagent_registry.py    ← clase SubagentRegistry (no modificar)
  services/
    prompt_loader.py        ← PromptLoader, validate_builder_inputs
    validation/
      base.py               ← BaseTaskValidator
      registry.py           ← ValidationRegistry
      selection.py          ← IGNORED_VALIDATION_PRODUCERS
      validators/           ← implementaciones de validadores
```
