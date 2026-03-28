# Validation System

## Overview

El sistema de validación está diseñado para cumplir tres principios:

1. **Entrada común**

   * Todos los validadores reciben un `TaskValidationInput`.

2. **Salida común**

   * Todos los validadores devuelven un `ValidationResult`.

3. **Consumo especializado**

   * Cada validador transforma internamente ese `TaskValidationInput` en la representación más adecuada para su proceso de validación.
   * Esa transformación interna actúa también como guardrail de formatos y capacidades.

El flujo general es:

```text
ExecutionResult + task context + execution artifacts/files
    ↓
build_task_validation_input(...)
    ↓
TaskValidationInput
    ↓
resolve_validation_route(...)
    ↓
ResolvedValidationIntent
    ↓
validator-specific rendering / consumption
    ↓
validator LLM output
    ↓
ValidationResult
```

## Current architecture

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

## Main concepts

### TaskValidationInput

Es el contrato de entrada común a todos los validadores.

Contiene:

* `intent`
* `task`
* `execution`
* `request_context`
* `evidence_package`
* `metadata`

Este contrato **no depende del tipo de validador**.

### ValidationResult

Es el contrato de salida común a todos los validadores.

Contiene, entre otros:

* `decision`
* `summary`
* `validated_scope`
* `missing_scope`
* `findings`
* `validated_evidence_ids`
* `unconsumed_evidence_ids`
* `followup_validation_required`
* `recommended_next_validator_keys`

Esto deja el sistema preparado para un futuro orquestador de validación multi-validador.

### ValidationEvidencePackage

Contiene la evidencia estructurada producida por la ejecución.

La unidad básica es `ValidationEvidenceItem`.

Cada item describe una evidencia en términos universales:

* `evidence_id`
* `evidence_kind`
* `media_type`
* `representation_kind`
* `source`
* `content_text`
* `content_summary`
* `structured_content`
* `metadata`

Esto permite que el paquete de validación sea escalable a distintos formatos.

## Validation routing

El router de validación decide **qué validador** debe procesar la tarea.

No valida la tarea en sí.

Su trabajo es:

* leer el contexto de la tarea
* leer el resumen de ejecución
* leer el resumen de evidencia
* escoger `validator_key`
* escoger `discipline`
* escoger `validation_mode`

Actualmente el router devuelve una `ValidationRoutingDecision` con schema estricto.

## Validator responsibilities

Cada validador debe hacer tres cosas:

1. **Declarar capacidades**

   * qué formatos y tipos de evidencia puede consumir

2. **Renderizar su evidencia**

   * transformar el `TaskValidationInput` al formato más eficiente para su modelo

3. **Validar**

   * producir una salida especializada del LLM
   * mapearla a `ValidationResult`

### Important design rule

El contrato de entrada es común, pero **la forma de consumirlo no es común**.

Ejemplo:

* un validador textual puede imprimir ficheros de texto directamente en prompt
* un validador multimodal de imágenes puede necesitar pasar otra representación
* un validador futuro de audio puede requerir transcripción o una representación específica

## Code validator flow

El validador de código sigue este patrón:

```text
TaskValidationInput
    ↓
capabilities.py
    ↓
renderer.py
    ↓
CodeValidationRenderableEvidence
    ↓
prompt.py
    ↓
LLM
    ↓
CodeValidationLLMOutput
    ↓
ValidationResult
```

### capabilities.py

Declara qué evidencia puede consumir el validador de código.

Ejemplo:

* `produced_file`
* `command_output`
* `persisted_artifact`
* `artifact_reference`

Y solo para representaciones compatibles con consumo textual.

### renderer.py

Selecciona la evidencia que el validador sí soporta y la renderiza en el formato más legible para el modelo.

También separa:

* `supported_items`
* `unsupported_items`

Eso actúa como guardrail de modalidad.

### prompt.py

No recibe la estructura cruda del package “tal cual”.
Recibe una representación renderizada y optimizada para ese validador.

## How to add a new validator

Ejemplo: `image_task_validator`

### 1. Crear la carpeta del validador

```text
app/services/validation/validators/image/
├── __init__.py
├── capabilities.py
├── prompt.py
├── renderer.py
├── schemas.py
└── service.py
```

### 2. Definir capacidades

En `capabilities.py` declara qué evidencia puede consumir.

Por ejemplo:

* `generated_image`
* `persisted_artifact`
* `artifact_reference`

Y media types como:

* `image/png`
* `image/jpeg`

### 3. Definir schema del output del LLM

En `schemas.py` define una salida especializada, por ejemplo:

* `ImageValidationLLMOutput`

Ese schema es interno al validador.

### 4. Implementar el renderer

En `renderer.py` transforma el `TaskValidationInput` al formato más adecuado para ese validador.

Importante:

* el renderer debe ignorar o marcar como no consumida la evidencia que no soporta
* no debe romper el contrato común

### 5. Implementar el servicio del validador

En `service.py`:

* recibe `TaskValidationInput`
* aplica capacidades
* renderiza
* llama al LLM con schema estricto
* convierte el resultado especializado a `ValidationResult`

### 6. Exponer el validador

En `validators/image/__init__.py` exporta el servicio principal.

### 7. Registrar en el dispatcher

En `app/services/validation/dispatcher.py` añade el nuevo `validator_key`.

Ejemplo:

```python
if intent.validator_key == "image_task_validator":
    return validate_image_task_with_llm(validation_input=validation_input)
```

### 8. Registrar en el router

En `app/services/validation/router/registry.py` añade una entrada al catálogo del router con:

* `validator_key`
* `discipline`
* descripción
* deliverables típicos
* evidencia típica

Esto permite al router decidir cuándo debe enrutar a ese validador.

### 9. Añadir tests

Cada validador nuevo debería tener, como mínimo:

* test de capacidades
* test de renderer
* test de service
* test vertical de ejecución → validación si aplica

## Multi-validator future

El sistema está diseñado para que, en el futuro, un orquestador de validación pueda encadenar validadores.

Ejemplo:

* un validador textual consume los ficheros de texto
* deja `unconsumed_evidence_ids` con una imagen
* marca `followup_validation_required=True`
* recomienda `image_task_validator`

Luego el orquestador podrá derivar esa misma tarea a otro validador con:

* la descripción de la tarea
* el resultado de la validación previa
* la evidencia aún pendiente

## Current scope

Actualmente el sistema soporta de forma real:

* construcción de `TaskValidationInput`
* routing de validación
* validador de código
* integración ejecución → validación para tareas completadas o parciales

Las tareas fallidas o rechazadas por ejecución no pasan por validación:

* van directamente a recovery

## Design rules

* No introducir contratos de entrada específicos por validador.
* No mezclar el contrato común con la representación específica del prompt.
* No hacer auto-discovery mágico de validadores.
* No usar builders LLM.
* No reintroducir semántica legacy de `CodeExecutor` o `task_validation_service`.

## Recommended next steps

1. Preparar el futuro orquestador multi-validador
