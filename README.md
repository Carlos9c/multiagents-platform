# 🧠 Agente de Ejecución y Validación de Tareas

## 📌 Resumen del proyecto

Este proyecto implementa un sistema de ejecución autónoma de tareas basado en agentes, con un foco fuerte en:

- Ejecución controlada de tareas atómicas  
- Validación estructurada de resultados  
- Persistencia consistente de artefactos  
- Trazabilidad completa del flujo de trabajo  
- Recuperación determinista ante fallos (en evolución)

El sistema orquesta el siguiente flujo:

**Task → ExecutionRun → Execution → Validation → Artifact → Task closure → Hierarchy reconciliation**

---

## 🧱 Componentes principales

### 1. Execution Engine
- Ejecuta tareas mediante agentes
- Produce `ExecutionResult` con:
  - evidencia (archivos, comandos, etc.)
  - resumen del trabajo
  - estado (`completed`, `partial`, `failed`, `rejected`)

---

### 2. Task Execution Service
- Orquesta todo el flujo:
  - creación de run
  - ejecución
  - validación
  - persistencia final
- Gestiona degradación ante errores
- Garantiza atomicidad del cierre

---

### 3. Validation Service
- Construye contexto de validación
- Decide el validador (router)
- Ejecuta validación (dispatcher)
- Devuelve `ValidationResult` estructurado

---

### 4. Artifact System
- Fuente de verdad del resultado validado
- `validation_result` = cierre canónico de la tarea
- Permite trazabilidad completa y auditoría

---

### 5. Task Hierarchy
- Consolida estado de tareas padre
- Propaga cambios de forma determinista
- Sin efectos colaterales parciales

---

### 6. (WIP) Post-Batch Processing
- Evaluación tras ejecución de batches
- Integración con:
  - recovery
  - evaluación de checkpoint
  - mutación del plan

⚠️ **Estado actual:** en revisión / rollback parcial  
Se ha detectado desalineación entre:
- contrato esperado por tests
- implementación real del servicio

---

## ✅ Estado actual del sistema

### 🔒 Consistencia transaccional
- Eliminados commits intermedios peligrosos
- Uso correcto de:
  - `flush()` durante construcción
  - `commit()` solo en puntos de frontera
  - `rollback()` en fallos

---

### 🔁 Atomicidad garantizada

El cierre de una tarea (validación + artifact + estado) es:

- Atómico  
- Consistente  
- Sin efectos parciales  

---

### 🧩 Contratos claros entre capas

- Execution → produce intención  
- Validation → produce decisión  
- Artifact → fija la verdad  
- Task → refleja el resultado final  

---

### ⚠️ Post-batch (estado real)

- Flujo parcialmente implementado  
- Tests existentes definen el contrato esperado  
- La implementación actual **no cumple completamente ese contrato**  
- Se ha decidido hacer rollback para:
  - evitar inconsistencias  
  - rediseñar con claridad  

---

## 🧪 Invariantes implementadas

### Ejecución
- No se ejecutan tareas no ejecutables  
- Run no se inicia en estados inválidos  

---

### Validación
- Coherencia entre:
  - `decision`  
  - `final_task_status`  
  - flags (`manual_review_required`, etc.)  
- Mismatch de validator detectado  

---

### Persistencia
- 1 run → 1 artifact de validación  
- Artifact consistente con task final  
- Task terminal ⇔ artifact canónico presente  

---

### Degradación
- Fallos en post-validación:
  - run = failed  
  - task = failed  
  - sin residuos inconsistentes  

---

### Reconciliación
- Sin commits implícitos ocultos  
- Rollback seguro ante fallo  
- No hay estados intermedios persistidos  

---

## 🧪 Cobertura de tests

Se han reforzado los tests en tres niveles:

### 1. Flujo vertical
- execution → validation → cierre  

### 2. Invariantes del sistema
- atomicidad del cierre  
- consistencia run/task/artifact  

### 3. Validación semántica
- incoherencias en `ValidationResult`  
- enforcement de contratos  

📌 Todos los tests del core (execution + validation) pasan en verde.

⚠️ Tests de `post_batch_service` actualmente:
- definen el contrato correcto  
- pero la implementación no está alineada  

---

## 🚀 Últimos avances

- Refactor completo del flujo execution + validation  
- Eliminación de legacy en validadores y tests  
- Introducción de invariantes explícitas del sistema  
- Corrección de consistencia transaccional  
- Aislamiento claro de responsabilidades  
- Reconciliación jerárquica sin efectos parciales  
- Tests de atomicidad y rollback  
- Primer diseño completo de **post-batch + recovery + plan mutation (WIP)**  

---

## 🧹 Limpieza pendiente

- Eliminar carpeta `workers` (no utilizada actualmente)  
- Revisar posibles restos menores de código muerto  
- Consolidar helpers de test duplicados  

---

## 🔭 Próximos pasos

### 🔥 Alta prioridad

#### 1. Rediseño de `post_batch_service`
Objetivo: alinear implementación con contrato real definido por tests.

Claves:
- Recovery debe ser **parte obligatoria del flujo**, no opcional  
- `created_recovery_task_ids` debe ser fuente de verdad  
- `recovery_context` debe construirse explícitamente  
- `evaluate_checkpoint` debe depender de recovery real  
- `assign/resequence/replan` deben ser mutuamente coherentes  

👉 Enfoque recomendado:
- Empezar desde tests (fuente de verdad)  
- Implementar paso a paso:
  1. recovery  
  2. evaluation  
  3. intent resolution  
  4. mutation  

---

#### 2. Revisar `request_adapter`
- Validar que el contexto que recibe el execution engine es:
  - completo  
  - coherente  
  - suficiente  

---

#### 3. End-to-end real
- Tests que cubran:
  - ejecución realista  
  - validación completa  
  - recovery  
  - post-batch  

---

### ⚙️ Media prioridad

#### 4. `run_command` hardening
- Mejor control de ejecución  
- Seguridad / sandboxing  
- Manejo robusto de errores  

---

#### 5. Revisión de artifacts
- Evitar duplicidades  
- Garantizar valor real  
- Simplificar trazabilidad  

---

### 🧩 Baja prioridad

#### 6. Refactor estructural
- Dividir servicios grandes si crecen demasiado  
- Mejorar organización modular  

---

#### 7. Configuración y portabilidad
- Storage  
- Runtime  
- Entornos  

---

## 🧠 Filosofía del sistema

Principio clave:

> **La fuente de verdad no es la ejecución, sino el resultado validado.**

Además:

- No hay estados implícitos  
- No hay efectos parciales persistidos  
- Todo cierre es verificable vía artifact  
- Los sistemas de recuperación deben ser **deterministas y auditables**

---

## 📌 Estado final

El core del sistema (execution + validation + cierre) está:

- Estable  
- Consistente  
- Testeado  
- Libre de efectos colaterales críticos  

El sistema de post-batch está:

- Bien diseñado a nivel de contrato (tests)  
- Pendiente de implementación correcta  

👉 El siguiente foco no es estabilidad, sino:

**hacer que el sistema sea capaz de continuar de forma inteligente tras fallos (recovery + planificación dinámica).**