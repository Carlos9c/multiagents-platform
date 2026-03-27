# 🚀 Multiagent Platform – Backend

## 🧠 Visión

Este proyecto implementa una **plataforma backend multiagente** cuyo objetivo es:

> Transformar una idea de proyecto en un sistema ejecutable de forma progresiva, estructurada y autónoma.

El sistema orquesta planificación, ejecución, evaluación y recuperación mediante un pipeline iterativo basado en **batches y checkpoints**, con trazabilidad completa y decisiones deterministas.

---

# 🏗️ Estado actual del proyecto

## ✅ Núcleo del sistema (COMPLETADO)

El sistema end-to-end está completamente operativo:

- ✔️ Creación de proyectos  
- ✔️ Generación de execution plan  
- ✔️ Ejecución por batches  
- ✔️ Evaluación por checkpoints  
- ✔️ Recovery automático de tareas fallidas  
- ✔️ Decisión determinista post-batch  
- ✔️ Iteración del workflow hasta cierre o bloqueo  
- ✔️ Persistencia completa mediante artifacts  

---

# 🔁 Pipeline de ejecución

El flujo actual es:

1. **Planner** genera `ExecutionPlan`  
2. Se ejecuta un **batch**  
3. Se recogen artifacts generados  
4. Se ejecuta:
   - `evaluation_service`  
   - `recovery_service` (si aplica)  
5. `post_batch_service`:
   - normaliza señales  
   - resuelve acción final (determinista)  
6. `project_workflow_service`:
   - continúa / resecuencia / replanifica / detiene  

---

# 🧩 Arquitectura consolidada

## 🔹 Separación de responsabilidades (CRÍTICO)

| Componente | Responsabilidad |
|----------|----------------|
| Planner | Genera plan inicial |
| Executor | Ejecuta tareas |
| Recovery | Actúa localmente sobre fallos |
| Evaluator | Emite señales (NO decide flujo) |
| PostBatchDecisionService | Decide acción final |
| WorkflowService | Orquesta iteraciones |

---

## 🔹 Decisión determinista post-batch (ESTABILIZADO)

Se ha eliminado lógica implícita dispersa.

La decisión final sigue:

if structural_replan:
    → replan
elif blocking_gap:
    → resequence
else:
    → continue

✔️ Recovery **NO implica replan automáticamente**  
✔️ Continuidad es el default  
✔️ Resequence solo si hay bloqueo real  
✔️ Replan solo si hay ruptura estructural  

---

## 🔹 Patch batches (NUEVO)

Se han introducido **batches intermedios (1.1, 1.2, …)**:

- Permiten insertar trabajo nuevo sin replanificar  
- Mantienen `plan_version` estable  
- Evitan romper el flujo  
- Eliminan soluciones arbitrarias  

---

## 🔹 Identidad de plan y batches (RESUELTO)

- `plan_version` determinista  
- `batch_internal_id` estable  
- naming normalizado:  
  Plan {version} · Batch {index}  

✔️ Se elimina completamente el problema de “volver a batch 1”  

---

## 🔹 Observabilidad completa (RESUELTO)

Se persisten artifacts estructurados:

- `execution_plan`  
- `evaluation_decision`  
- `post_batch_result`  
- `recovery_decisions`  
- `workflow_iteration_trace`  

✔️ Permite reconstrucción completa del sistema  
✔️ Auditoría desde BBDD sin endpoints adicionales  

---

## 🔹 Control de loops (CRÍTICO)

- Protección contra reprocesado de batches  
- Control explícito de `current_index`  
- Manejo correcto de planes parcheados  

✔️ Se evita loop infinito en workflow  

---

# 🧪 Testing

- ✔️ Tests de servicios principales  
- ✔️ Tests de decisiones post-batch  
- ✔️ Tests de recovery  
- ✔️ Tests de workflow  
- ✔️ Compatibilidad con mocks legacy (`SimpleNamespace`)  

Estado: **Todos los tests en verde**

---

# ⚠️ Problemas resueltos clave

Antes:
- Replanificaciones innecesarias  
- Recovery provocaba caos estructural  
- Batches sin identidad  
- Falta de trazabilidad  
- Decisiones implícitas y frágiles  

Ahora:
- Decisión determinista centralizada  
- Recovery desacoplado del flujo global  
- Plan estable con patching local  
- Observabilidad completa  
- Sistema auditable y predecible  

---

# 🔜 Siguientes pasos (priorizados correctamente)

## 🔥 BLOQUE 4 — Recovery (PRIORIDAD ACTUAL)

El sistema ya decide bien.  
Ahora hay que mejorar **la calidad del input (recovery)**.

### 4.1 Validar `last_execution_agent_sequence`

- ¿Se está usando realmente?  
- ¿Mejora decisiones?  
- ¿Introduce ruido?  

### 4.2 Refinar prompt de recovery

Diferenciar correctamente:

- scoping_problem → falta trabajo  
- execution_path_problem → mal enfoque  
- additive_gap → falta puntual  

### 4.3 (Opcional) Test de trajectory

- parsing correcto  
- fallback robusto  
- integración en prompt  

---

## 🧠 BLOQUE 5 — Evaluator (POSTERIOR)

Solo tras estabilizar recovery.

Objetivo:

- NO replan si backlog cubre trabajo  
- recovery ≠ fallo estructural  
- preferir continuar  

MUY importante:

- añadir ejemplos negativos  
- evitar sobre-reacción del modelo  

---

## 🧹 BLOQUE 6 — Limpieza (DEUDA TÉCNICA)

- eliminar legacy restante  
- consolidar nombres  
- eliminar helpers duplicados  
- limpiar executor antiguo  

---

# 🧭 Conclusión

El sistema ha pasado de:

pipeline frágil, no determinista y difícil de depurar  

a:

sistema robusto, trazable, determinista y extensible  

Estado real:

- 🟢 Decisión → RESUELTA  
- 🟢 Identidad → RESUELTA  
- 🟢 Observabilidad → RESUELTA  
- 🟡 Recovery → SIGUIENTE FOCO  
- ⚪ Evaluator → POSTERIOR  
- ⚪ Limpieza → FINAL  

---

# 💡 Nota final

El sistema ya no necesita más complejidad.

Ahora necesita:

mejor señal de entrada (recovery) + ajuste fino del evaluator  

No tocar la orquestación salvo que sea estrictamente necesario.