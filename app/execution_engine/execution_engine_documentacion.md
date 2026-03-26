# 🧠 Execution Engine – Documentación Técnica

## 📌 Visión General

El **Execution Engine** es el núcleo operativo del sistema multiagente. Su responsabilidad es:

> Ejecutar tareas atómicas sobre un workspace real de forma controlada, observable y validable.

El engine no “piensa en abstracto”: **opera sobre archivos, comandos y evidencia real**.

---

## 🏗️ Arquitectura

El execution engine está compuesto por:

- **Orchestrator** → decide el siguiente paso
- **Subagentes** → ejecutan acciones concretas
- **Tools** → primitivas de bajo nivel
- **State & Evidence** → seguimiento del proceso

Flujo simplificado:

1. Se recibe un `ExecutionRequest`
2. El **orchestrator** decide una acción
3. Se selecciona un **subagente**
4. El subagente usa **tools**
5. Se acumula **evidence**
6. Se repite hasta `finish` o `reject`

---

## ⚙️ Acciones del Orchestrator

- `inspect_context`
- `resolve_file_operations`
- `apply_file_operations`
- `run_command`
- `finish`
- `reject`

---

## 🤖 Subagentes

### context_selection_agent
Selecciona contexto relevante del repo.

### placement_resolver_agent
Define qué archivos crear/modificar.

### code_change_agent
Materializa cambios en archivos.

### command_runner_agent
Ejecuta un comando concreto.

---

## 🧰 Tools

Ejemplos:

- `read_text_file`
- `write_text_file`
- `capture_file_snapshot`
- `restore_file_snapshot`
- `run_command`

---

## 🔒 Filosofía de diseño

El engine sigue estas reglas:

- Capacidad explícita (no implícita)
- Acciones pequeñas y controladas
- Evidencia observable siempre
- No ejecutar lógica arbitraria
- No depender de comportamiento humano

---

# ➕ Cómo añadir nuevas Tools o Subagentes

Esta es la parte crítica del sistema. Si se hace mal, el engine se degrada rápidamente.

---

## 🧩 1. Añadir una nueva Tool

### Paso 1: Definir la tool

Ubicación:
```
app/execution_engine/tools/
```

Ejemplo base:

```python
def my_tool(...):
    # lógica pura, sin LLM
    return resultado
```

### Reglas obligatorias

- ❌ No usar LLM dentro de una tool
- ❌ No tener efectos laterales ocultos
- ✅ Debe ser determinista
- ✅ Debe devolver output estructurado
- ✅ Debe ser segura (paths, inputs, etc.)

---

### Paso 2: Integrarla en capabilities.py

Añadir:

```python
ToolCapability(
    name="my_tool",
    purpose="Qué hace",
    notes=["limitaciones", "uso correcto"],
)
```

⚠️ Si no está en capabilities:
👉 el orquestador **no sabrá que existe**

---

### Paso 3: Usarla desde un subagente

Nunca se llaman tools directamente desde el orchestrator.

---

## 🤖 2. Añadir un nuevo Subagente

### Paso 1: Crear el subagente

Ubicación:
```
app/execution_engine/subagents/
```

Debe heredar de `BaseSubagent`.

Ejemplo:

```python
class MyAgent(BaseSubagent):
    name = "my_agent"

    def supports_step_kind(self, step_kind: str) -> bool:
        return step_kind == "my_step"

    def execute_step(...):
        # lógica
        return state
```

---

### Paso 2: Definir su contrato

Debe tener:

- `step_kind` claro
- input/output bien definido
- uso explícito de tools

---

### Paso 3: Registrar en el engine

En el registry del engine:

```python
registry.register(MyAgent(...))
```

⚠️ Si no está registrado:
👉 el orchestrator no puede usarlo

---

### Paso 4: Añadir a capabilities.py

```python
SubagentCapability(
    name="my_agent",
    role="Qué hace",
    step_kinds=["my_step"],
    uses_tools=["my_tool"],
    strengths=[...],
    limits=[...],
)
```

---

### Paso 5: Añadir routing en orchestrator

```python
ACTION_MY_ACTION: ("my_agent", "my_step")
```

⚠️ Si no hay mapping:
👉 el subagente nunca se ejecuta

---

## 🚨 Errores comunes (y graves)

### ❌ Subagente no enrutable
Está registrado pero no mapeado → código muerto

### ❌ Tool no declarada en capabilities
Existe pero el LLM no la ve → no se usa

### ❌ Tool demasiado poderosa
Ejemplo: shell libre → comportamiento caótico

### ❌ Subagente que decide demasiado
Debe ejecutar, no tomar decisiones globales

---

## 🧠 Regla de oro

> Si el orquestador no puede ver explícitamente una capacidad, esa capacidad no existe.

---

## 📈 Buenas prácticas

- Añadir una capacidad = actualizar:
  - código
  - capabilities
  - prompt
- Mantener cada acción pequeña
- Preferir composición de pasos simples
- Evitar “agentes mágicos”

---

## 🧪 Testing

Cada nueva tool o subagente debe tener:

- test unitario
- test integrado con el engine
- test de edge cases

---

## 🧭 Conclusión

El execution engine no es un sistema genérico de agentes:

👉 es un sistema **operativo y determinista con LLM como planner**

Cuanto más explícitas y acotadas sean las capacidades:
- mejor planifica
- mejor ejecuta
- menos se rompe

