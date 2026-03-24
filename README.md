# 🚀 Multiagent Platform – Backend

## 🧠 Visión

Este proyecto implementa una **plataforma backend multiagente** cuyo objetivo es:

> Transformar una idea de software en un sistema ejecutable de forma progresiva y autónoma.

El sistema está diseñado para evolucionar hacia proyectos complejos mediante planificación jerárquica, descomposición progresiva, ejecución estructurada, recuperación automática ante fallos, evaluación por etapas y trazabilidad completa por proyecto.

---

## 🏗️ Estado actual del proyecto

### ✅ Núcleo implementado

#### 📦 Projects

- Multi-tenant por `project_id`
- Aislamiento completo por proyecto
- Inicialización automática del storage por proyecto

#### 📋 Tasks

- Jerarquía operativa actual:
  - `high_level`
  - `atomic`
- Estados soportados:
  - `pending`
  - `running`
  - `completed`
  - `failed`
  - `partial`
  - `awaiting_validation`
- Campos enriquecidos:
  - `objective`
  - `acceptance_criteria`
  - `technical_constraints`
  - `out_of_scope`
  - `task_type`
  - `priority`

#### ⚙️ Execution Runs

Cada ejecución genera un `ExecutionRun` con trazabilidad de:

- `work_summary`
- `work_details`
- `blockers_found`
- `validation_notes`
- `failure_type`
- `failure_code`
- `recovery_action`

#### 📁 Project Storage

- Separación por dominios, actualmente con `CODE_DOMAIN`
- Workspace aislado por ejecución
- Promoción explícita `workspace -> source` tras validación exitosa
- Bootstrap automático de storage al inicio del workflow

---

## 🧩 Arquitectura actual

### 1. Planner

Genera tareas `high_level` a partir de la descripción del proyecto.

Responsabilidades actuales:

- estructurar el proyecto en workstreams relevantes
- mantener nivel high-level
- no generar tareas atómicas
- no decidir ejecutor final

### 2. Atomic Task Generator

Convierte tareas `high_level` directamente en tareas `atomic`.

Cambio estructural importante:

Antes:
`high_level -> refined -> atomic`

Ahora:
`high_level -> atomic`

Esto reduce coste, iteraciones y ambigüedad.

Mejoras implementadas:

- el prompt ya está orientado a capacidades reales del `code_executor`
- evita semántica “multi-executor futuro” en el flujo activo
- busca entregables repo-based y file-based
- reduce tareas no ejecutables

### 3. Execution Sequencer

Genera batches y checkpoints a partir de las tareas atómicas.

Responsabilidades:

- ordenar la ejecución
- agrupar trabajo por batches
- definir checkpoints para evaluación posterior

### 4. Code Executor

Ejecuta tareas atómicas sobre workspace aislado.

Genera:

- cambios sobre archivos
- journal de ejecución
- edit plan
- output snapshot
- workspace changes

### 5. Validation Service

Valida el resultado del executor y genera siempre `code_validation_result`.

Fixes implementados:

- ya no quedan tareas fallidas sin artefacto de validación
- existe validación terminal para fallos/rechazos pre-validación
- separación entre validación normal y validación terminal

### 6. Recovery Service

Decide qué hacer cuando una tarea falla o queda parcial.

Contrato operativo actual:

- `retry`
- `reatomize`
- `insert_followup`
- `manual_review`

Mejoras implementadas:

- contrato único de recovery
- eliminación de rutas legacy ligadas a refinement
- mejor alineación con evaluator y post-batch
- soporte operativo real para `retry`

### 7. Evaluator

Evalúa el estado de la etapa tras cada batch.

Decide:

- `stage_completed`
- `stage_incomplete`
- `manual_review_required`

Y además define:

- estrategia de recovery
- necesidad de replan
- necesidad de follow-up
- cierre o no de etapa

Mejoras implementadas:

- eliminación de `refined` como nivel operativo
- eliminación de `retry_batch` del contrato nuevo
- reglas más estrictas para consistencia del output
- mejor tratamiento conceptual de fallos locales frente a replanning estructural

### 8. Post-Batch Processor

Orquesta lo que ocurre al terminar un batch:

- verifica estados terminales
- invoca recovery si hay tareas problemáticas
- llama al evaluator
- construye `PostBatchResult`
- decide continuidad, resequencing, replanning o bloqueo

Fixes implementados:

