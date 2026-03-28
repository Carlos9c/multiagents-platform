# 🧠 Agente Desarrollador — Estado del Proyecto

## 📌 Visión General

Este proyecto implementa un sistema de ejecución autónoma basado en planificación, ejecución por batches y evaluación iterativa.

El sistema sigue un flujo:

1. **Planificación** → generación de tareas (high-level / refined / atomic)
2. **Ejecución** → ejecución por batches atómicos
3. **Evaluación** → análisis post-batch
4. **Mutación del plan** → patch / resequence / replan
5. **Iteración** → hasta cierre o bloqueo

---

# ✅ Estado Actual

## 🔴 FASE 1 — CONTROL Y ESTABILIDAD (CERRADA)

Se ha estabilizado completamente el núcleo del sistema.

### ✔ Reglas de post-batch
- Eliminada la replanificación por defecto
- Separación clara entre:
  - decisión (`PostBatchResult`)
  - mutación (`live_plan_mutation_service`)
- Reglas explícitas:
  - recovery ≠ replan
  - preferencia por continue / resequence

---

### ✔ Desacoplamiento workflow / plan
- `active_plan` reutilizado correctamente
- invalidación solo cuando:
  - `iteration_requires_replan = True`
- no regeneraciones accidentales

---

### ✔ Ruta canónica de mutación
- `post_batch_service` decide
- `live_plan_mutation_service` ejecuta
- `execution_plan_patch_service` como primitiva

---

### ✔ Semántica clara de ejecución
- planner → crea
- mutation → modifica
- workflow → orquesta

---

### ✔ Tests de regresión sólidos
Cobertura real en:
- decisiones
- mutaciones
- workflow
- invariantes

---

## 🟡 FASE 2 — TRAZABILIDAD Y CONSISTENCIA (PRÁCTICAMENTE CERRADA)

### ✔ Trazabilidad enriquecida
Se han consolidado tres niveles:

#### 1. `workflow_batch_trace`
Incluye:
- resultado de ejecución de tasks
- decisión post-batch
- señales usadas (`decision_signals_used`)
- acción resuelta (`resolved_action`)
- `patched_plan_version`

#### 2. `WorkflowIterationSummary`
Incluye:
- transición de plan
- batches procesados
- batches bloqueados
- `used_patched_plan`
- `replan_triggered`

#### 3. `ProjectWorkflowResult`
Vista final coherente del estado del workflow

---

### ✔ Coherencia de estados
- `PostBatchResult` validado
- consistencia entre:
  - evaluación
  - workflow
  - artifacts

---

### ✔ Tests de invariantes (clave)
Se han añadido tests estructurales:

- `completed_batches` nunca se duplica
- replan invalida `active_plan`
- no se reejecutan batches
- `blocked_batches` excluye completados

---

### ✔ Limpieza técnica
- eliminación de metadata redundante
- eliminación de bloques muertos
- simplificación de payloads
- eliminación de duplicidad en trazas

---

# 📊 Estado General

El sistema ha pasado de:

❌ comportamiento inestable  
➡️ a  
✅ ejecución determinista, trazable y validada

---

# 🚧 SIGUIENTES PASOS (BACKLOG ACTUALIZADO)

## 🔴 BLOQUE ALTO

### 1. Limpieza final de semántica legacy
**Objetivo:** unificar lenguaje interno

Pendiente:
- eliminar `legacy_action`
- limpiar `ResolvedPostBatchIntent`
- eliminar flags heredados
- unificar semántica entre:
  - evaluación
  - intent
  - mutación
  - workflow

👉 Impacto: alto (reduce complejidad mental y bugs futuros)

---

### 2. Endurecer `run_command`
**Objetivo:** seguridad y robustez

Pendiente:
- sandbox / restricciones
- timeouts
- control de errores
- logging estructurado

👉 Impacto: crítico si se usa en producción

---

### 3. Revisar `request_adapter`
**Objetivo:** alinear input real con sistema

Pendiente:
- validar shape real de requests
- evitar pérdida de contexto útil
- evitar ruido innecesario

---

## 🟡 BLOQUE MEDIO

### 4. Test end-to-end real
**Objetivo:** validar comportamiento completo

Pendiente:
- flujo completo sin mocks
- ejecución representativa real

---

### 5. Reducir duplicidad de artifacts
**Objetivo:** simplificar trazabilidad

Pendiente:
- definir responsabilidad de:
  - batch_trace
  - iteration_trace
  - post_batch_result
- eliminar redundancia

---

### 6. Refactor de servicios grandes
**Objetivo:** mantenibilidad

Foco:
- `post_batch_service`
- `project_workflow_service`

---

## 🔵 BLOQUE BAJO

- portabilidad config/storage
- promoción de workspace
- tuning de prompts
- optimización del execution engine

---

# 🧠 Conclusión

El sistema ya no está en fase de construcción básica.

Está en fase de:

> **consolidación, simplificación y endurecimiento**

Lo crítico ya está resuelto:

- decisiones correctas
- ejecución estable
- trazabilidad consistente
- tests robustos

Ahora el foco es:

> **reducir complejidad, eliminar legacy y preparar para escenarios reales**

---