# 🧠 Agente de Ejecución y Validación de Tareas

## 📌 Resumen del proyecto

Este proyecto implementa un sistema de ejecución autónoma de tareas basado en agentes, con un foco fuerte en:

- Ejecución controlada de tareas atómicas  
- Validación estructurada de resultados  
- Persistencia consistente de artefactos  
- Trazabilidad completa del flujo de trabajo  
- Recuperación determinista ante fallos  
- Verificación repo-local explícita mediante evidencia operacional  

Flujo principal:

**Task → ExecutionRun → Execution Orchestrator → Subagents → Validation → Artifact → Task closure → Hierarchy reconciliation**

---

## 🧱 Componentes principales

### 1. Execution Engine
- Ejecuta tareas mediante orquestador + subagentes
- Produce `ExecutionResult` con evidencia acumulada

Subagentes actuales:
- `context_selection_agent`
- `code_change_agent`
- `command_runner_agent`

---

### 2. Orchestrator
- Decide:
  - `call_subagent`
  - `finish`
  - `reject`
  - `invalid` (guardrail, no decisión operativa real)

- Fases:
  - `discovery`
  - `execution`

- Loop controlado por budget

- Notas clave:
  - `reject` → salida válida (no ejecutable)
  - `invalid` → error del LLM, consume budget y continúa

---

### 3. Task Execution Service
- Orquesta:
  - creación de run
  - ejecución
  - validación
  - persistencia
  - promoción de workspace
  - reconciliación jerárquica

- Responsabilidad crítica:
  - **garantizar atomicidad real del cierre**
  - degradar correctamente en caso de fallo

---

### 4. Validation Service
- Construye contexto de validación
- Router → Dispatcher
- Devuelve `ValidationResult`

Características:
- Consume evidencia
- No ejecuta comandos
- Decide estado final de la tarea

⚠️ Estado actual:
- **en revisión activa**
- inconsistencias detectadas en integración con execution_service

---

### 5. Artifact System
- Fuente de verdad del sistema
- Un resultado validado define el estado final
- Base de auditoría completa

---

### 6. Workspace Runtime

Estructura:
project/
├── domain_data/source
├── executions/<run_id>/
│ ├── workspace
│ ├── run
│ ├── logs
│ └── outputs


Semántica:

- `source` → estado persistido del proyecto  
- `workspace` → overlay editable por ejecución  
- `run` → entorno efímero para verificación  
- `run` siempre se elimina  
- promoción = overlay → source  

---

### 7. Task Hierarchy
- Propagación determinista
- Sin efectos parciales
- Consistencia post-ejecución obligatoria

---

### 8. Post-Batch (WIP)
- Recovery
- Evaluation
- Plan mutation

Estado: **parcialmente reconstruido**

---

## ✅ Estado actual

### 🧩 Contratos y arquitectura

- Orchestrator desacoplado correctamente
- Subagentes alineados con contratos nuevos
- Eliminación de acciones abstractas legacy
- Flujo basado en decisiones reales (`call_subagent`, etc.)

---

### ⚙️ Ejecución

- Flujo execution → validation integrado
- Persistencia estructurada de:
  - changed_files
  - commands
  - artifacts
- Evidencia acumulativa multi-agente

---

### 🗂️ Workspace

- Modelo correcto implementado:
  - overlay + baseline + run efímero
- Eliminado modelo previo inconsistente
- Aislamiento garantizado entre ejecuciones

---

### 🧪 Tests

- Tests del orchestrator → ✅ verdes  
- Tests de subagentes → ✅ verdes  
- Tests de command_tool → ✅ corregidos  

⚠️ Problema actual:
- **tests de invariantes de ejecución fallando**
- causa: integración con validation

---

### ⚠️ Problema crítico actual

Hay una desalineación entre:

- `ExecutionResult` (salida del execution engine)
- `ValidationService`
- `TaskExecutionService`

Síntomas:

- ejecuciones `completed/partial` degradan a `failed`
- artifacts de validación no se persisten
- promoción de workspace no ocurre
- tests de invariantes fallan masivamente

👉 Esto indica que:
**la validación no está respetando el contrato esperado por el execution flow**

---

## 🧪 Invariantes

### Ejecución
- no estados inválidos
- primer paso → context selection
- `finish` requiere evidencia
- `invalid` consume budget, no rompe flujo

---

### Workspace
- run siempre efímero
- source único
- sin contaminación entre ejecuciones

---

### Validación
- decide estado final
- no ejecuta comandos
- debe ser determinista respecto a evidencia

---

### Persistencia
- 1 run → 1 validation artifact
- task terminal ⇔ artifact existente

---

### Degradación
- fallo → estado consistente
- sin residuos intermedios

---

## 🚀 Últimos avances

- Rediseño completo del orchestrator
- Eliminación de `kind` y acciones legacy
- Introducción formal de:
  - `call_subagent`
  - `finish`
  - `reject`
  - `invalid`
- Rediseño del workspace runtime
- Implementación de `command_runner_agent`
- Endurecimiento de `run_command`
- Evidencia estructurada por tipo:
  - files_read
  - changed_files
  - commands
- Separación clara entre:
  - ejecución
  - validación
  - persistencia

---

## 🧹 Limpieza realizada

- eliminación de lógica heurística inválida (`relevant_files` artificial)
- eliminación de legacy en tests inconsistentes
- simplificación del flujo del orchestrator
- reducción de coupling entre componentes

---

## 🧹 Limpieza pendiente

- eliminar carpeta `workers`
- eliminar restos legacy en services/tests
- simplificar serialización de evidencia
- unificar contratos de contexto

---

## 🔭 Próximos pasos

### 🔴 Alta prioridad

### 1. Arreglar Validation Service (CRÍTICO)
- alinear con `ExecutionResult`
- respetar evidencia generada
- evitar degradación incorrecta a `failed`
- asegurar:
  - persistencia de artifact
  - coherencia decisión → estado

---

### 2. End-to-end execution flow
- execution → validation → artifact → closure
- sin fallback implícito a failure
- invariantes cumplidos

---

### 3. Evidencia
- revisar formato final:
  - strings vs estructuras
- coherencia entre:
  - runtime
  - persistencia
  - validación

---

### 4. run_command + validación
- asegurar que:
  - comandos generan evidencia usable
  - validación la consume correctamente

---

### 🟠 Media prioridad

5. Post-batch completo  
6. Tests end-to-end reales  
7. Auditoría de validación  

---

### 🟡 Baja prioridad

8. Refactor estructural  
9. Configuración y portabilidad  

---

## 🧠 Filosofía

- La verdad es el resultado validado  
- Validación no re-ejecuta  
- Usuario no corrige errores del sistema  
- Sin estados implícitos  
- Sin efectos parciales  
- Evidencia acumulativa y auditable  
- Orquestador coordina, no ejecuta  

---

## 📌 Estado final

### Core
- arquitectura sólida  
- subagentes funcionando  
- orquestador estable  
- workspace correcto  

### Problema actual
- validación rompe el cierre de ejecución  

### Siguiente foco real

👉 **Arreglar validation para desbloquear el sistema completo**

Después:

- consolidar invariantes  
- cerrar post-batch  
- avanzar a sistema autónomo completo  