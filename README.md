# 🧠 Agente Desarrollador — Estado actual del proyecto

## 📍 Estado real tras esta fase

En esta fase se ha consolidado una parte importante del sistema y, sobre todo, se ha hecho una limpieza estructural para alinear código, tests y semántica interna.

Los cambios más relevantes han sido:

* unificación progresiva del vocabulario interno
* limpieza de ayudas y compatibilidades legacy en tests
* cierre del puente semántico entre ejecución y validación
* sustitución del flujo viejo de validación por un subsistema nuevo basado en:

  * routing
  * evidence package
  * validadores especializados
* integración del nuevo flujo en `task_execution_service`
* eliminación de dependencias principales de:

  * `code_executor`
  * `code_validator`
  * `task_validation_service`
  * contratos legacy asociados

La idea central que queda fijada es esta:

> la ejecución produce evidencia real
> y la validación razona sobre esa evidencia con contratos comunes de entrada y salida

---

# ✅ Qué se ha hecho realmente

## 1. Limpieza de semántica en tests y fixtures

Se ha trabajado de forma explícita en que la suite deje de “arreglar” payloads o aceptar vocabulario heredado de forma silenciosa.

Esto ha implicado:

* revisar `conftest.py`
* corregir factories y ayudas de test
* reescribir tests para usar payloads canónicos reales
* eliminar ayudas que seguían pensando en vocabulario viejo
* adaptar tests de servicios clave a la semántica nueva

Se han tocado y alineado, entre otros:

* `test_evaluation_service.py`
* `test_post_batch_service.py`
* `test_project_workflow_service.py`
* `test_live_plan_mutation_service.py`
* `test_recovery_assignment_compiler_service.py`
* `test_execution_plan_patch_service.py`
* `test_execution_plan_service.py`
* `test_task_execution_service.py`

Esto era imprescindible para que la suite validara la migración de verdad y no una versión maquillada de la misma.

---

## 2. Cierre de la doble semántica del executor

Se ha avanzado en cerrar la transición de `executor_type` y en sacar compatibilidad que ya no debía seguir viva.

La dirección marcada es:

* contratos canónicos reales
* sin normalizaciones silenciosas
* sin mantener vocabulario viejo por comodidad
* sin seguir ampliando semántica duplicada

Esto afecta directamente a:

* modelos
* schemas
* servicios
* fixtures
* tests

---

## 3. Nuevo sistema de validación

Se ha introducido un subsistema nuevo de validación que reemplaza el enfoque anterior acoplado a `CodeExecutor`.

La base del nuevo modelo es:

### Entrada común

* `TaskValidationInput`

### Salida común

* `ValidationResult`

### Comportamiento interno especializado

Cada validador transforma internamente el `TaskValidationInput` al formato que más le conviene para validar.

Eso permite:

* mantener contratos comunes del sistema
* especializar el consumo por tipo de validador
* preparar el terreno para validadores futuros de otros formatos

---

# ⚙️ Flujo actual ejecución → validación

## 1. Ejecución

```text
Task → execution_engine → ExecutionResult
```

El `execution_engine` produce:

* `ExecutionResult`
* `changed_files`
* comandos ejecutados
* `output_snapshot`
* `validation_notes`
* `blockers_found`
* `execution_agent_sequence`
* references a artifacts si aplica

---

## 2. Construcción del paquete de validación

```text
ExecutionResult + task context + execution context
    ↓
build_task_validation_input(...)
    ↓
TaskValidationInput
```

Este `TaskValidationInput` contiene:

* contexto de la tarea
* contexto de ejecución
* contexto de request
* `evidence_package`
* metadata

---

## 3. Evidence package

La validación ya no se basa en “snapshots legacy” ni en contratos del viejo validador.

Ahora se trabaja con un paquete de evidencia estructurado.

Ese paquete puede contener items como:

* ficheros producidos/modificados
* outputs de comandos
* artifacts persistidos
* referencias a artifacts
* metadata asociada

La unidad básica es `ValidationEvidenceItem`.

La idea importante es:

> el contrato es común
> pero cada validador decide cómo consumir internamente esa evidencia

---

## 4. Routing de validación

```text
TaskValidationInput
    ↓
resolve_validation_route(...)
    ↓
ResolvedValidationIntent
```

El router decide:

* `validator_key`
* disciplina
* modo de validación

Bajo estas reglas:

* schema estricto
* catálogo de validadores conocido
* fallback seguro

El router no valida la tarea.
Solo decide a qué validador debe ir.

---

## 5. Validación especializada

```text
TaskValidationInput
    ↓
validator-specific consumption
    ↓
LLM output especializado
    ↓
ValidationResult
```

La clave de esta arquitectura es que:

* lo que entra al sistema de validación siempre tiene el mismo shape
* lo que sale del sistema de validación siempre tiene el mismo shape
* lo que pasa por dentro depende del tipo de validación

En el caso actual del validador de código:

* se filtra qué evidencia puede consumir
* se renderiza en un formato textual útil para el LLM
* se obtiene una salida especializada
* se transforma a `ValidationResult`

