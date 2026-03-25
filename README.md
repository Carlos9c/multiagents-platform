# 🚀 Multiagent Platform – Backend

## 🧠 Visión

Este proyecto implementa una **plataforma backend multiagente** cuyo objetivo es:

> Transformar una idea de proyecto en un sistema ejecutable de forma progresiva y autónoma.

El sistema está diseñado para escalar hacia proyectos complejos mediante:

- planificación jerárquica
- descomposición progresiva
- ejecución orquestada
- validación iterativa
- trazabilidad completa por proyecto

---

# 🏗️ Estado actual del proyecto

## ✅ Núcleo implementado

### 📦 Projects

- Multi-tenant por `project_id`
- Configuración inicial del proyecto (incluyendo `enable_technical_refinement`)
- Aislamiento completo por workspace

---

### 📋 Tasks

Modelo avanzado con soporte para:

- `planning_level`: high_level / refined / atomic
- `status`: pending, completed, failed, partial, etc.
- `executor_type`: ahora resuelto dinámicamente por el orchestration engine
- jerarquía padre-hijo
- control de bloqueo (`is_blocked`)

---

### 🧠 Planner

- Generación de tareas high-level a partir del objetivo del proyecto
- Replanning automático en función de evaluación
- Iterativo y compatible con checkpoints

---

### 🧩 Technical Task Refiner (opcional)

- Activado mediante `project.enable_technical_refinement`
- Introduce capa intermedia entre high-level y atomic
- Permite mayor control en proyectos complejos
- Totalmente integrado en el workflow sin romper compatibilidad

---

### ⚙️ Atomic Task Generation

- Generación de tareas ejecutables reales
- Garantiza compatibilidad con capacidades del sistema
- Evita tareas imposibles (ej: ejecutar código si no hay executor)

---

### 🧪 Execution Engine (Orchestrated)

- Sustituye completamente al sistema legacy
- Arquitectura basada en:
  - orquestador
  - subagentes
  - tools
- Controlado por budget (`LoopBudget`)
- Ejecución paso a paso con trazabilidad completa

---

### 🔁 Task Execution Service

- Punto central de ejecución de tareas
- Resolución dinámica del executor
- Reconciliación de estados:
  - atomic → high-level
- Integración con validation y recovery

---

### 🔍 Validation

- Evaluación estructurada de resultados
- Decisiones:
  - completed
  - partial
  - failed
  - rejected
- Basada en evidencia real (diff, logs, outputs)

---

### 🛠️ Recovery Service

- Actúa ante fallos o resultados parciales
- Estrategias:
  - retry
  - follow-up tasks
  - reatomization
- No rompe el plan global (recuperación local)

---

### 📦 Execution Plan

- Generación de batches secuenciales
- Checkpoints tras cada batch
- Permite:
  - replan
  - resequencing
  - introducción de nuevas tareas

---

### 🔄 Project Workflow

Pipeline completo end-to-end:

1. Planner
2. (Opcional) Technical refinement
3. Atomic generation
4. Execution plan
5. Execution batches
6. Recovery / replan si aplica
7. Post-batch evaluation

---

## 🧪 Testing

- Tests unitarios y de integración cubriendo:
  - execution engine
  - task execution service
  - project workflow
  - post-batch logic
- Fixtures completas con base de datos temporal
- Sistema estable tras eliminación de legacy

---

# ⚠️ Problemas detectados recientes

## 1. Identidad de batches incorrecta

- `batch-1` se reutiliza en múltiples iteraciones
- Provoca:
  - duplicados en `completed_batches`
  - trazabilidad incorrecta
  - ambigüedad en logs

👉 Necesario introducir identidad compuesta (ej: `plan_version + batch_id`)

---

## 2. Semántica residual de ejecutores

- `code_executor` ya no es real (legacy)
- El orquestador decide ejecución

👉 Necesario:
- redefinir naming
- limpiar referencias en prompts y código

---

## 3. Restos de constantes legacy

- Strings duplicados (`pending`, `high_level`, etc.)
- Posibles inconsistencias

👉 Centralizar en modelos

---

## 4. Artefactos y naming legacy

- Posibles referencias a:
  - `code_executor_*`
- Necesario transición progresiva

---

## 5. Prompts desalineados

- Algunas instrucciones aún asumen modelo antiguo de ejecución

---

# 🧹 Limpieza realizada recientemente

- Eliminación completa del `legacy_local_engine`
- Refactor del execution engine a modelo orquestado
- Eliminación de dependencia de executor en atomic tasks
- Corrección de tests y compatibilidad total
- Introducción de `enable_technical_refinement` en Project
- Refactor completo del workflow para soportar refinamiento opcional

---

# 🚧 Siguientes pasos

## 🔥 Prioridad alta

### 1. Corregir identidad de batches

- Introducir `batch_key` único:
  - opción: `{plan_version}:{batch_id}`
- Ajustar:
  - `completed_batches`
  - `blocked_batches`
  - `iterations`

---

### 2. Limpiar constantes globales

- Centralizar en `models.task`
- Eliminar duplicados en services
- Evitar strings hardcodeados

---

### 3. Redefinir executor_type

- Opciones:
  - mantener `code_executor` como alias temporal
  - o renombrar a algo más realista (`workspace_executor`, etc.)

---

### 4. Limpiar execution_plan_service

- Eliminar lógica dependiente de executor legacy
- Asegurar que el plan es agnóstico del ejecutor

---

## 🧠 Prioridad media

### 5. Revisar capabilities

- Alinear con nuevo modelo de ejecución
- Eliminar dependencias implícitas de nombres legacy

---

### 6. Actualizar prompts

- Planner
- Atomic generator
- Recovery

👉 Objetivo: que el LLM entienda el sistema actual

---

### 7. Transición de artefactos

- Mantener compatibilidad de lectura
- Empezar a emitir nuevo formato
- Eliminar legacy progresivamente

---

## 🧩 Prioridad baja

### 8. Documentación interna

- Execution engine
- Workflow real
- Capas del sistema

---

# 🧭 Dirección futura

El sistema ya no es un simple pipeline:

👉 Es un **runtime de agentes orquestados con planificación adaptativa**

Los siguientes pasos van hacia:

- mejor trazabilidad
- mayor control del flujo
- eliminación total de legacy
- preparación para proyectos complejos reales

---

# ✅ Estado general

- Core funcional: ✔️
- Tests: ✔️
- Orquestación: ✔️
- Validación + recovery: ✔️
- Refinamiento opcional: ✔️

👉 **Sistema listo para fase de hardening y simplificación**
