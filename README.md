# 🧠 Agente de Ejecución y Validación de Tareas

## 📌 Resumen del proyecto

Este proyecto implementa un sistema de ejecución autónoma de tareas basado en agentes, con un foco fuerte en:

* Ejecución controlada de tareas atómicas
* Validación estructurada multi-agente
* Persistencia consistente de artefactos
* Trazabilidad completa del flujo de trabajo
* Recuperación determinista ante fallos
* Verificación repo-local explícita mediante evidencia operacional

Flujo principal:

**Task → ExecutionRun → Execution Orchestrator → Subagents → Validation (multi-validator) → Aggregation → Artifact → Task closure → Hierarchy reconciliation**

---

## 🧱 Componentes principales

### 1. Execution Engine

* Ejecuta tareas mediante orquestador + subagentes
* Produce `ExecutionResult` con evidencia acumulada

Subagentes actuales:

* `context_selection_agent`
* `code_change_agent`
* `command_runner_agent`

---

### 2. Orchestrator

* Decide:

  * `call_subagent`
  * `finish`
  * `reject`
  * `invalid` (guardrail, no decisión operativa real)

* Fases:

  * `discovery`
  * `execution`

* Loop controlado por budget

* Notas clave:

  * `reject` → salida válida (no ejecutable)
  * `invalid` → error del LLM, consume budget y continúa

---

### 3. Task Execution Service

* Orquesta:

  * creación de run
  * ejecución
  * validación
  * persistencia
  * promoción de workspace
  * reconciliación jerárquica

* Responsabilidad crítica:

  * **garantizar atomicidad real del cierre**
  * degradar correctamente en caso de fallo

---

### 4. Validation Service (RE-DISEÑADO)

Nuevo enfoque:

* Sistema **multi-validador basado en evidencia**
* Sin routing legacy
* Sin builders intermedios
* Sin duplicación de contratos

Entrada única canónica:

```python
class TaskValidationInput(BaseModel):
    execution_request: ExecutionRequest
    execution_result: ExecutionResult
    intent: ResolvedValidationIntent | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

Salida única:

```python
ValidationResult
```

---

#### 🔹 Principios clave

* Cada validador:

  * consume TODO el input
  * decide qué usar
  * no recibe inputs especializados

* Validación basada en:

  * evidencia (`ExecutionEvidence`)
  * contexto real
  * ficheros del workspace

* NO:

  * no ejecuta comandos
  * no propone mejoras
  * no replanifica

---

#### 🔹 Validadores actuales

* `code_change_agent_validator`
* `command_runner_agent_validator`

Regla:

👉 **1 validador ↔ 1 subagente ejecutor**

---

#### 🔹 Flujo interno

1. Selección de validadores basada en:

   * `execution_agent_sequence`
   * (ignorando `context_selection_agent`)

2. Ejecución secuencial de validadores

3. Agregación:

```text
failed > manual_review > partial > completed
```

4. Resultado final único

---

#### 🔹 Aggregation

* Consolida:

  * findings
  * blockers
  * artifacts
  * evidence_ids

* Genera:

  * decisión final
  * summary agregado
  * metadata de trazabilidad

---

### 5. Artifact System

* Fuente de verdad del sistema
* Persistencia del resultado de validación agregado
* Base de auditoría completa

Incluye ahora:

* validadores ejecutados
* resultados individuales
* decisión agregada

---

### 6. Workspace Runtime

Estructura:

```
project/
├── domain_data/source
├── executions/<run_id>/
│   ├── workspace
│   ├── run
│   ├── logs
│   └── outputs
```

Semántica:

* `source` → estado persistido del proyecto
* `workspace` → overlay editable por ejecución
* `run` → entorno efímero para verificación
* `run` siempre se elimina
* promoción = overlay → source

---

### 7. Task Hierarchy

* Propagación determinista
* Sin efectos parciales
* Consistencia post-ejecución obligatoria

---

### 8. Post-Batch (WIP)

* Recovery
* Evaluation
* Plan mutation

Estado: **pendiente de reconstrucción sobre nueva validación**

---

## ✅ Estado actual

### 🧩 Arquitectura

* Orchestrator estable
* Subagentes alineados
* Execution → Validation desacoplado correctamente
* Eliminación completa de:

  * routing legacy
  * package_builder
  * context duplication

---

### ⚙️ Ejecución

* Flujo completo:

  * execution → validation → aggregation → persistencia

* Evidencia acumulativa:

  * multi-agente
  * estructurada

---

### 🧠 Validación

* Sistema multi-validator implementado

* Basado en:

  * evidencia real
  * lectura de ficheros
  * contexto completo

* Sin sobreingeniería:

  * input único
  * sin builders
  * sin contratos redundantes

---

### 🧪 Tests

* Validators → ✅ cubiertos
* Aggregation → ✅ cubierta
* Validation service → ✅ cubierto
* Orchestrator → ✅ estable

---

## 🧪 Invariantes

### Ejecución

* `finish` requiere evidencia
* `invalid` no rompe flujo
* ejecución siempre determinista

---

### Validación

* 1 input canónico
* múltiples validadores independientes
* agregación determinista
* no ejecución de comandos

---

### Persistencia

* 1 run → 1 validation artifact
* artifact contiene:

  * resultado agregado
  * resultados individuales
* task terminal ⇔ artifact existente

---

### Workspace

* aislamiento total entre runs
* run efímero
* promotion controlada

---

## 🚀 Últimos avances

* Eliminación completa de validation legacy
* Eliminación de `package_builder`
* Simplificación radical de contratos
* Introducción de:

  * validadores por subagente
  * aggregation determinista
* Validación basada en evidencia real
* Integración completa con execution_service
* Tests robustos para:

  * validators
  * aggregation
  * service

---

## 🧹 Limpieza realizada

* eliminación de:

  * routing legacy
  * múltiples contextos de validación
  * builders innecesarios
* simplificación de evidence handling
* unificación de contratos

---

## 🔭 Próximos pasos

### 🔴 Alta prioridad

1. Refinar validadores

* asegurar cobertura de casos edge
* robustez frente a evidencia incompleta

---

2. End-to-end real

* escenarios complejos
* múltiples agentes
* evidencia cruzada

---

3. Evidencia

* consolidar formato definitivo
* garantizar trazabilidad total

---

4. command_runner_agent + validación

* mejorar interpretabilidad de outputs
* validar casos reales (tests, builds, etc.)

---

### 🟠 Media prioridad

5. Post-batch
6. Auditoría avanzada
7. Métricas de validación

---

### 🟡 Baja prioridad

8. Refactor estructural menor
9. Configuración avanzada

---

## 🧠 Filosofía

* La verdad es el resultado validado
* Validación no re-ejecuta
* Sin heurísticas mágicas
* Sin contratos duplicados
* Sin sobreingeniería
* Evidencia como fuente única de verdad
* Agregación explícita, no implícita
* Orquestador coordina, no decide verdad

---

## 📌 Estado final

### Core

* ejecución sólida
* validación rediseñada
* agregación consistente
* persistencia coherente

### Sistema

* coherente end-to-end
* sin legacy crítico
* preparado para escalar validadores

### Siguiente foco real

👉 **Refinar validadores y casos reales complejos**

Después:

* post-batch
* autonomía completa
