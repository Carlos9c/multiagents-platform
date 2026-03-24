# 🚀 Multiagent Platform – Backend

## 🧠 Visión

Este proyecto implementa una **plataforma backend multiagente** cuyo objetivo es:

> Transformar una idea de software en un sistema ejecutable de forma progresiva, trazable y cada vez más autónoma.

El sistema está diseñado para escalar hacia proyectos complejos mediante:

- planificación jerárquica
- descomposición progresiva
- ejecución distribuida
- evaluación estructurada
- recuperación inteligente
- trazabilidad completa por proyecto

---

# 🏗️ Estado actual del proyecto

## ✅ Núcleo implementado

### 📦 Projects

- Multi-tenant por `project_id`
- Aislamiento completo por proyecto
- Base para ejecución concurrente

---

### 📋 Tasks (modelo jerárquico estable)

- Jerarquía persistida:
  - `high_level`
  - `technical`
  - `atomic`

- Relaciones parent-child persistidas
- Estados bien definidos:
  - `pending`
  - `awaiting_validation`
  - `completed`
  - `partial`
  - `failed`

---

### 🔄 Reglas de consolidación jerárquica

Implementado y validado mediante `task_hierarchy_service`.

Un parent:
- pasa a `completed` si todas sus hijas están `completed`
- pasa a `pending` si existe al menos una hija no terminal
- pasa a `failed` si todas son terminales y al menos una es `failed`
- pasa a `partial` si todas son terminales, ninguna es `failed` y alguna es `partial`

Importante:
- Recovery puede reabrir el parent a `pending` creando nuevas tasks
- Nunca se ejecutan tasks no atómicas
- Las tasks padre no llegan al executor; se resuelven determinísticamente por el estado de sus hijas

---

### 🧠 Planner + Refiners

Pipeline implementado:

1. High-level planning
2. Technical refinement
3. Atomic task generation

Las atomic tasks ya incluyen:
- objetivo claro
- criterios de aceptación
- constraints
- tipo de ejecutor esperado a nivel orientativo

---

### ⚙️ Execution Plan

- Generación de batches (`ExecutionPlan`)
- Checkpoints obligatorios tras cada batch
- Dependencias implícitas
- Secuenciación controlada

---

### ▶️ Ejecución (`task_execution_service`)

- Solo ejecuta tasks `atomic`
- Flujo completo:
  1. Executor
  2. Validación
  3. Persistencia
  4. Reconciliación jerárquica

- Rutas soportadas:
  - `completed`
  - `partial`
  - `failed`
  - `rejected`

Además:
- en `completed` se promociona el workspace validado a source
- tras cualquier cierre terminal se reconcilia la jerarquía
- la ejecución no permite colar tasks no atómicas al executor

---

### 🔁 Recovery Service

Soporta actualmente:

- `reatomize`
- `insert_followup`
- `manual_review`

Invariantes clave fijadas:
- la task original debe permanecer terminal
- recovery no debe reciclar silenciosamente la task original como `pending`
- si hace falta más trabajo, se crean nuevas tasks
- el parent se reabre por consolidación jerárquica, no por reabrir la task original

Nota:
- el comportamiento `retry` quedó identificado como fuente de incoherencias y debe tratarse con mucho cuidado si se mantiene

---

### 🔍 Post-Batch Service

Responsable de:

- validar que el batch terminó realmente
- verificar que tasks y runs están en estado terminal
- ejecutar recovery si aplica
- reconciliar jerarquía tras recovery
- ejecutar evaluación del checkpoint

Integración completa con:
- recovery
- evaluation
- hierarchy reconciliation

Además:
- ya normaliza correctamente decisiones como `stage_incomplete`
- protege al workflow de continuar por rutas inconsistentes
- debe endurecerse todavía frente a cualquier reactivación incorrecta de la source task tras recovery

---

### 🧪 Evaluación de checkpoints

Nuevo contrato soportado a nivel práctico:

- `stage_incomplete`
- `continue`
- `project_complete`
- `manual_review`
- reapertura de finalización
- replanificación o resecuenciación cuando aplique

Regla crítica ya corregida:

> `stage_incomplete` sin manual review, sin replan y sin follow-up extra debe permitir continuar al siguiente batch

Esto corrige un bug previo donde el workflow se detenía por ambigüedad de normalización.

---

### 🔄 Workflow Orchestration end-to-end

Pipeline actual:

Planning → Refinement → Atomic Generation → Execution Plan → Batch Execution → Post-Batch → Evaluation → Continuation / Stop

Soporta:
- múltiples batches
- iteraciones
- finalización controlada
- guard de finalización
- cierre de etapa
- manual review cuando corresponde

---

# 🧪 Sistema de tests

Se ha introducido una suite de tests robusta con `pytest`.

## Cobertura actual

### `task_hierarchy_service`
- consolidación correcta de estados
- reapertura del parent por recovery

### `recovery_service`
- creación de nuevas tasks
- invariantes de terminalidad de la task original
- manual review
- validación de rutas de recovery

