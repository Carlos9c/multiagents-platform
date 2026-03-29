# 🧠 Agente de Ejecución y Validación de Tareas

## 📌 Resumen del proyecto

Este proyecto implementa un sistema de ejecución autónoma de tareas basado en agentes, con un foco fuerte en:

- Ejecución controlada de tareas atómicas
- Validación estructurada de resultados
- Persistencia consistente de artefactos
- Trazabilidad completa del flujo de trabajo
- Recuperación determinista ante fallos

El sistema orquesta el siguiente flujo:

Task → ExecutionRun → Execution → Validation → Artifact → Task closure → Hierarchy reconciliation

---

## 🧱 Componentes principales

### 1. Execution Engine
- Ejecuta tareas mediante agentes
- Produce `ExecutionResult` con:
  - evidencia (archivos, comandos, etc.)
  - resumen del trabajo
  - estado (`completed`, `partial`, `failed`, `rejected`)

### 2. Task Execution Service
- Orquesta todo el flujo:
  - creación de run
  - ejecución
  - validación
  - persistencia final
- Gestiona degradación ante errores
- Garantiza atomicidad del cierre

### 3. Validation Service
- Construye contexto de validación
- Decide el validador (router)
- Ejecuta validación (dispatcher)
- Devuelve `ValidationResult` estructurado

### 4. Artifact System
- Fuente de verdad del resultado validado
- `validation_result` = cierre canónico de la tarea

### 5. Task Hierarchy
- Consolida estado de tareas padre
- Propaga cambios de forma determinista
- Sin efectos colaterales parciales

---

## ✅ Estado actual del sistema

### 🔒 Consistencia transaccional
- Eliminados commits intermedios peligrosos
- Uso correcto de:
  - `flush()` durante construcción
  - `commit()` solo en puntos de frontera
  - `rollback()` en fallos

### 🔁 Atomicidad garantizada
El cierre de una tarea (validación + artifact + estado) es:
- Atómico
- Consistente
- Sin efectos parciales

---

## 🧪 Invariantes implementadas

### Ejecución
- No se ejecutan tareas no ejecutables
- Run no se inicia en estados inválidos

### Validación
- Coherencia entre:
  - `decision`
  - `final_task_status`
  - flags (`manual_review_required`, etc.)
- Mismatch de validator detectado

### Persistencia
- 1 run → 1 artifact de validación
- Artifact consistente con task final
- Task terminal ⇔ artifact canónico presente

### Degradación
- Fallos en post-validación:
  - run = failed  
  - task = failed  
  - sin residuos inconsistentes

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

Todos los tests actuales pasan en verde.

---

## 🚀 Últimos avances

- Refactor completo del flujo execution + validation
- Eliminación de legacy en validadores y tests
- Introducción de invariantes explícitas del sistema
- Corrección de consistencia transaccional
- Aislamiento claro de responsabilidades
- Reconciliación jerárquica sin efectos parciales
- Tests de atomicidad y rollback

---

## 🧹 Limpieza pendiente

- Eliminar carpeta `workers` (no utilizada actualmente)
- Revisar posibles restos menores de código muerto

---

## 🔭 Próximos pasos

### 🔥 Alta prioridad

#### 1. Revisar `request_adapter`
- Validar que el contexto que recibe el execution engine es:
  - completo
  - coherente
  - suficiente
- Evitar ejecuciones sobre contexto pobre

#### 2. End-to-end real
- Tests que cubran:
  - ejecución realista
  - validación completa
  - recovery
- Simular flujos completos de proyecto

---

### ⚙️ Media prioridad

#### 3. `run_command` hardening
- Mejor control de ejecución
- Seguridad / sandboxing
- Manejo robusto de errores

#### 4. Revisión de artifacts
- Evitar duplicidades
- Garantizar valor real
- Simplificar trazabilidad

---

### 🧩 Baja prioridad

#### 5. Refactor estructural
- Dividir servicios grandes si crecen demasiado
- Mejorar organización modular

#### 6. Configuración y portabilidad
- Storage
- Runtime
- Entornos

---

## 🧠 Filosofía del sistema

Principio clave:

**La fuente de verdad no es la ejecución, sino el resultado validado.**

Además:
- No hay estados implícitos
- No hay efectos parciales persistidos
- Todo cierre es verificable vía artifact

---

## 📌 Estado final

El core del sistema (execution + validation + cierre) está:

- Estable  
- Consistente  
- Testeado  
- Libre de efectos colaterales críticos  

El siguiente foco ya no es estabilidad, sino **calidad del contexto y del input operativo**.