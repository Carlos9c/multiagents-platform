# 🚀 Multiagent Platform – Backend

## 🧠 Visión

Este proyecto implementa una **plataforma backend multiagente** cuyo objetivo es:

> Transformar una idea de software en un sistema ejecutable de forma progresiva, autónoma y verificable sobre un workspace real.

El sistema no se limita a generar texto: **planifica, ejecuta, valida y recupera errores operando sobre artefactos reales (código, archivos, comandos, etc.)**.

---

# 🏗️ Estado actual del proyecto

## ✅ Núcleo funcional consolidado

Actualmente el sistema cubre un pipeline completo:

1. Planificación (planner)
2. Atomización (atomic)
3. Ejecución (execution engine)
4. Validación
5. Recovery
6. Post-batch + workflow

Todo ello con:
- ejecución real sobre workspace
- evidencias persistidas
- control de estado consistente
- tests pasando

---

## ⚙️ Execution Engine (estado actual)

### 🧩 Eliminación de `code_executor`

- ❌ Eliminado como concepto operativo
- ✅ Sustituido completamente por `execution_engine`
- 🔁 Alias legacy permitido solo en normalización puntual

👉 El ejecutor ya no se decide en atomic  
👉 Se resuelve dinámicamente en runtime por el engine

---

### 🧠 Arquitectura real

El sistema opera con:

- Orchestrator (LLM) → decide siguiente acción
- Subagentes → ejecutan acciones
- Tools → primitivas deterministas
- Evidence → trazabilidad completa

---

### 🔁 Acciones disponibles

- inspect_context
- resolve_file_operations
- apply_file_operations
- run_command
- finish
- reject

---

### 🤖 Subagentes activos

- context_selection_agent
- placement_resolver_agent
- code_change_agent
- command_runner_agent

---

### 🧰 Tools activas

- read_text_file
- write_text_file
- capture_file_snapshot
- restore_file_snapshot
- run_command

---

## 🔍 Catálogo de capacidades (nuevo)

Se ha introducido:

execution_engine/capabilities.py

Permite:

- Definir explícitamente:
  - subagentes
  - tools
  - límites
  - capacidades reales
- Inyectar esta información en:
  - orchestrator
  - atomic
  - recovery

👉 El sistema ya no usa capacidades implícitas

---

## 🧠 Atomic y Recovery alineados con capacidades reales

Antes:
- generaban tareas imposibles

Ahora:
- conocen capacidades reales
- generan tareas ejecutables
- evitan incoherencias

---

## 🔁 Tracking de ejecución real (nuevo)

Se han añadido:

- execution_agent_sequence (ExecutionRun)
- last_execution_agent_sequence (Task)

Permite:

- saber qué subagentes participaron
- trazabilidad completa
- debugging avanzado
- base futura para recovery inteligente

---

## 🧹 Limpieza estructural

Eliminado completamente:

- code_executor (uso activo)
- referencias en prompts
- referencias en atomic/recovery
- code_context_selection
- repo_inspector_agent
- repo_tree_tool

👉 Código muerto eliminado (no enrutable)

---

## 🔧 Mejora crítica: run_command

### Problema original

- shell=True
- ejecución arbitraria
- comportamiento impredecible

### Solución

- shell=False
- parseo seguro (shlex)
- bloqueo de operadores:
  - && || ; | > < >> etc.
- ejecución de un único comando

👉 Ahora run_command es:

“una acción puntual controlada, no scripting”

---

## 🧠 Ajustes en prompts

Se ha endurecido:

- no usar run_command para exploración
- no chaining
- no compensar mala planificación
- preferir finish

---

## 🧪 Testing

- Todos los tests pasan
- Ajustados a:
  - nuevos contratos
  - nuevos estados
  - eliminación de legacy

---

# ⚠️ Decisiones clave

## ✔️ No allowlist de comandos

Motivo:
- sistema multi-stack (Python, Java, Node, etc.)

Solución:
- restringir forma, no contenido

---

## ✔️ No mantener código muerto

- si no es enrutable → se elimina

---

## ✔️ Capacidades explícitas siempre

- todo definido en capabilities.py
- nada implícito

---

# 🚧 Deuda técnica

## 1. run_command aún usa string

Mejora futura:
- usar argv estructurado

---

## 2. step_kind no tipado

- sigue siendo string
- riesgo de errores

---

## 3. Prompts no verificados automáticamente

- falta coherencia garantizada con capabilities

---

## 4. Recovery no usa execution_agent_sequence

Gran oportunidad de mejora

---

## 5. Falta observabilidad avanzada

- no hay timeline
- debugging limitado

---

# 🧭 Siguientes pasos

## 🔥 Prioridad alta

### 1. Evolucionar run_command

- migrar a argv
- validación estructural

---

### 2. Recovery inteligente

- usar execution_agent_sequence
- adaptar estrategia según fallo real

---

### 3. Validación automática de capacidades

- test que detecte:
  - subagentes no registrados
  - tools no declaradas
  - código muerto

---

### 4. Tipado de step_kind

- usar constantes o enums

---

## ⚙️ Prioridad media

### 5. Endurecer prompts

- evitar loops
- mejorar decisiones finish/reject

---

### 6. Métricas del orchestrator

- nº pasos
- nº retries
- decisiones inválidas

---

### 7. Tests del engine más completos

- escenarios completos
- fallos controlados
- recovery

---

## 🧠 Futuro

### 8. Multi-executor real

- execution_engine como router
- múltiples backends

---

### 9. Planificación basada en ejecución real

- feedback loop execution → planner

---

### 10. Visualización del pipeline

- debug visual
- trazabilidad completa

---

# 🧠 Conclusión

El sistema ha evolucionado de:

❌ Generación de tareas + ejecución débil  
a  
✅ Sistema multiagente real con ejecución, validación y recuperación

Cambios clave:

- eliminación de code_executor
- capacidades explícitas
- orquestación real
- tools seguras
- limpieza de legacy

👉 El sistema ya no necesita reinvención, sino **refinamiento y robustez**