### `post_batch_service`
- integración recovery + evaluación
- continuación correcta con `stage_incomplete`
- detección de estados inválidos tras recovery

### `project_workflow_service`
- ejecución multi-batch
- validación de atomicidad
- protección frente a tasks no atómicas en batches
- flujo E2E controlado

### `task_execution_service`
- ejecución completa de atomic tasks
- rutas `completed`, `partial`, `failed`, `rejected`
- promoción de workspace
- reconciliación jerárquica

Resultado:
- el sistema ya es bastante más testeable
- las invariantes principales del workflow están cubiertas
- la base es mucho más segura para iterar

---

# ⚠️ Problemas actuales detectados

## 1. El executor es el cuello de botella real

Los fallos recientes no vienen ya del workflow ni de recovery, sino de la capa de ejecución.

Ejemplo real detectado:
- rechazo por contexto insuficiente
- repo demasiado sparse
- falta de superficie de código clara

Conclusión:
- el sistema puede planificar y orquestar
- pero la ejecución aún no es suficientemente inteligente respecto al estado real del repositorio

---

## 2. Algunas atomic tasks son válidas conceptualmente pero no operativamente

Actualmente puede ocurrir que una atomic task describa algo correcto a nivel funcional, pero no ejecutable en el estado real del repo.

Ejemplo:
- “implementar API mínima”
- pero no existe todavía estructura, entrypoint ni módulos base

Esto no es un simple bug: es una limitación del modelo actual de ejecución.

---

## 3. Falta awareness real del workspace/source

El sistema todavía no decide bien:
- qué estructura existe
- dónde debe ir cada fichero
- cuándo una tarea requiere bootstrap previo
- qué tipo de agente o estrategia conviene según el estado del repo

---

# 🧭 Nueva dirección propuesta: Execution Orchestrator

## Idea principal

Introducir una nueva capa entre atomic task y executor especializado:

**Execution Orchestrator**

### Responsabilidades

- interpretar la atomic task
- inspeccionar el estado real del repo/workspace
- decidir el modo de ejecución
- seleccionar el subagente más adecuado
- decidir dónde deberían vivir los cambios
- preparar el contexto operativo real antes de ejecutar

---

## Modelo propuesto

Atomic Task  
↓  
Execution Orchestrator  
↓  
Specialized Subagent  
↓  
Executor Result

---

## Tipos de ejecución que el orquestador debería distinguir

- `bootstrap`
- `edit_existing`
- `extend_existing`
- `repair_failed_work`
- `tests_only`
- `docs_only`

Esto permitiría resolver correctamente casos donde hoy el executor rechaza por falta de superficie.

---

## Posibles subagentes futuros

- `scaffolding_agent`
- `backend_agent`
- `test_agent`
- `docs_agent`
- `refactor_agent`

---

## Restricciones del orquestador

El orquestador **no debe**:
- cambiar el objetivo funcional de la atomic task
- replanificar la etapa
- inventar alcance no pedido

El orquestador **sí puede**:
- elegir subagente
- seleccionar working set
- decidir rutas objetivo
- clasificar el modo de ejecución
- detectar que antes hace falta bootstrap o scaffolding

---

# 🔧 Próximos pasos priorizados

## Corto plazo

1. Endurecer recovery definitivamente
   - evitar cualquier reapertura incoherente de la task original
   - mantener la source task siempre terminal
   - detectar de forma explícita estados corruptos tras materialización

2. Añadir tests adicionales sobre recovery y executor
   - rechazo por repo sparse
   - generación de follow-up/bootstrap tasks
   - protección ante retries incoherentes

3. Mejorar el atomic task generator
   - introducir awareness de precondiciones ejecutables
   - evitar atomics que dependan de estructura inexistente sin declararlo

---

## Medio plazo

4. Diseñar el contrato del `Execution Orchestrator`
   - inputs
   - outputs
   - límites de decisión
   - interacción con subagentes

5. Implementar una primera versión mínima
   - clasificación de tipo de ejecución
   - routing básico a un subagente adecuado
   - detección de repositorio sparse

6. Crear al menos dos subagentes iniciales
   - `scaffolding_agent`
   - `implementation_agent`

---

## Largo plazo

7. Framework serio de subagentes con tools
8. Selección dinámica de ejecutor en tiempo de ejecución
9. Mayor autonomía operativa sin perder trazabilidad
10. Ejecución multiagente especializada y robusta

---

# 📌 Conclusión

El proyecto ya no es solo una maqueta conceptual.

A día de hoy ya existe:
- pipeline end-to-end
- jerarquía coherente
- recovery razonablemente saneado
- evaluación integrada
- tests útiles sobre invariantes críticas

El siguiente gran salto no pasa por añadir más planificación.

Pasa por resolver esta pregunta:

> ¿cómo aterrizamos una atomic task sobre el estado real del repo sin depender de que el executor “adivine” la estructura?

La respuesta más prometedora ahora mismo es:

> introducir una capa de **Execution Orchestrator** que decida cómo, dónde y con qué subagente debe ejecutarse cada atomic task.