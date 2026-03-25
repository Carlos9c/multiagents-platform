# 🚀 Multiagent Platform – Backend

## 🧠 Visión

Este proyecto implementa una **plataforma backend multiagente** cuyo objetivo es:

> Transformar una idea de software en un sistema ejecutable de forma progresiva, trazable y cada vez más autónoma.

El sistema está diseñado para evolucionar hacia proyectos complejos mediante:

- planificación jerárquica
- refinamiento progresivo
- ejecución aislada por workspace
- validación estructurada
- recuperación controlada
- evaluación por checkpoints
- trazabilidad completa por proyecto y por run

---

# 🏗️ Estado actual del proyecto

## ✅ Núcleo implementado

### 📦 Projects

- Multi-tenant por `project_id`
- Aislamiento completo por proyecto
- Estructura de storage persistente por proyecto
- Base para ejecución concurrente y runs aislados

---

### 📋 Tasks

Modelo jerárquico persistido y ya estabilizado:

- `high_level`
- `technical`
- `atomic`

Cada task mantiene:

- relación parent-child
- estado persistente
- prioridad
- tipo de task
- tipo de ejecutor
- historial de ejecución y validación

Estados operativos relevantes:

- `pending`
- `running`
- `awaiting_validation`
- `completed`
- `partial`
- `failed`

Regla clave ya fijada:

> **solo las tasks atómicas llegan al executor**

Las tasks padre no se ejecutan directamente; se resuelven por consolidación jerárquica a partir del estado de sus hijas.

---

### 🔄 Consolidación jerárquica

Implementada y validada mediante `task_hierarchy_service`.

Reglas actuales:

- un parent pasa a `completed` si todas sus hijas están `completed`
- un parent pasa a `pending` si existe al menos una hija no terminal
- un parent pasa a `failed` si todas sus hijas son terminales y al menos una está `failed`
- un parent pasa a `partial` si todas sus hijas son terminales, ninguna está `failed` y alguna está `partial`

Además:

- recovery puede reabrir el parent creando nuevas tasks hijas
- la task original recuperada debe permanecer terminal
- no se recicla silenciosamente la misma task como si no hubiera pasado nada

---

## 🧠 Planning pipeline

### Implementado

El pipeline de planificación ya existe de extremo a extremo:

1. `Planner`
2. `Technical Task Refiner`
3. `Atomic Task Generator`
4. `Execution Plan Generator`

### Resultado actual

Las atomic tasks ya salen con suficiente estructura para ejecución real:

- título
- descripción
- objetivo
- criterios de aceptación
- restricciones técnicas
- out of scope
- tipo de tarea
- executor esperado

Esto permite que la ejecución opere sobre contratos mucho más claros que al inicio del proyecto.

---

## ⚙️ Execution Plan

El proyecto ya soporta generación de `ExecutionPlan` con:

- batches
- dependencias inferidas
- checkpoints por batch
- secuenciación de tareas
- rationale de orden
- detección de bloqueos
- plan versionado

Capacidades actuales:

- varios batches por iteración
- reevaluación tras checkpoints
- nueva versión de plan cuando procede
- resecuenciación del trabajo restante
- continuación o parada controlada del workflow

---

# ▶️ Execution Engine

## Estado actual

El mayor salto reciente del proyecto ha sido la incorporación de un **Execution Engine orquestado**, separado del executor local heredado.

Hoy conviven dos engines:

- `legacy_local_engine`
- `orchestrated_engine`

La factoría ya permite seleccionar el engine, y el workflow ya está alineado con el nuevo camino orquestado.

---

## 🧩 Arquitectura del Execution Engine

El engine nuevo está organizado como un módulo propio y escalable:

- `contracts.py`
- `capabilities.py`
- `orchestrator.py`
- `subagent_registry.py`
- `agent_runtime/`
- `subagents/`
- `tools/`
- `request_adapter.py`
- `factory.py`
- `engines/`

### Componentes principales

#### `ExecutionOrchestrator`

Es el corazón del runtime operativo.

Responsabilidades:

- recibir una atomic task ya cerrada
- respetar su contrato sin modificarla
- decidir el siguiente paso operativo
- delegar en subagentes especializados
- aplicar una política de progresión por fases
- impedir loops absurdos
- producir un `ExecutionResult` trazable

