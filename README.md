# 🚀 Multiagent Platform – Backend

## 🧠 Visión

Este proyecto implementa una **plataforma backend multiagente** cuyo objetivo es:

> Transformar una idea de proyecto en un sistema ejecutable de forma progresiva, estructurada y autónoma.

El sistema se basa en:
- planificación jerárquica
- ejecución por batches
- evaluación iterativa
- recovery automático
- y control de flujo inteligente

---

# 🏗️ Estado actual del proyecto

El sistema ha pasado de un estado experimental a una base **estructuralmente sólida**.

## ✅ Núcleo estable

### 🔹 Evaluación estructurada
- `StageEvaluationOutput` con contrato fuerte
- separación clara entre:
  - `continue_current_plan`
  - `resequence_remaining_batches`
  - `replan_remaining_work`
  - `manual_review`
  - `close_stage`
- validaciones cruzadas entre:
  - `decision`
  - `recommended_next_action`
  - `plan_change_scope`
  - `remaining_plan_still_valid`

### 🔹 Contexto de evaluación limpio (CRÍTICO)
Se ha eliminado una de las principales fuentes de errores:

Antes:
- mezcla de artifacts históricos
- ruido de ciclos anteriores

Ahora:
- ventana real de checkpoint basada en `artifact_id`
- el evaluador solo ve evidencia del ciclo actual

### 🔹 Separación de contexto
El evaluador recibe bloques diferenciados:
- `processed_batch_summary`
- `checkpoint_artifact_window_summary`
- `recovery_tasks_created_summary`
- `remaining_batches_summary`
- `pending_task_summary`
- `additional_context`

👉 Resultado:
- menos ambigüedad
- menos replanificaciones erróneas

### 🔹 Recovery rediseñado correctamente

#### Eliminación completa de `retry`
- fuera del schema
- fuera del flujo
- schemas estrictos (`extra="forbid"`)

#### Input mejorado
- se introduce `last_execution_agent_sequence`
- recovery entiende la trayectoria real de ejecución

#### Integridad estructural garantizada
- ya no se crean tasks bajo una atomic sin `parent_task_id`
- si ocurre → error explícito

👉 Resultado:
- recovery más fiable
- sin corrupción del árbol de tareas

### 🔹 Limpieza de schemas
- `recovery.py` es la única fuente de verdad
- eliminada duplicidad en `evaluation.py`

### 🔹 Tests reforzados
Cobertura sólida en:
- evaluation_service
- post_batch_service
- recovery_service
- schemas

Incluyendo:
- validaciones de contrato
- artifact window
- decisiones de flujo
- integridad de recovery

---

# ⚠️ Problemas resueltos

## ❌ Replanificación excesiva
Antes:
- el sistema replanificaba con facilidad

Ahora:
- mejor señal
- mejor contexto
- mejor contrato

👉 Aún queda un ajuste fino (ver backlog)

## ❌ Ruido en artifacts
Antes:
- el evaluador veía datos irrelevantes

Ahora:
- ventana limpia por checkpoint

## ❌ Ambigüedad en recovery
Antes:
- lógica difusa
- retry ambiguo

Ahora:
- acciones claras:
  - `reatomize`
  - `insert_followup`
  - `manual_review`

## ❌ Corrupción de jerarquía de tareas
Antes:
- una atomic podía convertirse en padre

Ahora:
- prohibido explícitamente

---

# 🧱 BACKLOG ACTUAL (REAL)

## 🔥 BLOQUE 1 — Estabilizar decisiones post-recovery (CRÍTICO)

### Problema actual
El sistema aún puede sobre-reaccionar cuando aparecen nuevas tasks de recovery.

### Objetivo
Evitar que recovery implique automáticamente cambio estructural.

### Tareas

#### 1.1 Reglas explícitas en `post_batch_service`
- Si:
  - `remaining_plan_still_valid = True`
  - y hay backlog válido  
→ ❌ NO replan

- Si:
  - recovery crea tasks
  - y `still_blocks_progress = False`  
→ ❌ NO replan  
→ ✔ continue / resequence

- Solo replan si:
  - inconsistencia estructural real
  - o `plan_change_scope` fuerte

#### 1.2 Priorizar continuidad
- default → `continue_current_plan`
- resequence solo si:
  - dependencia real
  - orden incorrecto

#### 1.3 Formalizar decisión final
Eliminar lógica implícita basada en flags dispersos.

#### 1.4 Tests críticos
- recovery + no blocking → NO replan
- backlog válido → NO replan
- follow-up simple → NO replan

---

## 🧱 BLOQUE 2 — Identidad de planes y batches (BUG 2)

### Problema
- el sistema puede “volver a batch 1”
- falta trazabilidad

### Tareas

#### 2.1 `plan_version` determinista
`max(plan_version) + 1`

#### 2.2 ID estable de batch
`{plan_version}_{batch_index}`

#### 2.3 Nombre normalizado
`Plan {version} · Batch {index}`

---

## 🧱 BLOQUE 3 — Observabilidad

### Tareas

#### 3.1 Artifacts completos
- execution_plan
- evaluation_decision
- post_batch_result
- recovery_decisions

#### 3.2 Trazabilidad del flujo
Guardar por iteración:
- batch
- tasks
- recovery
- decisión

---

## 🧱 BLOQUE 4 — Recovery (refinamiento)

### Tareas
- validar impacto real de `last_execution_agent_sequence`
- ajustar prompt si es necesario
- (opcional) tests de trayectoria

---

## 🧱 BLOQUE 5 — Evaluador (fase final)

### Tareas
- reforzar prompt:
  - NO replan por defecto
  - recovery ≠ fallo estructural
- añadir ejemplos negativos

---

## 🧱 BLOQUE 6 — Limpieza final

### Tareas
- renombrado semántico
- eliminación de legacy restante

---

# 🧠 Prioridad real

## 🔥 Inmediato
1. reglas post-recovery (bloque 1)
2. identidad de plan/batch (bloque 2)

## ⚠️ Medio
3. observabilidad

## ⚙️ Bajo
4. ajustes de recovery y evaluador

---

# 🧭 Siguientes pasos recomendados

1. Implementar reglas explícitas en `post_batch_service`
2. Añadir tests de “no replan innecesario”
3. Introducir `plan_version` determinista
4. Estabilizar identidad de batch
5. Mejorar trazabilidad de ejecución

---

# 💬 Conclusión

El sistema ha evolucionado de:

> comportamiento errático y difícil de razonar

a:

> arquitectura sólida con semántica clara y decisiones estructuradas

Ahora el foco ya no es “arreglar caos”, sino:

> afinar comportamiento y asegurar estabilidad operativa