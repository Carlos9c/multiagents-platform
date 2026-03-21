# 🚀 Multiagent Platform – Backend

## 🧠 Visión

Este proyecto implementa una **plataforma backend multiagente** cuyo objetivo es:

> Transformar una idea de software en un sistema ejecutable de forma progresiva y autónoma.

El sistema está diseñado para escalar hacia proyectos complejos (web apps, APIs, videojuegos, etc.) mediante:

* planificación jerárquica
* descomposición progresiva
* ejecución distribuida
* abstracción de LLMs
* trazabilidad completa por proyecto

---

# 🏗️ Estado actual del proyecto

## ✅ Núcleo implementado

### 📦 Projects

* Multi-tenant por `project_id`
* Base de aislamiento del sistema

---

### 📋 Tasks (modelo avanzado)

Sistema de tareas rediseñado para planificación escalable:

#### 🔑 Características clave

* Jerarquía:

  * `parent_task_id`
* Niveles de planificación:

  * `high_level`
  * `refined`
  * `atomic`
* Tipos de tarea:

  * `implementation`
  * `documentation`
  * `onboarding`
  * `testing`
* Preparado para múltiples ejecutores:

  * `executor_type`

#### 🧩 Campos clave

| Campo                | Descripción             |
| -------------------- | ----------------------- |
| planning_level       | Nivel de planificación  |
| parent_task_id       | Relación jerárquica     |
| proposed_solution    | Solución técnica        |
| implementation_steps | Pasos concretos         |
| tests_required       | Validación              |
| acceptance_criteria  | Definición de terminado |

---

### ⚙️ Execution Runs

* Trazabilidad completa de ejecución
* Estados de ejecución persistidos

---

### 📁 Artifacts

* Outputs versionables
* Base para código generado

---

### 🤖 Planner Agent (IMPLEMENTADO)

Responsable de:

* convertir una idea en tareas `high_level`
* validar calidad mínima
* asegurar presencia de:

  * documentación
  * onboarding

#### Output

* tareas persistidas en DB
* artifact `project_plan`

---

### 🧱 Infraestructura

* FastAPI
* PostgreSQL + SQLAlchemy + Alembic
* Celery (preparado)
* Arquitectura modular

---

# 🧬 Arquitectura del sistema

```
User Input
   ↓
(🔜 Requirements Gate)
   ↓
Planner (✔️)
   ↓
High-Level Tasks
   ↓
(🔜 Technical Task Refiner)
   ↓
Refined Tasks
   ↓
(🔜 Atomic Task Generator)
   ↓
Atomic Tasks
   ↓
Executor
   ↓
Artifacts
```

---

# 🧠 Filosofía del sistema

> El planner piensa
> El refiner aterriza
> El executor ejecuta

---

# ⚠️ Problemas abordados

## ❌ Tareas demasiado abstractas

✔️ Solución:

* validaciones estrictas
* nuevo modelo de tareas

---

## ❌ Límite de tokens en proyectos grandes

✔️ Solución:

* planificación jerárquica
* descomposición progresiva

---

## ❌ Falta de ejecutabilidad real

✔️ Solución:

* introducir nivel atómico (prioridad inmediata)

---

# 🚀 Roadmap actualizado (PRIORIDAD REAL)

## 🔥 FASE ACTUAL (CRÍTICA)

### 1. Technical Task Refiner (INMEDIATO)

Convierte:

```
high_level → refined
```

#### Output esperado

* tareas técnicas concretas
* solución propuesta
* pasos de implementación
* tests definidos

#### Campos clave

* `planning_level = refined`
* `parent_task_id`
* `proposed_solution`
* `implementation_steps`
* `tests_required`

---

### 2. Atomic Task Generator (INMEDIATO)

Convierte:

```
refined → atomic
```

#### Output esperado

Tareas ejecutables por máquina:

* cambio concreto
* scope pequeño
* testeable
* determinista

#### Ejemplo

```
"Crear servicio Orchestrator"
↓
- crear archivo orchestrator.py
- definir clase OrchestratorService
- implementar método start_run
- añadir test unitario
```

---

### 3. Adaptar Executor (INMEDIATO)

* Ejecutar **solo tareas `atomic`**
* fallback opcional a `refined`

---

### 4. Trazabilidad completa (INMEDIATO)

Relación jerárquica:

```
high_level → refined → atomic
```

---

# 🧠 Modelo de planificación final

## Estructura jerárquica

```
Epic (high_level)
  → Technical Tasks (refined)
      → Atomic Tasks (atomic)
```

---

## Beneficios

* evita límites de contexto
* permite proyectos grandes
* mantiene coherencia arquitectónica
* habilita ejecución progresiva

---

# 🔜 FASE SIGUIENTE

## Requirements Gate

Antes del planner:

* hacer preguntas al usuario
* detectar ambigüedades
* enriquecer input

---

## Markdown Plan Generator

Generación opcional:

```
docs/project_plan.md
```

Incluye:

* arquitectura
* tareas
* fases

---

## Planning Loop (guardrail)

Para proyectos grandes:

```
plan → incomplete → continue → merge
```

---

# 🧪 Testing (pendiente)

* planner tests
* refiner tests
* atomic generator tests
* E2E pipeline

---

# 🔐 Seguridad (pendiente)

* API keys
* RLS en PostgreSQL
* aislamiento por proyecto

---

# 🚀 Cómo ejecutar el proyecto

## 1. Clonar repo

```bash
git clone <repo>
cd multiagent-platform
```

---

## 2. Instalar dependencias

```bash
poetry install
```

o

```bash
pip install -r requirements.txt
```

---

## 3. Configurar entorno

```bash
cp .env.example .env
```

---

## 4. Migraciones

```bash
alembic upgrade head
```

---

## 5. Ejecutar API

```bash
uvicorn app.main:app --reload
```

---

## 6. Ejecutar planner

```
POST /planner/projects/{id}/plan
```

---

# 🎯 Estado real del proyecto

## ✔️ Ya resuelto

* modelo de tareas escalable
* planner funcional
* persistencia sólida

## ⚠️ En progreso (crítico)

* technical refiner
* atomic generator
* executor real sobre atomic

---

# 🧠 Visión a largo plazo

Este sistema evolucionará hacia:

* generación completa de software
* agentes especializados
* ejecución autónoma supervisada
* soporte multimodal (código, UI, infra)

---

# 💡 Idea clave

Este proyecto no es un backend tradicional.

Es un:

> **sistema de generación autónoma de software basado en agentes**

---

# ▶️ Siguiente paso recomendado

👉 Implementar **Technical Task Refiner**

(ya diseñado para alimentar el Atomic Task Generator)

---

**Una vez eso esté listo, el sistema deja de ser “planner” y pasa a ser “builder”.**