---

## 6. Aplicación del resultado en ejecución

`task_execution_service.py` ya no valida con el flujo viejo.

Ahora:

* si ejecución termina en `completed` o `partial`

  * pasa por el nuevo sistema de validación
* si ejecución termina en `failed` o `rejected`

  * no se valida
  * va directamente a recovery

Eso elimina la necesidad de seguir manteniendo el viejo puente `executor → validator`.

---

# 🧩 Estructura real de validación

La estructura a reflejar en README debe ser la real, no una idealizada. A día de hoy, la parte nueva gira alrededor de:

```text
app/services/validation/
├── __init__.py
├── contracts.py
├── dispatcher.py
├── service.py
├── evidence/
│   ├── __init__.py
│   └── package_builder.py
├── router/
│   ├── __init__.py
│   ├── prompt.py
│   ├── registry.py
│   ├── schemas.py
│   └── service.py
└── validators/
    ├── __init__.py
    └── code/
        ├── __init__.py
        ├── capabilities.py
        ├── prompt.py
        ├── renderer.py
        ├── schemas.py
        └── service.py
```

---

# 🧪 Estado de los tests

La suite ha quedado alineada con la semántica nueva y ya no depende del sistema viejo para pasar.

Además, se han añadido tests de validación y tests verticales del flujo ejecución → validación.

Actualmente hay cobertura sobre:

* router de validación
* registro/catálogo del router
* builder del `TaskValidationInput`
* validador de código
* `validation/service.py`
* integración de `task_execution_service.py` con el nuevo flujo
* tests verticales del camino ejecución → validación

Esto deja el sistema en una posición mucho más fiable que antes.

---

# 🧠 Principios que quedan fijados

## 1. Contratos comunes

Siempre:

* entrada: `TaskValidationInput`
* salida: `ValidationResult`

## 2. Consumo especializado

Cada validador decide:

* qué evidencia puede consumir
* cómo la representa para su modelo
* qué deja fuera y reporta como no consumido

## 3. Guardrails por validador

Los validadores no deben consumir cualquier formato indiscriminadamente.

Esto es importante porque:

* evita errores de modalidad
* prepara el futuro encadenamiento entre validadores
* deja trazabilidad sobre evidencia consumida vs no consumida

## 4. Evidence-first

La validación razona sobre:

* evidencia real producida por ejecución
* no sobre adaptadores legacy
* no sobre contratos puente del sistema viejo

---

# 🚀 Siguientes pasos propuestos

## 🔴 Prioridad 1 — cerrar limpieza restante del executor y schemas asociados

Queda rematar restos pequeños en:

* `executor.py`
* `code_execution.py`

El objetivo es cerrar por completo cualquier dependencia semántica sobrante de esta transición.

---

## 🔴 Prioridad 2 — endurecer aún más la suite

Aunque la limpieza principal ya se ha hecho, conviene seguir reforzando:

* factories estrictamente canónicas
* cero auto-normalización silenciosa
* eliminación total de vocabulario viejo en tests
* helpers explícitos si alguna compatibilidad temporal sigue existiendo

Esto sigue siendo prioritario, porque una suite permisiva vuelve a esconder deuda.

---

## 🔴 Prioridad 3 — mejorar `request_adapter`

Ahora mismo el adapter sigue infraalimentando al engine.

Debería enriquecerse con:

* contexto de runs previos
* artifacts relevantes recientes
* señales útiles del proyecto
* mejor selección de archivos candidatos
* mejor selección de related tasks

Esto no es un refactor estético. Es una mejora real de capacidad del sistema.

---

## 🟡 Prioridad 4 — preparar el camino para validadores adicionales

El sistema ya está diseñado para crecer hacia:

* validadores de imágenes
* validadores documentales
* otros dominios futuros

Pero antes de abrir esa fase hay que mantener firme la base actual:

* contratos comunes
* capabilities por validador
* consumo especializado de evidencia
* `ValidationResult` preparado para validación parcial y continuación futura

---

## 🟡 Prioridad 5 — evolucionar hacia orquestación multi-validador

Todavía no se ha implementado el orquestador completo, pero el sistema ya apunta en esa dirección.

La idea futura sería:

* un validador consume la parte de evidencia que soporta
* reporta lo no consumido
* el orquestador deriva el resto a otro validador
* compone el resultado final de validación

---

## 🔵 Prioridad 6 — partir servicios grandes

Cuando se cierre esta fase de limpieza, sigue siendo recomendable atacar:

* `post_batch_service`
* `project_workflow_service`

No por estética, sino por robustez y control.

---

# ✅ Resumen honesto

A estas alturas:

* la migración semántica va mucho mejor encaminada
* la suite ya no oculta tanto legacy como antes
* el sistema nuevo de validación existe de verdad
* `task_execution_service` ya usa el flujo nuevo en el camino principal
* las piezas viejas principales de validación se han podido retirar

Lo que queda no es rehacer la base, sino:

* cerrar restos
* endurecer contratos y tests
* enriquecer contexto de ejecución
* preparar la siguiente fase con cimientos sólidos
