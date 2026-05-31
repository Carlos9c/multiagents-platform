# Motor QA — Referencia Completa

El Motor QA es un sistema de aseguramiento de calidad autónomo y adversarial que se ejecuta cuando un proyecto alcanza el estado `COMPLETED`. Realiza análisis automatizado multidimensional usando un portafolio de agentes de sondeo especializados, orquestados por un bucle de decisión LLM, y sintetiza los hallazgos en un veredicto y un backlog de remediación priorizado.

---

## Tabla de Contenidos

1. [Visión general de la arquitectura](#visión-general-de-la-arquitectura)
2. [Flujo de extremo a extremo](#flujo-de-extremo-a-extremo)
3. [Puntos de entrada](#puntos-de-entrada)
4. [Modelos de datos](#modelos-de-datos)
5. [Contratos](#contratos)
6. [Capa de estrategias](#capa-de-estrategias)
7. [Bootstrapper](#bootstrapper)
8. [Orquestador](#orquestador)
9. [Presupuesto](#presupuesto)
10. [Estado de sesión QA (en memoria)](#estado-de-sesión-qa-en-memoria)
11. [Agentes de sondeo](#agentes-de-sondeo)
    - [Agentes Fase 5](#agentes-fase-5-nivel-ejecución)
    - [Agentes Fase 7](#agentes-fase-7-escenarios--corpus)
    - [Agentes Fase 8](#agentes-fase-8-adversarial--owasp)
    - [Agentes Fase 9](#agentes-fase-9-rendimiento--regresión)
    - [Agente de síntesis](#agente-de-síntesis)
12. [Lector de fuentes](#lector-de-fuentes)
13. [Ejecutor Docker](#ejecutor-docker)
14. [Capa de persistencia](#capa-de-persistencia)
15. [Integración con Aria](#integración-con-aria)
16. [Endpoints de la API](#endpoints-de-la-api)
17. [Evaluadores del supervisor](#evaluadores-del-supervisor)
18. [Cómo añadir un nuevo agente QA](#cómo-añadir-un-nuevo-agente-qa)
19. [Límites conocidos y notas operacionales](#límites-conocidos-y-notas-operacionales)

---

## Visión general de la arquitectura

```
                 ┌──────────────────────────────────────┐
                 │            QA Runner                 │
                 │  (hilo de fondo, sesión DB propia)   │
                 └───────────────┬──────────────────────┘
                                 │ QARequest
                                 ▼
                 ┌──────────────────────────────────────┐
                 │             QA Engine                │
                 │  1. select(product_type) → strategy  │
                 │  2. QABootstrapper.bootstrap()        │
                 │  3. QAOrchestrator.run()              │
                 │  4. _persist_result() → DB           │
                 └───────────────┬──────────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
              ▼                  ▼                  ▼
       QABootstrapper     QAOrchestrator      Persistencia DB
     (verificar artefacto  (bucle LLM de     (QASession +
      / compilar APK)       sondeo +          filas QAFinding)
                            síntesis)
                                 │
              ┌──────────────────┼──────────────────┐
              │         LLM decide qué agente        │
              │         llamar en cada ronda         │
              │                  │                  │
              ▼                  ▼                  ▼
        Agentes Fase 5    Agentes Fase 7-9   synthesis_agent
        (sondeos Docker)  (análisis LLM      (veredicto +
                           estructurado)      remediación)
```

---

## Flujo de extremo a extremo

```
Proyecto alcanza estado COMPLETED
        │
        ▼ evento PROJECT_COMPLETED
Aria establece conversation.qa_offer_pending = True
        │
        ▼ siguiente mensaje del usuario
QATool.execute(hint=None) → Situación A1: construir payload de oferta
Aria envía mensaje de oferta al usuario
        │
        ▼ usuario responde
QATool.execute(hint="sí") → Situación A2
  └─ _evaluate_yes_no() → True  (llamada LLM; si falla: False)
  └─ create_qa_session(db, project_id)  → fila QASession (status=running)
  └─ commit
  └─ ThreadPoolExecutor.submit(run_qa_background, project_id, qa_session_id)
  └─ devuelve status="qa_started"
Aria transiciona conversation.phase = qa_running
        │
        ▼ (hilo de fondo)
run_qa_background()
  └─ abre SessionLocal propia
  └─ _run(db, project_id, qa_session_id)
        │
        ▼
QAEngine.run(db, request)
  ├─ strategy = select(product_type)           # lanza ValueError si tipo desconocido
  ├─ db_session.strategy_used = product_type   # se establece inmediatamente
  ├─ QABootstrapper.bootstrap()                # no-op para la mayoría; compila APK para Android
  │       └─ devuelve (request, None)  o  (request, resultado_bloqueado)
  │
  └─ QAOrchestrator.run(db, request, strategy)
          │
          ├── Fase 1: Sondeo (dirigido por LLM)
          │     ┌──────────────────────────────────────┐
          │     │  BUCLE (max_probe_rounds=8)           │
          │     │  Guardas evaluadas ANTES de cada LLM: │
          │     │  - timeout (300 s)                    │
          │     │  - max_probe_rounds alcanzado          │
          │     │  - max_findings acumulados (20)        │
          │     │  LLM decide: call_agent / finish       │
          │     │  Validaciones DESPUÉS de la decisión: │
          │     │  - synthesis_agent excluido del sondeo │
          │     │  - sin repetición consecutiva          │
          │     │  - agente en lista de estrategia       │
          │     │  - límite por agente (5)               │
          │     │  - agente registrado                   │
          │     └──────────────────────────────────────┘
          │
          └── Fase 2: Síntesis (determinista)
                synthesis_agent agrega hallazgos
                → veredicto final + tareas de remediación

QAEngine._persist_result()
  └─ QASession: status=COMPLETED|BLOCKED, verdict, findings_summary, probes (JSON), duration

_persist_findings()
  └─ una fila QAFinding por hallazgo (con producer_agent)

_notify_aria()
  └─ busca conversación activa del proyecto; si no existe → termina sin notificar
  └─ active_conv.pending_qa_report = result.model_dump_json()
  └─ process_with_pre_transitions(QA_COMPLETED, event_data)
       donde event_data incluye: verdict, has_findings, critical_count,
       high_count, findings_summary, remediation_tasks, error_message
  └─ db.commit()
  └─ WebSocket broadcast
        │
        ▼
El usuario ve resumen de hallazgos + oferta de remediación
        │
        ▼ usuario acepta
QATool._handle_remediation_response()
  └─ _evaluate_yes_no() → True
  └─ Filas Task creadas (una por RemediationTask)
  └─ Las tareas entran al backlog de ejecución
```

---

## Puntos de entrada

| Punto de entrada | Ubicación | Cuándo se usa |
|---|---|---|
| `run_qa_background(project_id, qa_session_id)` | `qa_runner.py` | Hilo de fondo desde API o QATool |
| `POST /qa/{project_id}/run` | `app/api/qa.py` | Disparo directo por API (testing, CI) |
| `QATool.execute(db, project_id, conv, hint)` | `qa_tool.py` | Integración con conversación de Aria |

---

## Modelos de datos

### `QASession` (`app/models/qa_session.py`)

Registro persistente en base de datos de cada ejecución QA.

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | PK | Auto-incremento |
| `project_id` | FK → projects | Proyecto propietario |
| `triggered_by` | `String(20)` | Siempre `"user"` |
| `status` | `String(20)` | `pending / running / completed / failed / blocked` |
| `product_type` | `String(50)` | Copiado del proyecto al crear la sesión |
| `strategy_used` | `String(50)` | Establecido por el motor al inicio de la ejecución |
| `verdict` | `String(30)` | `passed / partial / failed / blocked` |
| `agents_called` | `JSON` | Lista ordenada de nombres de agentes que ejecutaron |
| `findings_summary` | `JSON` | Dict `{severidad: cantidad}` |
| `probes` | `JSON` | Lista de dicts `ProbeRecord` serializados |
| `duration_seconds` | `Float` | Tiempo de pared de la ejecución completa |
| `error_message` | `Text` | Solo cuando ocurrió un error irrecuperable |
| `created_at` | `DateTime` | Timestamp de creación de la sesión |
| `completed_at` | `DateTime` | Establecido por `_persist_result` |

### `QAFinding` (`app/models/qa_finding.py`)

Un hallazgo individual persistido tras cada ejecución QA.

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | PK | Auto-incremento |
| `qa_session_id` | FK → qa_sessions | Sesión propietaria |
| `finding_id` | `String(36)` | ID asignado por el agente (ej. `"fqa-001"`, `"bqa-BC-003"`) |
| `severity` | `String(20)` | `critical / high / medium / low / info` |
| `category` | `String(30)` | Ver `VALID_QA_CATEGORIES` |
| `title` | `String(255)` | Descripción corta |
| `description` | `Text` | Explicación completa |
| `reproduction_steps` | `JSON` | Lista ordenada de pasos |
| `evidence` | `JSON` | Dict de evidencia libre |
| `affected_component` | `String(255)` | Archivo, módulo, endpoint, etc. |
| `auto_remediable` | `Boolean` | Si una herramienta puede corregirlo automáticamente |
| `remediation_hint` | `Text` | Sugerencia concreta de corrección |
| `remediation_task_id` | FK → tasks | Establecido cuando se creó una Task de remediación |
| `producer_agent` | `String(50)` | Nombre del agente QA que produjo este hallazgo |
| `created_at` | `DateTime` | Timestamp de creación de la fila |

**Categorías válidas:** `functional`, `security`, `performance`, `usability`, `accessibility`, `compatibility`, `reliability`, `data_integrity`, `boundary`, `adversarial`, `regression`

**Veredictos válidos:** `passed`, `partial`, `failed`, `blocked`

---

## Contratos

Todos los contratos QA viven en `app/services/qa/contracts.py`.

### `QARequest`

Entrada al motor para una ejecución individual:

```python
class QARequest(BaseModel):
    project_id: int
    qa_session_id: int
    product_type: str
    project_goal: str
    source_path: str
    workspace_path: str
    artifact_path: str | None = None   # establecido por QABootstrapper para Android
```

### `QAFindingDetail`

Un hallazgo producido durante el sondeo:

```python
class QAFindingDetail(BaseModel):
    finding_id: str
    severity: Literal["critical", "high", "medium", "low", "info"]
    category: Literal["functional", "security", "performance", ...]
    title: str
    description: str
    reproduction_steps: list[str]
    affected_component: str | None
    auto_remediable: bool
    remediation_hint: str | None
    evidence: dict[str, Any]
    producer_agent: str | None     # establecido por cada agente para atribuir el hallazgo
```

### `ProbeRecord`

Registro de un intento de sondeo por cada llamada a un agente:

```python
class ProbeRecord(BaseModel):
    agent_name: str
    probe_type: str
    target: str
    outcome: Literal["passed", "failed", "blocked", "skipped"]
    duration_seconds: float | None
    findings_count: int
    notes: str | None
    raw_output: str | None
```

### `QAResult`

Salida final del motor:

```python
class QAResult(BaseModel):
    verdict: Literal["passed", "failed", "blocked", "partial"]
    findings: list[QAFindingDetail]
    probes: list[ProbeRecord]
    remediation_tasks: list[RemediationTask]
    agents_called: list[str]
    duration_seconds: float | None
    error_message: str | None
```

---

## Capa de estrategias

Cada tipo de producto tiene una `QAStrategy` (`app/services/qa/strategies/`) que define qué agentes están permitidos y si se necesita un artefacto compilado antes del sondeo.

| Tipo de producto | `requires_compiled_artifact` | Agentes de sondeo disponibles |
|---|---|---|
| `rest_api` | No | functional_tester, security_scanner, contract_validator, performance_profiler + los 6 de Fase 7-9 |
| `graphql_api` | No | functional_tester, security_scanner, contract_validator + 5 de Fase 7-9 (sin performance_qa_agent) |
| `web_app` | No | functional_tester, security_scanner, performance_profiler + los 6 de Fase 7-9 |
| `cli_tool` | No | functional_tester, security_scanner + 5 de Fase 7-9 (sin security_qa_agent) |
| `library` | No | functional_tester, security_scanner, contract_validator + 4 de Fase 7-9 (sin security_qa_agent, sin performance_qa_agent) |
| `mobile_android` | **Sí** | apk_installer, functional_tester, security_scanner + 5 de Fase 7-9 (sin performance_qa_agent) |
| `desktop_app` | No | functional_tester, security_scanner + 4 de Fase 7-9 (sin security_qa_agent, sin performance_qa_agent) |

El registro de estrategias (`strategies/registry.py`) es un dict; `strategy_selector.select(product_type)` lanza `ValueError` para cualquier tipo no elegible para QA.

---

## Bootstrapper

`QABootstrapper` (`qa_bootstrapper.py`) se ejecuta antes del orquestador para tipos de producto que necesitan un artefacto compilado (actualmente solo `mobile_android`).

**Pasos:**

1. Busca APK existente bajo `source_path` con patrones glob — si se encuentra, establece `request.artifact_path` y retorna inmediatamente.
2. Usa LLM para detectar el comando de compilación correcto leyendo `build.gradle`, `pubspec.yaml`, `package.json`, `README.*`, etc.
3. Si el LLM no puede detectar el comando → retorna `QAResult(verdict="blocked", ...)`.
4. Ejecuta el comando de compilación dentro del contenedor Docker del proyecto (timeout: 600 s).
5. Si el build falla → retorna `QAResult(verdict="blocked", ...)`.
6. Busca nuevamente el APK producido con los patrones glob estándar. Si no lo encuentra y el LLM sugirió un `output_pattern`, lo intenta adicionalmente con ese patrón.
7. Si no se encuentra ningún artefacto → retorna `QAResult(verdict="blocked", ...)`.
8. En caso de éxito → retorna `(request_actualizado, None)`.

Para todos los demás tipos de producto, `bootstrap()` es un no-op y retorna inmediatamente.

---

## Orquestador

`QAOrchestrator` (`qa_orchestrator.py`) dirige dos fases:

### Fase 1 — Sondeo (dirigido por LLM)

En cada ronda, el orquestador llama a `_decide()` que realiza una llamada LLM estructurada devolviendo:

```json
{"action": "call_agent", "agent_name": "security_scanner", "reasoning": "..."}
```

o

```json
{"action": "finish", "agent_name": null, "reasoning": "..."}
```

**Restricciones de presupuesto (evaluadas ANTES de cada llamada LLM — no pueden ser sobreescritas):**

| Restricción | Efecto |
|---|---|
| `elapsed >= timeout_seconds` | Para el sondeo; marca `budget_exhausted = True` |
| `round_num >= max_probe_rounds` | Para el sondeo; marca `budget_exhausted = True` |
| `total_findings >= max_findings` | Para el sondeo automáticamente |

**Validaciones de la decisión LLM (evaluadas DESPUÉS de recibir la decisión, en este orden):**

| Validación | Comportamiento |
|---|---|
| `agent_name == "synthesis_agent"` | Saltado; synthesis_agent está excluido del bucle de sondeo |
| `agent_name == last_agent_called` | Saltado; regla de no repetición consecutiva |
| `not strategy.is_agent_allowed(agent_name)` | Saltado; el agente no está en la lista de la estrategia |
| `call_count(agent_name) >= max_probes_per_agent` | Saltado; límite por agente alcanzado |
| `registry.get(agent_name) is None` | Saltado; agente no registrado |

Cuando la llamada LLM falla → el sondeo termina de forma controlada (sin excepción).

**Orden de sondeo recomendado (del prompt del orquestador):**

1. `security_scanner` → `security_qa_agent`
2. `functional_tester` → `functional_qa_agent`
3. `boundary_qa_agent`
4. `adversarial_qa_agent`
5. `contract_validator` (solo para tipos API/library)
6. `performance_profiler` → `performance_qa_agent` (solo si no hay hallazgos críticos)
7. `apk_installer` (solo para `mobile_android`)
8. `regression_qa_agent` (siempre el último agente de sondeo)

### Fase 2 — Síntesis (determinista)

Tras terminar el sondeo, `synthesis_agent` es llamado directamente (no a través del bucle LLM) para agregar todos los hallazgos y producir el veredicto final más las tareas de remediación. Si la llamada LLM de síntesis falla, el veredicto se deriva de forma determinista desde las severidades de los hallazgos (ver sección [Agente de síntesis](#agente-de-síntesis)).

---

## Presupuesto

`QABudget` (`budget.py`) controla los límites de recursos:

| Parámetro | Valor por defecto | Significado |
|---|---|---|
| `max_probe_rounds` | 8 | Máximo de rondas de decisión LLM en la Fase 1 |
| `timeout_seconds` | 300 | Timeout de pared para la fase de sondeo |
| `max_findings` | 20 | Para el sondeo automáticamente cuando se acumula esta cantidad |
| `max_probes_per_agent` | 5 | Máximo de llamadas a cualquier agente individual |
| `max_steps` | 12 | Campo legado (no usado por el orquestador actual) |
| `max_agent_calls` | 10 | Campo legado (no usado por el orquestador actual) |

El agotamiento del presupuesto establece `session.budget_exhausted = True`, lo que hace que el agente de síntesis tienda hacia el veredicto `"partial"`.

---

## Estado de sesión QA (en memoria)

`QASession` (`app/services/qa/qa_session.py`) es el objeto de estado mutable que se enhebra a través del bucle del orquestador — análogo a `ResolutionState` en el motor de ejecución. **No confundir con el modelo DB `QASession` de `app/models/qa_session.py`.**

```python
class QASession(BaseModel):
    request: QARequest
    phase: Literal["discovery", "probing", "synthesis", "complete"]
    agents_called: list[str]           # para control de no-repetición y presupuesto
    probes: list[ProbeRecord]          # uno por llamada a agente
    findings: list[QAFindingDetail]    # hallazgos acumulados
    notes: list[str]                   # notas de trazabilidad del orquestador
    step_count: int
    budget_exhausted: bool
    synthesis_verdict: str | None
    synthesis_error: str | None
    remediation_tasks: list[RemediationTask]
```

**Invariantes clave:**
- `session.record_agent_call(name)` debe llamarse al inicio de cada `probe()`.
- `session.add_probe(ProbeRecord(...))` debe llamarse al final de cada `probe()`.
- Los agentes nunca lanzan excepciones ante fallos de sondeo esperados — retornan `ProbeRecord(outcome="skipped")`.
- Solo `QAAgentError` (fallo de infraestructura irrecuperable) puede lanzarse.

---

## Agentes de sondeo

Todos los agentes implementan `BaseQAAgent` de `app/services/qa/agents/base.py` y están registrados en `build_default_registry()` en `app/services/qa/agents/registry.py`.

### Agentes Fase 5 (nivel ejecución)

Estos agentes ejecutan comandos dentro del contenedor Docker del proyecto y realizan análisis libre de fuentes.

#### `functional_tester`

- **Archivo:** `app/services/qa/agents/functional_tester.py`
- **Tipo de sondeo:** `functional_test`
- **Qué hace:** Ejecuta la suite de tests del proyecto en Docker; usa LLM para evaluar la corrección del código fuente.
- **Docker:** Prueba en secuencia `pytest`, `npm test`, `cargo test`, `go test ./...`.

#### `security_scanner`

- **Archivo:** `app/services/qa/agents/security_scanner.py`
- **Tipo de sondeo:** `security_scan`
- **Qué hace:** Análisis estático LLM de forma libre contra OWASP Top 10 y vulnerabilidades comunes.
- **Diferencia con `security_qa_agent`:** Análisis no estructurado vs. checklist por ítem estructurado.

#### `contract_validator`

- **Archivo:** `app/services/qa/agents/contract_validator.py`
- **Tipo de sondeo:** `contract_validation`
- **Qué hace:** Lee archivos de esquema OpenAPI/GraphQL; valida contratos contra la implementación.
- **Usado por:** Estrategias `rest_api`, `graphql_api`, `library`.

#### `performance_profiler`

- **Archivo:** `app/services/qa/agents/performance_profiler.py`
- **Tipo de sondeo:** `performance_profile`
- **Qué hace:** Ejecuta comandos de temporización en Docker; análisis libre de cuellos de botella.

#### `apk_installer`

- **Archivo:** `app/services/qa/agents/apk_installer.py`
- **Tipo de sondeo:** `apk_install`
- **Qué hace:** Instala y hace smoke-test del APK Android en un emulador ADB conectado.
- **Usado por:** Solo estrategia `mobile_android`.

### Agentes Fase 7 (escenarios / corpus)

#### `functional_qa_agent`

- **Archivo:** `app/services/qa/qa_agents/functional_qa_agent.py`
- **Tipo de sondeo:** `functional_analysis`
- **Diseño:** **Análisis LLM en dos pasadas.**
  - Pasada 1: Genera 5-12 escenarios de test priorizados (casos felices, borde, error) desde el código fuente.
  - Pasada 2: Evalúa cada escenario contra el código fuente (opcionalmente usando la salida de tests Docker como evidencia primaria).
- **IDs de hallazgos:** `fqa-{número}` (ej. `fqa-001`)
- **Categoría:** `functional`
- **Saltado cuando:** No se encuentran archivos fuente; falla el LLM de la Pasada 1; falla el LLM de la Pasada 2.

#### `boundary_qa_agent`

- **Archivo:** `app/services/qa/qa_agents/boundary_qa_agent.py`
- **Tipo de sondeo:** `boundary_analysis`
- **Diseño:** Usa un **corpus determinista** de casos de test de borde por tipo de producto. El LLM solo identifica qué ítems del corpus la implementación maneja incorrectamente — no inventa casos de test.
- **Tamaño del corpus:** 6-12 ítems específicos por tipo de producto + 2 ítems universales (`unicode_bomb`, `format_string`).
- **IDs de hallazgos:** Derivados de IDs del corpus (ej. `bqa-BC-001`).
- **Categoría:** `boundary`

### Agentes Fase 8 (adversarial / OWASP)

#### `adversarial_qa_agent`

- **Archivo:** `app/services/qa/qa_agents/adversarial_qa_agent.py`
- **Tipo de sondeo:** `adversarial_analysis`
- **Diseño:** El LLM genera escenarios de ataque realistas *específicos al tipo de producto y su implementación*, luego evalúa si el código es vulnerable. Solo reporta casos con evidencia clara a nivel de código.
- **IDs de hallazgos:** `aqa-{número}`
- **Categoría:** `adversarial`

#### `security_qa_agent`

- **Archivo:** `app/services/qa/qa_agents/security_qa_agent.py`
- **Tipo de sondeo:** `owasp_checklist`
- **Diseño:** Ejecuta un **checklist fijo OWASP Top 10 (2021)** en una única llamada LLM batch. Produce veredicto pasa/falla por ítem con evidencia, más hallazgos estructurados para los ítems fallidos.
- **Ítems del checklist:** A01–A10 (Control de Acceso Roto → SSRF).
- **IDs de hallazgos:** Asignados por el agente (ej. `sqa-A03-001`).
- **Categoría:** `security`

### Agentes Fase 9 (rendimiento / regresión)

#### `performance_qa_agent`

- **Archivo:** `app/services/qa/qa_agents/performance_qa_agent.py`
- **Tipo de sondeo:** `performance_analysis`
- **Diseño:** Enfoque en dos capas:
  - **Determinista:** Ejecuta comandos de temporización en Docker; compara contra umbrales específicos por tipo de producto. Las violaciones producen hallazgos `pqa-T001` inmediatamente sin LLM.
  - **LLM:** Analiza el código fuente para cuellos de botella adicionales (queries N+1, E/S síncrona en contextos asíncronos, paginación faltante, etc.) produciendo hallazgos `pqa-S{número}`.
- **Umbrales:**

| Tipo de producto | Métrica | Umbral |
|---|---|---|
| `rest_api` / `graphql_api` | inicio | 10 s |
| `rest_api` / `graphql_api` | respuesta | 500 ms |
| `web_app` | inicio | 15 s |
| `web_app` | carga de página | 3000 ms |
| `cli_tool` | ejecución | 30 s |
| `library` | importación | 500 ms |
| `mobile_android` | inicio | 3 s |
| `desktop_app` | inicio | 5 s |

#### `regression_qa_agent`

- **Archivo:** `app/services/qa/qa_agents/regression_qa_agent.py`
- **Tipo de sondeo:** `regression_analysis`
- **Diseño:** Consulta la BD para la sesión QA completada más reciente del mismo proyecto. Usa LLM para comparar semánticamente los hallazgos — reporta solo hallazgos **nuevos** en la sesión actual (regresiones). `resolved_count` registra hallazgos previamente reportados que ya no están presentes.
- **Saltado cuando:** No existe sesión QA completada previa (primera ejecución).
- **IDs de hallazgos:** Asignados por el agente (ej. `rqa-001`).
- **Categoría:** `regression`

### Agente de síntesis

#### `synthesis_agent`

- **Archivo:** `app/services/qa/agents/synthesis_agent.py`
- **Tipo de sondeo:** `synthesis`
- **Llamado por:** `QAOrchestrator._run_synthesis()` directamente (no a través del bucle LLM de sondeo).
- **Diseño:** Recibe TODOS los hallazgos acumulados de todos los agentes de sondeo; el LLM sintetiza un veredicto final y lista priorizada de tareas de remediación.
- **Veredictos que puede producir el LLM:** `passed`, `partial`, `failed`, `blocked`.
- **Fallback determinístico** (cuando la llamada LLM falla):

| Condición | Veredicto |
|---|---|
| Sin hallazgos | `passed` |
| Presupuesto agotado (independientemente de hallazgos) | `partial` |
| Tiene hallazgos `critical` o `high` | `failed` |
| Solo hallazgos `medium` / `low` / `info` | `partial` |

> **Nota:** El veredicto `blocked` a nivel de sesión NO lo produce el agente de síntesis en el fallback — lo produce `QAEngine` cuando el bootstrapper bloquea o cuando ocurre un error fatal irrecuperable en el motor.

---

## Lector de fuentes

`source_reader.py` lee los archivos fuente más relevantes del árbol del proyecto para análisis LLM.

**Orden de selección:**
1. Archivos de prioridad universal (`README.md`, `README.rst`, `README.txt`, `readme.md`)
2. Patrones específicos por tipo de producto (ver `_PRIORITY_PATTERNS` para el mapeo completo)
3. Archivos de test (hasta 3 en total, 2 por patrón como máximo)

**Límites:**
- `_TOTAL_CHAR_BUDGET = 24.000` caracteres en todos los archivos
- `_MAX_FILE_CHARS = 6.000` caracteres por archivo
- Resultados de glob limitados a 5 archivos por patrón

`format_for_prompt(files)` renderiza los archivos como bloques `=== ruta ===\ncontenido` separados por líneas en blanco.

---

## Ejecutor Docker

`docker_runner.py` proporciona `run_in_project_container(project_id, command, cwd, timeout_seconds)`.

- Busca la `EnvironmentSession` activa del proyecto en `session_store`.
- Si no hay sesión → retorna `DockerRunResult(no_session=True)`.
- Los llamadores verifican `result.available` antes de interpretar la salida.
- Todos los agentes Fase 5 degradan de forma controlada cuando Docker no está disponible (resultado del sondeo = `"skipped"`).
- Los agentes Fase 7-9 no requieren Docker — realizan análisis puro de fuentes.

---

## Capa de persistencia

### `qa_runner._persist_findings`

Llamado por `_run()` tras retornar `QAEngine.run()`. Escribe una fila `QAFinding` por hallazgo en `QAResult.findings`, incluyendo `producer_agent`.

### `qa_engine._persist_result`

Llamado al final de cada `QAEngine.run()` independientemente del éxito. Actualiza:

- `db_session.status`: `BLOCKED` si `verdict == "blocked"`, en caso contrario `COMPLETED`.
- `db_session.verdict`, `agents_called`, `duration_seconds`, `error_message`, `completed_at`.
- `db_session.findings_summary`: almacenado como dict (columna JSON — sin `json.dumps` adicional).
- `db_session.probes`: lista de dicts `ProbeRecord.model_dump()`.
- `db_session.strategy_used`: establecido a `product_type` si no se estableció previamente.

### Migración Alembic

`alembic/versions/b2c3d4e5f6a7_add_producer_agent_and_probes.py` añade:
- `qa_findings.producer_agent` (String 50, nullable)
- `qa_sessions.probes` (JSON, nullable)

---

## Integración con Aria

El QA está completamente integrado en la máquina de estados de conversación de Aria a través de `QATool` (`app/services/conversation/aria/tools/qa_tool.py`).

### Flujo de estados

```
Proyecto COMPLETED
      │
      ▼ evento PROJECT_COMPLETED
Aria establece conversation.qa_offer_pending = True
      │
      ▼ siguiente mensaje del usuario
QATool.execute(hint=None)
  └─ Verificaciones en orden: B (qa_running?) → C/D (pending_qa_report?) → A (qa_offer_pending?)
  └─ Situación A1: construye payload de oferta
Aria envía mensaje de oferta al usuario
      │
      ▼ usuario responde
QATool.execute(hint="sí") → Situación A2
  └─ _evaluate_yes_no() → True  [LLM; ante fallo de LLM devuelve False → declina]
  └─ create_qa_session() + commit
  └─ ThreadPoolExecutor.submit(run_qa_background)
  └─ devuelve status="qa_started"
Aria transiciona conversation.phase = qa_running
      │
      ▼ (hilo de fondo completa)
qa_runner._notify_aria()
  └─ Busca conversación activa; si no existe → termina sin notificar
  └─ active_conv.pending_qa_report = QAResult JSON
  └─ process_with_pre_transitions(evento QA_COMPLETED)
  └─ WebSocket broadcast
      │
      ▼ siguiente mensaje del usuario (o inmediato)
QATool.execute(hint=None) → Situación C: resumir hallazgos
Aria presenta resumen de hallazgos + oferta de remediación
      │
      ▼ usuario responde
QATool.execute(hint="sí") → Situación D
  └─ _evaluate_yes_no() → True
  └─ Filas Task creadas a partir de remediation_tasks
  └─ devuelve status="remediation_confirmed"
Aria transiciona de vuelta a executing (o completed)
```

### Transiciones de fase disparadas por QATool

| `status` en ToolResult | Acción de Aria |
|---|---|
| `qa_offer_declined` | Limpia `qa_offer_pending`; permanece en completed |
| `qa_started` | Establece `phase=qa_running`; limpia `qa_offer_pending` |
| `qa_running` | Mensaje estático "QA en curso" (sin cambio de estado) |
| `qa_completed_with_findings` | Limpia `pending_qa_report` tras resumir; pregunta sobre remediación |
| `qa_completed_no_issues` | Limpia `pending_qa_report`; permanece en completed |
| `remediation_confirmed` | Limpia `pending_qa_report`; transiciona de vuelta a executing |
| `remediation_declined` | Limpia `pending_qa_report`; permanece en completed |

---

## Endpoints de la API

### `POST /qa/{project_id}/run`

Inicia una ejecución QA en background. Retorna inmediatamente con `qa_session_id`.

```json
{"qa_session_id": 42, "status": "running", "project_id": 7}
```

### `GET /qa/{project_id}/sessions`

Retorna el historial de sesiones QA de un proyecto (más reciente primero, hasta 10).

### `GET /qa/{project_id}/sessions/{qa_session_id}`

Retorna una única sesión QA incluyendo todas sus filas `QAFinding`.

```json
{
  "id": 42,
  "project_id": 7,
  "status": "completed",
  "product_type": "rest_api",
  "verdict": "failed",
  "agents_called": ["security_scanner", "security_qa_agent", "functional_qa_agent"],
  "findings_summary": {"critical": 0, "high": 2, "medium": 1, "low": 0, "info": 0},
  "duration_seconds": 87.3,
  "findings": [
    {
      "id": 101,
      "finding_id": "sqa-A03-001",
      "severity": "high",
      "category": "security",
      "title": "Inyección SQL en endpoint de búsqueda",
      "producer_agent": "security_qa_agent"
    }
  ]
}
```

### `POST /projects/{project_id}/conversations/notify-qa-completed`

Endpoint interno para ejecutores externos. Enruta un evento de finalización QA a través de Aria y hace broadcast por WebSocket. **Nota:** este endpoint no establece `pending_qa_report` — está pensado para sistemas que ya gestionan `pending_qa_report` por su cuenta.

---

## Evaluadores del supervisor

Cada agente QA tiene su propio evaluador supervisor. El supervisor lee de `qa_sessions` y `qa_findings` (no de `execution_runs`) y evalúa la calidad de la salida del agente.

| Clase evaluadora | `AGENT_NAME` | Evidencia usada |
|---|---|---|
| `FunctionalQAAgentEvaluator` | `functional_qa_agent` | Sesiones donde el agente ejecutó + hallazgos con `producer_agent == "functional_qa_agent"` |
| `BoundaryQAAgentEvaluator` | `boundary_qa_agent` | Mismo patrón |
| `AdversarialQAAgentEvaluator` | `adversarial_qa_agent` | Mismo patrón |
| `SecurityQAAgentEvaluator` | `security_qa_agent` | Mismo patrón |
| `PerformanceQAAgentEvaluator` | `performance_qa_agent` | Mismo patrón |
| `RegressionQAAgentEvaluator` | `regression_qa_agent` | Mismo patrón |
| `QASessionEvaluator` | `qa_session` | Todas las sesiones completadas + TODOS los hallazgos + TODOS los probes (sin filtro de productor) |

### Alcance de la evidencia (evaluadores por agente)

`build_qa_agent_evaluation_context(db, project_id, agent_name=...)`:

1. Obtiene sesiones completadas recientes donde `agent_name in session.agents_called`.
2. Consulta filas `QAFinding` filtradas por `producer_agent == agent_name` — **solo los hallazgos de ese agente**.
3. Extrae registros de probe de `session.probes` donde `probe["agent_name"] == agent_name`.
4. Retorna `{"sessions": [{"agent_findings": [...], "agent_probes": [...], ...}]}`.

La guardia `not_supervised` (`return EvaluatorOutput(result=None)`) es la **primera** instrucción cuando `ctx["sessions"]` está vacío — no se realiza ninguna llamada LLM para agentes que nunca ejecutaron en este proyecto.

### Prompts

- YAML del evaluador: `app/prompts/supervisor/{agent_name}_evaluator.yaml`
- Esquema: `AgentEvaluationOutput` (verdict: `healthy / needs_attention / degraded`, findings, issues, suggestions)

---

## Cómo añadir un nuevo agente QA

1. **Crear el archivo del agente:** `app/services/qa/qa_agents/{nombre}_qa_agent.py`
   - Heredar de `BaseQAAgent` en `app/services/qa/agents/base.py`.
   - Implementar la propiedad `name` y el método `probe()`.
   - Llamar `session.record_agent_call(self.name)` primero.
   - Llamar `session.add_finding(...)` para cada hallazgo con `producer_agent=self.name`.
   - Llamar `session.add_probe(ProbeRecord(...))` antes de retornar.
   - Establecer `producer_agent=self.name` en todos los objetos `QAFindingDetail`.

2. **Crear el prompt YAML:** `app/prompts/qa/{nombre}_qa_agent.yaml`

3. **Registrar en el registro de agentes:** Añadir a `build_default_registry()` en `app/services/qa/agents/registry.py`.

4. **Añadir a las estrategias relevantes:** Añadir `"{nombre}_qa_agent"` a `allowed_agents` en los archivos de estrategia apropiados bajo `app/services/qa/strategies/`.

5. **Crear el evaluador supervisor:** `app/services/supervisor/evaluators/{nombre}_qa_agent_evaluator.py`
   - Usar `build_qa_agent_evaluation_context(db, project_id, agent_name=...)`.
   - Retornar `EvaluatorOutput(result=None)` cuando `ctx["sessions"]` esté vacío.
   - Establecer `AGENT_NAME = "{nombre}_qa_agent"`.

6. **Crear el prompt del evaluador:** `app/prompts/supervisor/{nombre}_qa_agent_evaluator.yaml`

7. **Registrar el evaluador** en `supervisor_runner._EVALUATORS` y `aggregate_builder._QA_AGENT_EVALUATOR_NAMES`.

8. **Escribir tests:** Añadir clase de test en `tests/services/qa/test_new_qa_agents.py` y `tests/services/supervisor/test_qa_evaluators.py`.

---

## Límites conocidos y notas operacionales

### Ejecuciones QA concurrentes

Existen dos instancias separadas de `ThreadPoolExecutor`: una en `app/api/qa.py` (máx. 2 workers) y otra en `qa_tool.py` (máx. 2 workers). Si ambas se usan simultáneamente, pueden enviarse hasta 4 ejecuciones QA concurrentes. Cada ejecución tiene su propia sesión DB `SessionLocal` — no hay bloqueo de coordinación. Ejecutar sesiones QA concurrentes para el mismo proyecto es seguro a nivel de BD pero producirá registros `QASession` independientes con posibles comparaciones solapadas en `regression_qa_agent`.

### `regression_qa_agent` en la primera ejecución

El agente compara contra la sesión completada previa. En la primera ejecución QA de un proyecto no existe línea base — el sondeo se omite (`outcome="skipped"`) y no se añaden hallazgos. Esto es esperado y se registra en el log.

### Presupuesto de archivos fuente

Cada llamada a `source_reader.read_for_analysis()` lee como máximo 24.000 caracteres. En proyectos grandes solo una fracción del código fuente será visible para cada agente. Los agentes están diseñados para producir hallazgos útiles incluso con visibilidad parcial, pero los monorepos muy grandes pueden ver menor calidad en los hallazgos.

### Disponibilidad Docker

Todos los agentes Fase 5 (y el sondeo de temporización de `performance_qa_agent`) degradan de forma controlada cuando no hay sesión Docker activa disponible. Los agentes estructurados Fase 7-9 no requieren Docker en absoluto — realizan análisis puro de fuentes.

### Almacenamiento de `findings_summary`

`QASession.findings_summary` se almacena como dict JSON (no como cadena JSON) en la columna `JSON`. El endpoint `GET /qa/.../sessions/{id}` incluye una guardia de compatibilidad retroactiva (`isinstance(findings_summary, str)`) para filas escritas antes de esta corrección, pero las filas nuevas siempre se almacenan como dicts.

### Alineación veredicto vs. estado de sesión

| Veredicto | Estado de sesión |
|---|---|
| `passed` | `completed` |
| `partial` | `completed` |
| `failed` | `completed` |
| `blocked` | `blocked` |

Un veredicto `blocked` significa que la ejecución no pudo completarse (fallo de compilación APK, error fatal del motor, etc.). Un veredicto `failed` significa que la ejecución completó pero encontró problemas críticos o de alta severidad.

### Notificación a Aria si no hay conversación activa

Si `_notify_aria` no encuentra una conversación activa para el proyecto (ej. fue eliminada entre el inicio y el fin del QA), termina sin notificar y registra un warning. Los hallazgos **sí** quedan persistidos en la BD; solo se omite la notificación por WebSocket.
