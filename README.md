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

Task → ExecutionRun → Execution Orchestrator → Subagents → Validation → Artifact → Task closure → Hierarchy reconciliation

---

## 🧱 Componentes principales

### 1. Execution Engine
- Ejecuta tareas mediante orquestador + subagentes
- Produce `ExecutionResult` con evidencia acumulada

Subagentes:
- context_selection_agent
- code_change_agent
- command_runner_agent

---

### 2. Orchestrator
- Decide:
  - call_subagent
  - finish
  - reject
- Fases:
  - discovery
  - execution
- Loop con budget
- Diferencia:
  - DECISION_REJECT → no hay ruta
  - DECISION_INVALID → error, sigue loop

---

### 3. Task Execution Service
- Orquesta:
  - run
  - ejecución
  - validación
  - persistencia
- Garantiza atomicidad

---

### 4. Validation Service
- Construye contexto
- Router → dispatcher
- Devuelve `ValidationResult`
- Consume evidencia, no ejecuta comandos

---

### 5. Artifact System
- Fuente de verdad
- Resultado validado = cierre real
- Base de auditoría

---

### 6. Workspace Runtime

Estructura:

project/
- domain_data/source
- executions/<run_id>/
  - workspace
  - run
  - logs
  - outputs

Semántica:
- source = base persistida
- workspace = cambios
- run = entorno efímero de verificación
- run se elimina siempre
- promoción = overlay sobre source

---

### 7. Task Hierarchy
- Propagación determinista
- Sin efectos parciales

---

### 8. Post-Batch (WIP)
- Recovery
- Evaluation
- Plan mutation

Estado: en reconstrucción

---

## ✅ Estado actual

### Consistencia
- Sin commits intermedios
- rollback correcto

### Atomicidad
- cierre = atómico + consistente

### Contratos
- Orchestrator → coordina
- Subagents → producen evidencia
- Validation → decide
- Artifact → fija verdad

---

### Orquestación
- Ya no hay acciones abstractas
- Se llaman subagentes reales
- Simplificación clara del loop

---

### Workspace
- Eliminado modelo incorrecto previo
- run tree efímero correcto
- evita contaminación y loops

---

### Evidencia
- acumulativa
- multi-agente
- usable por validación

---

### Post-batch
- normalización corregida
- tests verdes en esa parte
- flujo completo pendiente

---

## 🧪 Invariantes

### Ejecución
- no estados inválidos
- no repetir subagente
- primer paso = context
- finish requiere evidencia
- invalid consume budget

### Workspace
- run siempre efímero
- source único
- sin legacy partitions

### Validación
- coherencia decisión/estado
- no ejecuta comandos

### Persistencia
- 1 run → 1 artifact
- task terminal ⇔ artifact

### Degradación
- fallo limpio
- sin residuos

### Plan
- stage_closure correcto
- consistencia batch

---

## 🧪 Tests

Cubren:

- flujo vertical
- invariantes
- workspace
- orchestrator
- plan mutation

Estado: core en verde

---

## 🚀 Últimos avances

- Nuevo modelo de orquestador
- Eliminación de actions legacy
- Introducción:
  - CALL
  - FINISH
  - REJECT
  - INVALID
- Eliminación de `kind`
- Rediseño workspace:
  - source / workspace / run
- run_command con árbol efímero
- limpieza automática
- normalización de plan
- separación clara de errores vs decisiones

---

## 🧹 Limpieza pendiente

- eliminar workers
- eliminar restos legacy
- consolidar tests
- simplificar evidencia

---

## 🔭 Próximos pasos

### Alta prioridad

1. run_command end-to-end
- comando + cwd correctos
- evidencia persistida
- validación consume evidencia

2. Mejorar evidencia
- estructura clara
- trazabilidad por agente

3. post_batch_service
- recovery → evaluation → mutation
- coherencia total

4. manual review
- separar:
  - user clarification
  - gap interno
  - constraint externo

---

### Media prioridad

5. auditoría de validación  
6. tests end-to-end reales  
7. simplificación artifacts  

---

### Baja prioridad

8. refactor estructural  
9. configuración y portabilidad  

---

## 🧠 Filosofía

- La verdad es el resultado validado
- Validación no re-ejecuta
- Usuario no tapa fallos del sistema
- Sin estados implícitos
- Sin efectos parciales
- Evidencia acumulativa y auditable
- Orquestador coordina agentes reales

---

## 📌 Estado final

Core:
- estable
- coherente
- alineado con subagentes
- sin legacy crítico

Post-batch:
- bien definido
- pendiente de cierre

Siguiente foco:

cerrar run_command + mejorar evidencia + completar recovery y planificación dinámica