# 🧠 Agente de Ejecución y Validación de Tareas

## 📌 Resumen del proyecto

Este proyecto implementa un sistema de ejecución autónoma de tareas basado en agentes, con un foco fuerte en:

* Ejecución controlada de tareas atómicas
* Validación estructurada multi-agente
* Persistencia consistente de artefactos
* Trazabilidad completa del flujo de trabajo
* Recuperación determinista ante fallos
* Verificación repo-local explícita mediante evidencia operacional
* Separación estricta entre coordinación, ejecución y validación

Flujo principal:

Task → ExecutionRun → Execution Orchestrator → Subagents → Validation (multi-validator) → Aggregation → Artifact → Task closure → Hierarchy reconciliation

---

## 🧱 Componentes principales

### 1. Execution Engine

* Ejecuta tareas mediante orquestador + subagentes
* Produce ExecutionResult con evidencia acumulada

Subagentes actuales:

* context_selection_agent
* code_change_agent
* command_runner_agent

---

### 2. Orchestrator

Decide:

* call_subagent
* finish
* reject
* invalid (guardrail, no decisión operativa real)

Fases:

* discovery
* execution

Notas clave:

* reject → salida válida
* invalid → error del LLM, consume budget
* coordina, no evalúa calidad
* no abre loops de “pulido”

Refinamientos recientes:

* cierre automático cuando:

  * contexto listo
  * implementación suficiente
  * verificación material ya cubierta
* un fallo de comando solo abre loop si es reparable
* warnings ≠ gap operativo
* enforcement de secuencia lógica entre subagentes
* separación entre fallo técnico y necesidad real de iteración

---

### 3. Task Execution Service

Orquesta:

* ejecución
* validación
* persistencia
* promoción
* reconciliación

Garantiza:

* atomicidad real
* consistencia run → artifact → task

---

### 4. Validation Service (RE-DISEÑADO)

Sistema multi-validador basado en evidencia.

Entrada:

TaskValidationInput

Salida:

ValidationResult

Principios:

* cada validador consume todo el input
* sin inputs especializados
* basado en evidencia real

NO:

* no ejecuta comandos
* no propone mejoras
* no replanifica

Validadores:

* code_change_agent_validator
* command_runner_agent_validator

Regla:

1 validador ↔ 1 subagente

Flujo:

1. selección
2. ejecución
3. agregación
4. resultado final

Orden de prioridad:

failed > manual_review > partial > completed

---

### 5. Artifact System

Fuente de verdad del sistema.

Incluye:

* resultados individuales
* resultado agregado
* evidencia
* trazabilidad

---

### 6. Workspace Runtime

Estructura:

project/
├── source
├── executions/<run_id>/
│   ├── workspace
│   ├── run

Semántica:

* source → estado persistido
* workspace → overlay
* run → entorno efímero
* run siempre se elimina

---

### 7. Command Runner Agent (REFORZADO)

Rol:

* decidir si verificar
* ejecutar comando si aplica
* registrar evidencia

Capacidades actuales:

* verificación repo-aware
* compatible con restricciones del runner
* independiente del lenguaje

Flujo en 2 fases:

1. selección de archivos relevantes
2. planificación del comando

Evidencia generada:

* command_execution
* notas de decisión
* outcome summary

Soporta:

* ejecución real
* verification_not_applicable

---

### 8. Code Change Agent (REFINADO)

Rol:

* materializar cambios completos

Mejoras:

* enfoque cross-language
* coherencia estructural
* cambios mínimos
* evitar refactors innecesarios

Limitaciones actuales:

* depende de la estructura existente
* puede introducir subestructuras nuevas
* falta enforcement fuerte de layout

---

### 9. Task Hierarchy

* propagación determinista
* sin efectos parciales

---

### 10. Post-Batch (WIP)

* recovery
* evaluación
* mutation

Refuerzo:

* invariantes terminales del plan
* stage_closure obligatorio

---

## ✅ Estado actual

### Arquitectura

* orquestador estable
* subagentes alineados
* validación desacoplada
* sin legacy crítico

Boundary claro:

* orquestador coordina
* subagentes ejecutan
* validadores juzgan

---

### Ejecución

* flujo completo funcional
* evidencia estructurada
* decisiones operativas más precisas

---

### Validación

* multi-validator funcional
* basada en evidencia
* agregación determinista

---

### Tests

* validators ✔
* aggregation ✔
* orchestrator ✔
* command_runner_agent ✔
* e2e básicos ✔

---

## 🧪 Invariantes

### Ejecución

* finish requiere evidencia
* invalid no rompe flujo
* cierre cuando checklist completo
* loops solo si hay gap real

---

### Validación

* input único
* validadores independientes
* agregación determinista

---

### Persistencia

* 1 run → 1 artifact
* artifact contiene verdad final

---

### Workspace

* aislamiento total
* run efímero
* promoción controlada

---

## 🚀 Últimos avances

* eliminación de validation legacy
* eliminación de package_builder
* simplificación de contratos
* multi-validator real
* mejora del cierre del orquestador
* command_runner_agent repo-aware y runner-aware
* eliminación de heurísticas dependientes de lenguaje
* soporte de no verificación
* mejora de prompts
* refuerzo de invariantes
* estabilización de loops

---

## 🧹 Limpieza realizada

* eliminación de routing legacy
* eliminación de builders
* unificación de contratos
* reducción de heurísticas débiles
* eliminación de lógica dependiente de lenguaje

---

## 🔭 Próximos pasos

### Alta prioridad

1. End-to-end complejos
2. Refinar orquestador
3. Mejorar disciplina estructural de code_change_agent
4. Promoción parcial de artefactos

---

### Media

5. Estructura tentativa de proyecto (high-level → execution)
6. Enforcements reales de paths
7. Contexto estructural del repo más rico
8. Post-batch

---

### Baja

9. Auditoría
10. Métricas
11. Refactors
12. Configuración avanzada

---

## 🧠 Filosofía

* la verdad es el resultado validado
* validación no re-ejecuta
* evidencia = fuente única
* sin sobreingeniería
* orquestador no decide verdad
* los agentes deben adaptarse al repo

---

## 📌 Estado final

Core:

* ejecución sólida
* validación robusta
* agregación consistente

Sistema:

* coherente end-to-end
* preparado para escalar

Foco actual:

👉 disciplina estructural + decisiones operativas reales

Siguiente paso:

👉 robustez en escenarios complejos y control del layout del repo