#### `StructuredLLMRuntime`

Runtime basado en LLM estructurado para:

- siguiente acción
- selección de contexto
- plan de operaciones de fichero
- materialización de artefactos

#### `SubagentRegistry`

Registro explícito de subagentes disponibles.

#### Subagentes actuales

- `context_selection_agent`
- `placement_resolver_agent`
- `code_change_agent`
- `command_runner_agent`
- `repo_inspector_agent` / piezas auxiliares equivalentes según módulo actual

---

## 🧭 Flujo operativo del engine

El comportamiento actual del engine ya no es un bucle completamente libre. Ahora sigue una lógica de fases.

### Fases actuales

- `discovery`
- `planning`
- `materialization`
- `completion`

### Política actual por fases

#### `discovery`
Se permite inspección inicial del contexto.

#### `planning`
Se resuelven operaciones de fichero necesarias para cumplir la task.

#### `materialization`
Se materializan cambios reales en el workspace.

#### `completion`
Se cierra el pass operativo y se delega la evaluación final al validator externo.

---

## ✅ Mejoras recientes ya incorporadas en el engine

### 1. Transición por fases
Se evitó que el orquestador se quedara indefinidamente en `inspect_context`.

### 2. Override determinista de acciones inválidas
Si el modelo pide una acción incoherente con la fase, el orquestador la normaliza.

Ejemplo real ya corregido:

- pedir `inspect_context` cuando ya existe `planned_file_operations`
- pedir más inspección cuando lo correcto es `apply_file_operations`

### 3. Detección de estancamiento
Se introdujo detección de repetición sin progreso real.

Esto evita:

- consumir presupuesto en bucles vacíos
- gastar tokens en la misma acción sin cambio operativo
- terminar en failure opaco por presupuesto sin señal útil

### 4. Capabilities por executor
Se introdujo una capa de capacidades para dejar de razonar solo por tipo de tarea y empezar a razonar por lo que el ejecutor puede hacer.

### 5. Workspace aislado por run
Cada ejecución opera sobre:

`projects/{project_id}/executions/{run_id}/workspace`

y solo tras validación satisfactoria se promociona a:

`projects/{project_id}/domain_data/code/source`

### 6. Promoción segura a source
La promoción al source ya se hace únicamente cuando la validación devuelve `completed`.

### 7. Compatibilidad con validator heredado
El engine adapta su `ExecutionResult` al contrato que aún consume validación.

---

## 📁 Workspace runtime y storage

Ya existe una separación operativa clara entre:

- `source`
- `workspace aislado por ejecución`
- artefactos/logs/outputs del run

### Capacidades actuales

- preparar workspace por run
- copiar baseline desde source
- leer y escribir ficheros dentro del workspace
- recoger cambios respecto a source
- generar diff
- promocionar workspace validado a source
- limpiar workspace cuando proceda

### Correcciones recientes

Se corrigieron problemas reales de integración en Windows:

- ejecución de comandos con `encoding="utf-8"` y `errors="replace"`
- generación de diff con `git diff --no-index` usando el mismo tratamiento
- eliminación del fallo por `cp1252` durante lectura de stdout/stderr

---

# 🔍 Task Execution Service

## Estado actual

`task_execution_service` ya está adaptado al engine nuevo.

Flujo actual:

1. validar que la task es ejecutable
2. crear `ExecutionRun`
3. preparar workspace aislado
4. construir `ExecutionRequest`
5. seleccionar engine
6. ejecutar
7. persistir run
8. validar resultado
9. promocionar workspace si corresponde
10. reconciliar jerarquía

### Rutas ya soportadas

- `completed`
- `partial`
- `failed`
- `rejected`

### Mejoras recientes

- preparación explícita del workspace antes de ejecutar
- promoción a source solo tras validación `completed`
- persistencia de artefacto terminal incluso en fallos tempranos
- adaptación a resultados sintéticos cuando el engine falla antes de una salida normal
- mayor trazabilidad en logs

---

# ✅ Validación

## Estado actual

La validación sigue fuera del engine, y de momento esa separación se mantiene de forma intencionada.

Esto permite:

- mantener un juez externo al runtime operativo
- inspeccionar workspace real
- comparar contra source
- usar snapshots finales
- decidir `completed`, `partial` o `failed`

### Evidencias que ya usa

- diff de workspace
- archivos creados / modificados
- snapshots finales
- notas del journal del executor
- contexto resuelto de ejecución
- working set

### Estado de integración

La integración executor → validator sigue funcionando, pero ya se ha confirmado algo importante:

> la validación hoy depende mucho de que la ejecución produzca artefactos reales y observables en workspace

Esto ha ayudado a detectar varios no-ops del engine que antes podían pasar más desapercibidos.

---

# 🔁 Recovery Service

## Capacidades actuales

Recovery soporta:

- `reatomize`
- `insert_followup`
- `manual_review`

### Invariantes ya fijadas

- la task original debe permanecer terminal
- recovery no debe hacer `retry` silencioso de la misma task
- si hace falta más trabajo, se crean nuevas tasks
- el parent se reabre por consolidación jerárquica, no por reactivar artificialmente la source task

### Cambio importante

El camino `retry` quedó rechazado en el workflow actual y ya se trató como contrato inválido para evitar inconsistencias.

---

# 🧪 Post-batch y evaluación

## Post-batch

Ya existe la capa encargada de:

- confirmar que el batch terminó realmente
- verificar terminalidad de tasks y runs
- disparar recovery si hace falta
- reconciliar jerarquía tras recovery
- ejecutar evaluación del checkpoint

## Evaluación de etapas

Soporta ya decisiones como:

- `continue`
- `stage_incomplete`
- `project_complete`
- `manual_review`

Y se corrigió la normalización para no bloquear el workflow en estados ambiguos cuando la etapa no estaba completa pero podía continuar.

---

# 🔄 Workflow end-to-end

## Pipeline actual

El workflow ya recorre:

Planning → Technical Refinement → Atomic Generation → Execution Plan → Batch Execution → Post-Batch → Evaluation → Continuation / Stop

### Lo que ya hace bien

- ejecutar batches
- iterar sobre nuevas versiones de `ExecutionPlan`
- parar en `manual_review` cuando corresponde
- crear y validar workspaces por task
- encadenar ejecución y validación
- reabrir el plan tras recovery cuando procede

### Lo que ha quedado demostrado

El backend ya no es una maqueta conceptual.  
Ya existe un sistema que realmente:

- planifica
- atomiza
- ejecuta
- valida
- recupera
- reevalúa
- replanifica

---

# 🧪 Testing

La suite de tests se ha ampliado y ya cubre partes críticas del sistema.

## Cobertura destacada

### `task_hierarchy_service`
- consolidación de estados
- reapertura del parent por nuevas hijas
- invariantes jerárquicas

### `recovery_service`
- follow-up tasks
- terminalidad de la source task
- rechazo del camino `retry`
- manual review

### `post_batch_service`
- integración recovery + evaluación
- detección de estados inválidos
- continuación correcta del workflow

### `project_workflow_service`
- batches
- iteraciones
- protección frente a tasks no atómicas
- flujo E2E controlado

### `task_execution_service`
- preparación de workspace
- integración con execution engine
- promoción de workspace validado
- reconciliación jerárquica
- rutas terminales y de validación

### `execution_engine`
- resolución de operaciones de fichero
- materialización multiarchivo
- rollback si falla una escritura
- trazabilidad del orquestador
- política por fases
- override de acciones incoherentes

---

# 📚 Documentación interna del engine

Ya existe documentación específica del módulo:

- `app/execution_engine/execution_engine_documentation.md`

Esa documentación recoge:

- arquitectura del módulo
- contratos
- subagentes
- tools
- flujo
- extensibilidad
- pasos para añadir nuevos subagentes o nuevas tools

---

# ⚠️ Problemas actuales reales

## 1. Completion phase todavía necesita endurecerse
Ya se corrigió gran parte del loop, pero se ha observado que el modelo puede intentar abusar de `run_command` en fase de completion.

La política actual ya se ha endurecido, pero es un área que todavía requiere vigilancia.

## 2. `file_materialization` es el cuello de botella principal
Los pasos de materialización están devolviendo respuestas grandes y lentas.

Síntomas observados:

- prompts grandes
- salidas largas
- tiempos de 30s–40s
- consumo fuerte de tokens

Conclusión:
- el sistema ya materializa
- pero la granularidad de materialización todavía es demasiado gruesa

## 3. Algunas tasks siguen generando superficies demasiado amplias
Especialmente en implementación, donde el plan de operaciones puede crecer mucho si el contexto resuelto o el working set no se limita bien.

## 4. La validación sigue cargando mucho contexto
En runs fallidos o complejos, recovery + stage evaluation + execution plan vuelven a cargar bastante contexto. Funciona, pero es costoso.

## 5. El modelo del engine exige más disciplina que el resto
En la práctica actual:

- `gpt-5.4-mini` funciona bien en planning, recovery y evaluación
- `gpt-5.2` se está comportando de forma más estable en el execution engine

Esto sugiere que el engine debe seguir teniendo configuración de modelo independiente.

---

# 🧭 Decisiones de diseño recientes

## 1. El validator sigue fuera del engine
No se ha mezclado todavía ejecución y validación.

## 2. El engine ya no debe razonar solo por `task_type`
Se ha empezado a mover hacia capacidades del ejecutor.

## 3. La transición operativa debe estar gobernada por política, no solo por LLM
Este ha sido uno de los aprendizajes más importantes.

## 4. El workspace temporal no debe contaminar source
Solo se promociona tras validación satisfactoria.

## 5. El sistema debe fallar de forma honesta antes que terminar en no-op silencioso
Esto ya ha guiado varias correcciones recientes.

---

# 🔧 Próximos pasos priorizados

## Corto plazo

### 1. Endurecer la fase de completion
- permitir `run_command` solo cuando realmente tenga sentido
- limitarlo todavía más o hacerlo opcional por política
- evitar cualquier nuevo loop de verificación operativa

### 2. Reducir el tamaño de `file_materialization`
- dividir mejor planes multiarchivo
- acotar el contexto enviado al materializer
- reducir el contenido innecesario en prompts

### 3. Mejorar el `placement_resolver`
- generar planes más pequeños
- reducir superficie de cambios
- evitar sobreplanificación en tasks simples

### 4. Revisar cuándo merece la pena ejecutar comandos
- no todo cambio necesita `run_command`
- especialmente cuando existe validator externo

### 5. Añadir más tests sobre completion phase
- `run_command` repetido
- override a `finish`
- materialización correcta sin verificación redundante

---

## Medio plazo

### 6. Refinar la selección de contexto
- menos ruido
- mejor working set
- mejor selección de archivos y rutas candidatas

### 7. Hacer el engine menos caro
- prompts más compactos
- mejor reutilización de estado
- menos rondas LLM por task

### 8. Revisar granularidad de atomic tasks
- evitar tasks atómicas que sigan siendo demasiado anchas para materialización en un solo paso

### 9. Fortalecer el contrato entre engine y validator
- menos adaptación heredada
- contrato más nativo del engine nuevo

---

## Largo plazo

### 10. Extender subagentes más allá de código
La arquitectura ya debe pensarse para otros outputs:

- documentación
- imágenes
- audio
- presentaciones
- otros artefactos

### 11. Diseñar herramientas verdaderamente multimodales
Para no seguir pensando el engine únicamente como escritura de `.py`.

### 12. Sustituir completamente el executor heredado
Cuando el engine orquestado esté suficientemente estable.

---

# 📌 Conclusión

El proyecto ha dado un salto importante.

A día de hoy ya existe una plataforma backend que:

- planifica de forma jerárquica
- atomiza trabajo
- genera batches y checkpoints
- ejecuta tasks atómicas en workspaces aislados
- valida resultados reales
- recupera fallos creando nuevo trabajo
- reevalúa el plan y continúa iterando

El mayor avance reciente ha sido dejar de tratar la ejecución como un bloque opaco y convertirla en un **runtime orquestado por fases**, con subagentes, tools, trazabilidad y políticas explícitas.

El principal reto ya no es “cómo planificar más”.

El principal reto ahora es este:

> **cómo materializar cambios útiles, pequeños y verificables de forma consistente, sin abrir loops de contexto ni inflar innecesariamente los pasos de escritura y verificación.**

Ese es el punto exacto en el que está hoy el proyecto.