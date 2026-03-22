# 🚀 Multiagent Platform – Backend

## 🧠 Visión

Este proyecto implementa una **plataforma backend multiagente** cuyo objetivo es:

> Transformar una idea de software en un sistema ejecutable de forma progresiva, estructurada y autónoma.

El sistema está diseñado para escalar hacia proyectos complejos mediante:

* planificación jerárquica
* descomposición progresiva
* ejecución especializada por dominio
* validación semántica real
* trazabilidad completa por proyecto

---

# 🏗️ Estado actual del proyecto

## ✅ Núcleo implementado

### 📦 Projects

* Multi-tenant por `project_id`
* Aislamiento lógico completo
* Storage físico por proyecto (`project_storage`)

---

### 📋 Tasks (modelo avanzado)

* Jerarquía: `high_level → refined → atomic`
* Estados definidos:

  * `pending`
  * `running`
  * `awaiting_validation`
  * `completed`
  * `partial`
  * `failed`
* Asignación de `executor_type` en nivel atómico

---

### 🧠 Planner

* Genera tareas de alto nivel
* Persistencia estructurada del plan

---

### 🔧 Technical Task Refiner

* Convierte tareas high-level en tareas técnicas refinadas

---

### ⚙️ Atomic Task Generator

* Convierte tareas refinadas en tareas ejecutables atómicas
* Asigna executor concreto (`code_executor`)

---

### 🧩 Execution Plan

* Generación de batches ejecutables
* Checkpoints obligatorios tras cada batch
* Persistencia del plan

---

# ⚡ Ejecución de tareas (Code Executor)

## 🧱 Pipeline completo de ejecución (7 fases)

1. **Preparación de workspace**
2. **Resolución de contexto**
3. **Construcción de working set**
4. **Planificación de cambios (LLM)**
5. **Generación de código (LLM)**
6. **Aplicación de cambios en workspace**
7. **Construcción de journal + resultado**

---

## 📁 Workspace Runtime

Cada ejecución ocurre en:

```
executions/{run_id}/workspace/
```

Y se inicializa copiando:

```
domain_data/code/source/
```

➡️ Esto garantiza aislamiento por ejecución.

---

## 🧠 Fuente de conocimiento por tarea

Cada tarea construye su contexto a partir de:

1. **Baseline canónico**

   * `domain_data/code/source/`

2. **Workspace de ejecución**

   * copia del source en `executions/{run_id}/workspace/`

3. **Contexto lógico**

   * `CodeExecutorInput`

4. **Working set**

   * subconjunto de archivos relevantes

5. **Edit plan**

   * decisiones del LLM

6. **Cambios reales**

   * `WorkspaceChangeSet`

7. **Resultado de ejecución**

   * `CodeExecutorResult`

---

## ⚠️ Problema identificado (IMPORTANTE)

Actualmente la selección de contexto es:

> ❌ parcialmente heurística y rudimentaria

Esto **NO es aceptable** para un sistema de este nivel.

---

# 🧪 Validación (Code Validator)

## 🎯 Objetivo

Determinar de forma estricta:

> Si la tarea realmente satisface lo que se pidió

---

## 🔍 Qué evalúa

El validador utiliza:

* Task original
* Execution result
* Diff real del workspace
* Snapshots finales de archivos
* Evidencia estructurada

---

## 🤖 Validación semántica (LLM)

Decide únicamente:

* `completed`
* `partial`
* `failed`

Sin recomendaciones ni mejoras.

---

## 📦 Output

Se genera:

* `code_validation_result` (artifact)

---

# 🔄 Promoción a source (CRÍTICO)

## ✔️ Nueva regla del sistema

Después de validación:

### Si `completed`

➡️ Se promociona el workspace a:

```
domain_data/code/source/
```

### Si `partial` o `failed`

➡️ NO se promociona nada

---

## 🧠 Implicación clave

> `source/` es el estado canónico del proyecto

Todas las tareas futuras parten de ahí.

---

## ❗ Garantía fuerte

Si la promoción falla:

➡️ la tarea pasa a `failed`

Nunca puede existir:

* task `completed`
* sin reflejo en `source/`

---

# 🛠️ Post Batch Processing

## ✔️ Garantías actuales

* Solo se ejecuta si TODAS las tareas están en estado terminal
* Nunca con tareas en `awaiting_validation`

---

## 🔁 Recovery (RE-DISEÑADO)

Ahora el recovery usa:

### 1. Execution context

* Qué hizo el executor

### 2. Validation context (NUEVO)

* Por qué la tarea no cumple

---

## ⚠️ Cambio clave

Antes:

* recovery basado en `ExecutionRun`

Ahora:

* recovery basado en:

  * execution context
  * validation context

---

## 🧠 Recovery ahora es semántico

Puede decidir:

* retry
* reatomize
* insert_followup
* manual_review

---

# 🧠 Evaluación (Evaluator)

* Se ejecuta tras cada batch
* Decide:

  * continuar ejecución
  * replanificar
  * resecuenciar
  * cerrar etapa

---

# 🧱 Project Storage

## 📁 Estructura

```
projects/{project_id}/
  project_meta/
  artifacts/
  executions/
  domain_data/
    code/
      source/
```

---

## 🚀 Bootstrap automático

Ahora ocurre en:

`run_project_workflow(...)`

➡️ `_bootstrap_project_storage_for_execution(...)`

---

# ⚠️ Limitaciones actuales

## 1. ❌ Context selection rudimentaria

* Falta selector inteligente
* No hay control fino de tokens/contexto

## 2. ❌ No hay indexación del repo

* No hay búsqueda semántica
* No hay chunking ni embeddings

## 3. ❌ Candidate files heurísticos

* `_infer_candidate_files` es débil

---

# 🚀 Siguientes pasos (CRÍTICOS)

## 🔥 1. Code Context Selector (URGENTE)

Diseñar:

```
app/services/code_context_selector.py
```

### Responsabilidades

* Selección inteligente de contexto
* Decidir:

  * qué archivos entran
  * qué excluir
  * qué artifacts usar
* Justificar decisiones
* Detectar contexto insuficiente

---

## 🔥 2. Indexación del repositorio

* Embeddings de archivos
* Búsqueda semántica
* Chunking inteligente

---

## 🔥 3. Mejora de working set

* Dejar de depender solo de candidate files
* Introducir scoring de relevancia

---

## 🔥 4. Control de contexto del LLM

* Límite de tokens real
* Priorización de contenido
* Resúmenes automáticos

---

## 🔥 5. Mejora del execution plan

* Adaptativo en función del estado real del repo
* No solo del plan inicial

---

## 🔥 6. Observabilidad

* Debug de:

  * contexto usado
  * decisiones del LLM
  * selección de archivos

---

## 🔥 7. Multi-executor (futuro)

* `code_executor` (actual)
* `documentation_executor` (opcional)
* otros dominios

---

# 🧭 Estado actual

## ✔️ Lo bueno

* Pipeline completo E2E funcional
* Validación semántica real
* Recovery inteligente
* Workspace aislado
* Source canónico definido
* Arquitectura modular

---

## ⚠️ Lo que falta

* Context intelligence real
* Retrieval robusto
* Escalabilidad de contexto

---

# 🧠 Conclusión

El sistema ya NO es un prototipo.

Es un **motor de ejecución multiagente real**, pero aún no es:

> suficientemente inteligente en la selección de contexto

Ese es el siguiente salto crítico.

---

# 🎯 Prioridad inmediata

1. Code Context Selector
2. Indexación del repo
3. Mejora del working set

Sin eso, el sistema ejecuta…
pero no escala.

Con eso, empieza a ser realmente potente.