- exige artefacto de validación para tareas problemáticas
- alinea `RecoveryContext` con el resto del sistema
- evita incoherencias de stage closure en batches no finales
- corrige el bug de `checkpoint_blocked` incompatible con cierre de etapa

### 9. Project Workflow Service

Orquesta el pipeline end-to-end.

Flujo actual:

1. planner
2. atomic generation
3. execution plan
4. ejecución batch a batch
5. recovery
6. evaluation
7. iteraciones automáticas o cierre

Mejoras implementadas:

- refinement opcional, no obligatorio
- compatibilidad legacy con `refined`
- no envía automáticamente todo replanning a manual review
- permite iteraciones automáticas tras resequencing/replanning
- storage bootstrap al inicio del workflow

---

## 🔁 Workflow actual

### Flujo principal

1. Se genera planificación `high_level`
2. Se atomiza directamente
3. Se genera execution plan con batches y checkpoints
4. Se ejecutan tareas atómicas en orden
5. Cada tarea:
   - ejecuta
   - valida
   - si `completed`, promueve `workspace -> source`
6. Al terminar cada batch:
   - se procesan tareas problemáticas
   - se genera recovery si aplica
   - se evalúa el checkpoint
7. El workflow decide:
   - continuar
   - reatomizar / resecuenciar / replanificar
   - requerir revisión manual
   - cerrar etapa

---

## 🧠 Sistema de contexto

### Diseño actual

El contexto que recibe el executor se construye con:

- memoria operativa del proyecto
- memoria de tareas previas
- archivos ya existentes en el repositorio

Regla clave ya acordada:

> El context selector solo selecciona contexto ya existente en el repositorio.

También queda claro que:

- puede crearse un fichero nuevo durante la ejecución
- pero ese fichero nuevo no forma parte del contexto seleccionado
- si un path no existe, no debe seleccionarse como contexto

### Problemas detectados en esta capa

Se detectó que el selector estaba mezclando:

- paths de contexto existente
- paths de salida futuros

Esto provocó errores como:

- `Model selected unknown repository paths`
- bloqueos artificiales en tareas documentales o greenfield

### Dirección de arreglo ya definida

- pasar explícitamente al prompt la lista de ficheros existentes
- usar memoria de proyecto + ficheros existentes como única base contextual
- no romper el proceso por rutas inventadas fuera de responsabilidad del selector
- filtrar y degradar en vez de bloquear por completo cuando el modelo proponga paths inexistentes

---

## 🐛 Problemas detectados y cambios realizados

### 1. Atomic tasks no ejecutables

Problema:
- se generaban tareas incompatibles con las capacidades reales del executor

Solución:
- reescritura del prompt de atomic
- enfoque basado en capacidades reales del `code_executor`
- reducción del lenguaje ambiguo de “multi-executor futuro”

### 2. Fallos sin `code_validation_result`

Problema:
- post-batch rompía si una tarea fallaba sin artefacto de validación

Solución:
- validación terminal explícita
- persistencia de `code_validation_result` incluso en fallos y rechazos pre-validación

### 3. Desalineación entre generación atómica y cliente atomic

Problema:
- mismatch entre `parent_task_*` y `refined_task_*`

Solución:
- alineación de `atomic_task_generator.py` y `atomic_task_generator_client.py`

### 4. Recovery desalineado

Problemas:
- coexistían contratos incompatibles
- recovery reinterpretaba tareas de forma demasiado agresiva

Solución:
- contrato único en `schemas/recovery.py`
- `recovery_client.py` reescrito sobre ese contrato
- `recovery_service.py` reescrito para materialización coherente
- mejor preservación de intención original

### 5. Evaluator y post-batch inconsistentes

Problemas:
- `retry_batch` sin materialización real
- stage closure posible en momentos incoherentes
- demasiada tendencia a manual review

Solución:
- refactor del schema de evaluación
- limpieza del evaluator
- refactor de `post_batch_service.py`
- protección frente a cierre de etapa en batches intermedios

### 6. Workflow demasiado agresivo hacia manual review

Problema:
- `requires_replanning` acababa demasiado pronto en `awaiting_manual_review`

Solución:
- reescritura de `project_workflow_service.py`
- soporte para iteraciones automáticas de replanning/resequencing

### 7. Promoción a source

Problemas detectados:
- durante un tiempo la validación no promovía a `source`
- luego apareció un bug por llamada incompleta a `promote_workspace_to_source`

