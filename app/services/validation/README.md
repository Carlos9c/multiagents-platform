# 🧠 Validation Routing & Validator System

## 📌 Overview

El sistema de validación ha sido rediseñado para ser:

* **Escalable**
* **Explícito**
* **Basado en evidencia real**
* **Desacoplado del execution engine legacy**

El flujo completo es:

```
Routing LLM (estricto)
    ↓
Builder determinista
    ↓
Validator LLM (estricto)
    ↓
ValidationResult (canónico)
```

---

## 🧩 Componentes principales

### 1. Validation Router (LLM)

**Responsabilidad:**
Decidir **qué validador debe validar la tarea** y bajo qué condiciones.

**NO valida la tarea.**

**Input:**

* Task context
* Execution summary
* Evidence summary

**Output:**

```json
{
  "validator_key": "code_task_validator",
  "discipline": "code",
  "validation_mode": "post_execution",
  "requires_workspace": true,
  "requires_changed_files": true
}
```

**Propiedades clave:**

* Usa LLM con schema estricto
* No inventa validadores → usa catálogo
* Decide qué evidencia necesita el validador

---

### 2. Validation Builder (determinista)

**Responsabilidad:**
Construir el input completo del validador a partir de:

* Task
* ExecutionRequest
* ExecutionResult
* ExecutionRun
* Artifacts
* Workspace / Source files

**NO usa LLM.**

**Ejemplo de output:**

```python
CodeValidationInput(
    task=...,
    execution=...,
    request_context=...,
    evidence=...,
    file_snapshots=...,
    metadata=...
)
```

---

### 3. Validator (LLM)

**Responsabilidad:**
Evaluar si la tarea está:

* `completed`
* `partial`
* `failed`
* `manual_review`

**Basado en:**

* evidencia real
* archivos
* comandos
* artefactos
* contexto de ejecución

**Output:**

```json
{
  "decision": "completed",
  "summary": "...",
  "validated_scope": "...",
  "missing_scope": "...",
  "findings": [...],
  "confidence": "high"
}
```

---

### 4. Dispatcher

**Responsabilidad:**
Invocar el validador correcto en función de `validator_key`.

```python
if intent.validator_key == "code_task_validator":
    return validate_code_task_with_llm(...)
```

---

## 📁 Estructura de carpetas

```
app/services/validation/
├── contracts.py
├── dispatcher.py
├── builders/
│   └── code_validation_input_builder.py
├── router/
│   ├── service.py
│   ├── schemas.py
│   ├── prompt.py
│   └── registry.py
└── validators/
    ├── __init__.py
    └── code/
        ├── service.py
        ├── schemas.py
        └── prompt.py
```

---

## 🧠 Principios de diseño

### ✔ Separación estricta de responsabilidades

| Componente | Hace                |
| ---------- | ------------------- |
| Router     | Decide quién valida |
| Builder    | Prepara evidencia   |
| Validator  | Evalúa              |
| Dispatcher | Ejecuta             |

---

### ✔ LLMs con contratos estrictos

* Routing → schema validado
* Validator → schema validado
* Sin outputs libres

---

### ✔ Evidencia > intuición

El sistema valida usando:

* archivos modificados
* ejecución real
* artefactos
* contexto de workspace

---

### ✔ Sin magia implícita

* No auto-registro de validadores
* No discovery dinámico
* Todo explícito

---

## ➕ Cómo añadir un nuevo validador

Ejemplo: `api_contract_validator`

---

### Paso 1 — Crear carpeta

```
validators/
└── api_contract/
    ├── service.py
    ├── schemas.py
    └── prompt.py
```

---

### Paso 2 — Definir schema del output LLM

```python
class ApiContractValidationOutput(BaseModel):
    decision: Literal["completed", "partial", "failed", "manual_review"]
    summary: str
    findings: list[...]
    confidence: Literal["high", "medium", "low"]
```

---

### Paso 3 — Crear prompt del validador

Debe:

* explicar el rol
* definir reglas de validación
* restringir el output al schema

---

### Paso 4 — Implementar servicio

```python
def validate_api_contract_task_with_llm(validation_input: ApiValidationInput) -> ValidationResult:
```

Debe:

* llamar al LLM con schema estricto
* validar salida con Pydantic
* mapear a `ValidationResult`

---

### Paso 5 — Crear builder (si aplica)

```
builders/api_validation_input_builder.py
```

Debe:

* construir input determinista
* NO usar LLM

---

### Paso 6 — Registrar en el catálogo del router

En:

```
router/registry.py
```

Añadir:

```python
ValidationRouterCatalogEntry(
    validator_key="api_contract_validator",
    discipline="api",
    typical_deliverables=[...],
    typical_evidence=[...],
)
```

---

### Paso 7 — Extender dispatcher

```python
if intent.validator_key == "api_contract_validator":
    return validate_api_contract_task_with_llm(...)
```

---

### Paso 8 — Tests

Cubrir:

* routing
* builder
* validator
* fallback

---

## 🚫 Antipatrones (NO hacer)

❌ Validar dentro del router
❌ Usar LLM en builders
❌ Inventar campos fuera del schema
❌ Auto-descubrir validadores
❌ Lógica en `__init__.py`
❌ Mezclar ejecución y validación

---

## 🔮 Escalabilidad futura

* múltiples disciplinas (code, api, infra, data)
* validadores especializados
* posible `validation_engine`

---

## ✅ Estado actual

✔ Routing LLM funcional
✔ Builder determinista de código
✔ Validator LLM de código
✔ Dispatcher operativo
✔ Tests en verde

---

## 🧭 Siguientes pasos recomendados

1. Crear `validation/service.py` (orquestador)
2. Integrar en `task_execution_service`
3. Añadir nuevos validadores
4. Mejorar prompts
5. Añadir evaluación cruzada (futuro)

---
