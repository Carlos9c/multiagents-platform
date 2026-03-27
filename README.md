# 🧠 Agente Desarrollador — Execution Engine

Sistema de ejecución autónoma basado en planificación iterativa, evaluación por checkpoints y mutación controlada del plan.

---

## 🚀 Estado actual del proyecto

El sistema ha alcanzado un estado **estable en el core de ejecución**, con:

* Decisión post-batch completamente formalizada (`ResolvedPostBatchIntent`)
* Separación clara entre:

  * decisión (post-batch)
  * mutación (live_plan_mutation_service)
  * orquestación (project_workflow_service)
* Eliminación de rutas legacy de mutación de plan
* Control explícito de:

  * replan estructural vs patch local
  * assignment vs resequence vs continue
* `active_plan` persistente entre iteraciones
* Tests de regresión cubriendo comportamiento crítico

---

## 🧩 Arquitectura (visión global)

Planner → Atomic Generator → Execution Plan
↓
Execution Engine (Workflow)
↓
Batch Execution (tasks)
↓
Post-Batch Evaluation
↓
ResolvedPostBatchIntent
↓
LivePlanMutationService (opcional)
↓
Workflow decide siguiente paso

---

## 🧠 Componentes clave

### 1. post_batch_decision_service

Responsabilidad:

* Interpretar señales del evaluator
* Resolver una intención canónica

Output:
ResolvedPostBatchIntent

Tipos de intent:

* continue → seguir sin cambios
* assign → introducir nuevas tareas en el plan
* resequence → ajustar orden local
* replan → reconstrucción estructural
* close → cerrar etapa
* manual_review → intervención humana

⚠️ Este servicio **NO muta el plan**.

---

### 2. live_plan_mutation_service

Responsabilidad:

* Aplicar mutaciones sobre el plan activo **cuando procede**

Entrada:

* intent (ResolvedPostBatchIntent)
* contexto de ejecución
* recovery

Salida:
LivePlanMutationResult

Tipos de resultado:

* assignment → plan parcheado con nuevas tareas
* resequence_patch → inserción inmediata de batch
* resequence_deferred → no se muta plan (solo señal lógica)
* escalated_to_replan → no se pudo mutar → requiere replan
* none → no aplica mutación

⚠️ Este servicio:

* NO decide si hay que replanear
* NO genera planes desde cero
* SOLO modifica el plan existente o escala

---

### 3. post_batch_service

Responsabilidad:

* Orquestar evaluación + decisión + mutación
* Persistir artefactos

Flujo:
evaluate_checkpoint → resolve_intent → mutate_live_plan → construir resultado

Garantías:

* Nunca deja tareas nuevas sin asignar si el intent lo requiere
* No crea patches inválidos (ej: resequence diferido)
* Escala correctamente a replan cuando no puede mutar

---

### 4. project_workflow_service (ORQUESTADOR)

Responsabilidad:

* Ejecutar el workflow iterativo completo

Claves:

#### active_plan

* Se mantiene entre iteraciones
* Se actualiza si hay patch
* Se invalida si hay replan estructural

#### iteration_requires_replan

* Señal explícita desde post-batch
* Provoca regeneración del plan en la siguiente iteración

#### Flujo de iteración

for iteration:
if no active_plan:
generar plan

```
ejecutar batches  

post_batch_result  

if requires_replan:  
    active_plan = None  

elif patched_plan:  
    active_plan = patched_plan  

continuar / parar según estado  
```

---

## ⚖️ Semántica del sistema (CRÍTICO)

### 🔴 Replan (estructural)

Se produce cuando:

* remaining_plan_still_valid = False
* inconsistencia global
* assignment no colocable

Acción:

* se descarta active_plan
* el workflow genera uno nuevo

---

### 🟡 Resequence (local)

Dos tipos:

Patch inmediato:

* Se inserta batch nuevo
* mutation_kind = resequence_patch

Deferred:

* No se modifica el plan
* Solo cambia interpretación futura
* mutation_kind = resequence_deferred

---

### 🟢 Assignment

* Introduce nuevas tareas en el plan
* Siempre antes de continuar
* Nunca deja tareas en limbo

---

### 🔵 Continue

* Plan intacto
* Sin trabajo nuevo

---

### ⚫ Close

Solo ocurre si:

* último batch
* sin tareas pendientes
* sin recovery nuevo

---

## 🧪 Cobertura de tests (regresión)

Cubierto:

Decision layer:

* selección correcta de intent
* no replan por defecto
* cierre legal de etapa

Mutation layer:

* assignment correcto
* resequence deferred sin patch
* escalado a replan
* rechazo de intents no mutantes

Post-batch:

* integración completa decisión + mutación
* persistencia consistente
* no creación de patches inválidos

Workflow:

* reutilización de active_plan
* invalidación en replan
* no reejecución de batches
* adopción de planes parcheados

---

## 🧱 Decisiones arquitectónicas clave

Separación estricta:

* Decision → qué hacer
* Mutation → cómo modificar el plan
* Workflow → cuándo ejecutar

Una única vía de mutación:

Todo pasa por live_plan_mutation_service

El workflow es el único orquestador:

* decide cuándo regenerar plan
* decide cuándo continuar
* decide cuándo parar

---

## ⚠️ Riesgos conocidos (controlados)

* complejidad creciente del post_batch_service
* tamaño de project_workflow_service
* dependencia fuerte de consistencia en signals

---

## 🔜 Siguientes pasos recomendados

Crítico:

* reforzar tests de regresión
* endurecer validaciones de mutation

Alto:

* trazabilidad completa plan → batch → run → recovery → evaluación
* limpieza final de legacy

Medio:

* unificación de artefactos de trazas
* portabilidad de storage/config
* integración end-to-end

Bajo:

* tuning de prompts
* refactor de servicios grandes
* optimización del execution engine

---

## 🧭 Filosofía del sistema

1. No replanear por defecto
2. No dejar trabajo sin asignar
3. Separar decisión de ejecución

---

## 🏁 Conclusión

El sistema actual ya no es un prototipo:

* Tiene semántica estable
* Tiene control de estado explícito
* Tiene arquitectura extensible

A partir de aquí el foco es:
robustez, observabilidad y mantenibilidad