Soluciones:
- la promoción ahora ocurre tras validación `completed`
- antes de marcar task como `completed`
- se corrigió el uso de `domain_name=CODE_DOMAIN`

### 8. Logging y observabilidad del LLM

Problema:
- no había visibilidad real de latencia por llamada LLM

Solución:
- instrumentación centralizada en `/app/services/llm`
- logs de:
  - inicio de llamada
  - fin de llamada
  - duración
  - tamaño de prompt
  - tokens si están disponibles
  - errores
- timeout explícito en el provider de OpenAI
- soporte para override de modelo por llamada desde factory

### 9. Configuración LLM por tarea

Mejora implementada:

- `get_llm_provider(model=None)`
- por defecto usa `settings.openai_model`
- permite usar un modelo distinto para una llamada concreta

Ejemplo de intención ya acordada:
- usar un modelo más rápido como `gpt-5.4-mini` en tareas costosas como atomic generation

---

## 📊 Observabilidad actual

Se ha avanzado en logging de LLM para identificar:

- qué llamada tarda
- cuánto tarda
- qué schema usa
- tamaño del prompt
- si hay llamadas anormalmente lentas

Esto es especialmente importante porque se detectaron casos de llamadas extremadamente lentas en atomic generation.

---

## ⚠️ Limitaciones actuales

A día de hoy siguen existiendo estas limitaciones o frentes abiertos:

- ejecución completamente síncrona
- sin paralelización
- context selection todavía en fase de ajuste fino
- evaluator todavía sensible a diseño del post-batch
- latencias LLM todavía por optimizar
- falta de control fino de coste por etapa
- el README previo estaba desactualizado y no reflejaba esta evolución

---

## ✅ Estado funcional actual

El sistema ya dispone de:

- planificación high-level
- atomización directa
- secuenciación por batches
- ejecución de tareas atómicas
- validación terminal y normal
- promoción a source
- recovery estructurado
- evaluación por checkpoint
- workflow iterativo
- logging centralizado de llamadas LLM

En otras palabras, el pipeline ya está cerca de un E2E real, aunque todavía requiere estabilización especialmente en contexto, evaluación y latencia.

---

## 🧭 Siguientes pasos propuestos

### Prioridad alta

#### 1. Cerrar correctamente la capa de code context

Objetivo:
- asegurar que el selector solo use ficheros existentes
- evitar paths inventados
- no bloquear el proceso por errores fuera de su responsabilidad

#### 2. Ajustar fino evaluator + post-batch tras nuevas ejecuciones

Objetivo:
- comprobar que las nuevas reglas no escalan a manual review demasiado pronto
- validar que las decisiones de cierre y replanning son coherentes

#### 3. Medir latencias reales LLM por tipo de llamada

Objetivo:
- identificar qué servicios son más lentos
- decidir dónde usar modelos más rápidos
- ajustar prompts por coste/latencia

#### 4. Afinar recovery en base a resultados reales

Objetivo:
- comprobar si `retry`, `reatomize` e `insert_followup` se comportan como esperáis
- validar que preservan la intención original de la tarea

### Prioridad media

#### 5. Reducir coste y tamaño de prompts

Especialmente en:
- atomic generation
- evaluation
- recovery
- context selection

#### 6. Limpiar restos legacy de `refined`

Sigue habiendo trazas semánticas y metadata histórica que conviene limpiar cuando el flujo actual quede estable.

#### 7. Mejorar trazabilidad de promociones y reaperturas

Para dejar aún más claro cuándo:
- una task fue reintentada
- se promovió a source
- se abrió nueva iteración

### Futuro

- paralelización de batches si el modelo de storage/source lo permite
- especialización real por tipos de executor
- control de costes por proyecto o workflow
- endpoint E2E estable y productizable

---

## 🧠 Conclusión

El sistema ha evolucionado de una arquitectura con mucha dependencia de refinement y prompts ambiguos a un pipeline mucho más honesto con la realidad del executor y del workflow.

Los avances más importantes han sido:

- salto directo `high_level -> atomic`
- validación estructurada y persistente
- recovery coherente
- evaluator más limpio
- post-batch corregido
- workflow con iteraciones automáticas
- promoción a source tras validación
- observabilidad real de llamadas LLM

### Estado actual resumido

> La plataforma ya tiene una base E2E funcional y bastante más robusta que al inicio, pero todavía está en fase de estabilización fina en context selection, evaluator/post-batch y latencia de LLM.