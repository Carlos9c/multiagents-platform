# Execution Engine Documentation

## Purpose

The `execution_engine` module is the new runtime layer responsible for **operational resolution** of an already-atomic task.

Its job is not to plan the product, not to rewrite the task, and not to validate the final business outcome. Its job is to:

- receive one atomic task plus execution context
- iterate intelligently using LLM-backed decisions
- choose which subagent should act next
- gather context and evidence
- create or modify artifacts inside the workspace
- optionally run commands for operational evidence
- return a structured `ExecutionResult` for the external validator

This module is intentionally placed **between** task generation/refinement and final validation.

---

## Design principles

### 1. The task is immutable inside the engine
The orchestrator and subagents **must not change**:

- task title
- task description
- task objective
- task scope

They may only:

- resolve the task operationally
- reject it if no safe route exists
- return evidence and a structured outcome

### 2. Validation remains outside the engine
The engine can perform local checks, but the final task validation still belongs to `task_validation_service`.

### 3. The orchestrator is intelligent, but bounded
The orchestrator can reason with an LLM, but it is constrained by:

- structured next-action outputs
- step budgets
- explicit subagent registry
- structured state

### 4. Domain-specific work must not pollute the generic runtime
The engine core should remain reusable for future domains such as:

- code
- documentation
- images
- slides
- spreadsheets

The current implementation is still code-first, but the correct long-term separation is:

- generic engine core
- domain-specific subagents and operation plans

### 5. Prefer signals over blocking guardrails
Only hard-block when there is:

- structural inconsistency
- invalid contracts
- unsafe filesystem behavior
- impossible routing

For everything else, prefer:

- risk flags
- trace notes
- additional iteration

---

## High-level flow

1. A task reaches `task_execution_service`
2. The service builds an `ExecutionRequest`
3. The selected execution backend is resolved through `execution_engine.factory`
4. The `ExecutionOrchestrator` starts its loop
5. The orchestrator asks the runtime for the **next action**
6. A subagent is selected from the registry
7. The subagent executes its bounded operation
8. `ResolutionState` is updated
9. The orchestrator either:
   - continues
   - finishes
   - rejects
   - fails
10. The engine returns an `ExecutionResult`
11. The external validator decides the final task outcome

---

## Current module structure

Below is the current logical structure discussed and implemented for the execution engine.

```text
app/execution_engine/
├── __init__.py
├── agent_runtime/
│   ├── __init__.py
│   ├── base.py
│   └── structured_llm_runtime.py
├── base.py
├── budget.py
├── context_selection.py
├── contracts.py
├── execution_plan.py
├── factory.py
├── file_operations.py
├── monitoring.py
├── next_action.py
├── orchestrator.py
├── request_adapter.py
├── resolution_state.py
├── state.py
├── engines/
│   ├── __init__.py
│   ├── legacy_local_engine.py
│   └── orchestrated_engine.py
├── subagents/
│   ├── __init__.py
│   ├── base.py
│   ├── code_change_agent.py
│   ├── command_runner_agent.py
│   ├── context_selection_agent.py
│   ├── placement_resolver_agent.py
│   └── repo_inspector_agent.py
└── tools/
    ├── __init__.py
    ├── command_tool.py
    ├── context_builder_tool.py
    ├── file_reader_tool.py
    ├── file_snapshot_tool.py
    ├── file_writer_tool.py
    ├── repo_tree_tool.py
    └── workspace_scan_tool.py
```

---

## Core contracts

### `contracts.py`
This file defines the public boundary of the module.

Main models:

- `ExecutionRequest`
- `ExecutionResult`
- `ExecutionEvidence`
- `ChangedFile`
- `CommandExecution`
- `ProjectExecutionContext`
- `RelatedTaskSummary`

These are the core contracts that the rest of the application should depend on.

### `ExecutionRequest`
Represents the task and the operational context provided to the engine.

Key ideas:

- task identity
- project identity
- run identity
- immutable task content
- context paths
- constraints
- prior related task summaries

### `ExecutionResult`
Represents the engine’s operational outcome.

It is not the final validator verdict. It is the engine’s own structured result.

Typical decisions:

- `completed`
- `partial`
- `failed`
- `rejected`

### `ExecutionEvidence`
The main evidence bucket produced by the engine.

Current evidence includes:

- changed files
- executed commands
- notes
- artifact references

Long term, this should probably become a more generic evidence model so that non-code executors fit naturally.

---

## Runtime layer

### `agent_runtime/base.py`
Defines the runtime abstraction used by LLM-backed subagents and by the orchestrator.

Main interface:

- `BaseAgentRuntime.generate_structured(...)`

This is deliberately provider-agnostic.

### `agent_runtime/structured_llm_runtime.py`
Uses the current `app.services.llm` abstraction as the first runtime implementation.

Why this matters:

- avoids hard lock-in at the engine boundary
- keeps runtime swappable later
- allows future replacements such as another provider or a framework-backed runtime

---

## Orchestrator loop

### `orchestrator.py`
This is the most important file in the module.

The orchestrator is responsible for:

- running the intelligent loop
- asking the runtime for the next action
- choosing the next subagent
- updating state
- tracking risk flags
- recording trace events
- deciding when to finish or reject

### Current action types
Defined in `next_action.py`.

Current actions:

- `inspect_context`
- `resolve_file_operations`
- `apply_file_operations`
- `run_command`
- `finish`
- `reject`

### Current orchestrator state inputs
The next-action prompt currently includes things like:

- whether repo summary exists
- whether context has been selected
- planned file operations status
- pending/applied/failed operations
- changed files
- executed commands
- risk flags
- notes gathered so far

### Monitoring
The orchestrator also writes trace events using `monitoring.py`.

Tracked event types include:

- `orchestrator_started`
- `next_action_decided`
- `subagent_selected`
- `subagent_completed`
- `subagent_rejected_step`
- `subagent_unexpected_error`
- `orchestrator_finished`
- `orchestrator_budget_exceeded`

At the moment traces are appended into `ExecutionEvidence.notes`.

This is good enough for initial observability, but it should probably evolve into a first-class persisted trace later.

---

## State handling

### `state.py`
Contains the low-level counters for execution loop budgets.

Used to track:

- step count
- agent call count
- tool call count
- command run count
- repair attempts

### `resolution_state.py`
Contains the operational state that evolves across iterations.

This is the engine’s working memory.

Current tracked information includes:

- repo summary
- candidate paths
- selected paths
- selected file context
- planned file operations
- pending operation paths
- applied operation paths
- failed operation paths
- risk flags
- step notes
- orchestrator trace
- execution evidence

### Why `ResolutionState` matters
This is the contract that allows the orchestrator to be iterative instead of stateless.

Without this file, the engine would collapse into a sequence of isolated prompts with no reliable memory.

---

## File operation planning

### `file_operations.py`
Defines the current code-domain operation plan.

Main models:

- `FileOperation`
- `FileOperationPlan`
- `MaterializedFile`
- `FileMaterializationResult`

### Why this exists
It provides structure between:

- placement reasoning
- file materialization

Instead of asking the LLM to “just edit the repo,” we split it into:

1. decide what files should be touched
2. generate the actual file contents

### Current `FileOperation` capabilities
A file operation can currently describe:

- operation type (`create`, `modify`)
- relative path
- reason and purpose
- category (`source`, `test`, `config`, `integration`, `docs`)
- sequence
- dependencies on prior paths
- integration notes
- edit mode
- expected symbols

### Multi-file support
Yes, multi-file tasks are supported.

The plan is a list of operations, ordered by:

- `sequence`
- then category
- then path

That means a single atomic task can legitimately require:

- creating a new module
- wiring it into an existing entry point
- adding tests

### Important limitation
Even though multi-file plans are supported, the current system is still code-centric. The abstraction should eventually generalize into a domain-neutral artifact-operation layer.

---

## Subagents

### `subagents/base.py`
Defines the base subagent interface.

Every subagent must provide:

- `name`
- `supports_step_kind(step_kind)`
- `execute_step(request, step, state)`

The important design rule is that a subagent should do **one bounded thing**, not orchestrate the full task.

### Current subagents

#### `context_selection_agent.py`
LLM-backed.

Purpose:

- choose the smallest useful repository context
- identify relevant files
- build selected file context

#### `placement_resolver_agent.py`
LLM-backed.

Purpose:

- decide which files should be created or modified
- produce a `FileOperationPlan`
- annotate risks and integration notes

#### `code_change_agent.py`
LLM-backed.

Purpose:

- materialize the pending file operation plan
- generate full file contents
- write files in ordered fashion
- snapshot and rollback on failure
- update applied/pending/failed operation tracking

#### `command_runner_agent.py`
Deterministic.

Purpose:

- run operational commands
- collect command evidence

#### `repo_inspector_agent.py`
Deterministic.

Purpose:

- inspect the repository tree
- collect candidate directories

Note: this agent is still present, but the loop currently leans heavily on `context_selection_agent` for the first meaningful context action. This overlap should be reviewed later.

---

## Tools

The `tools/` directory contains deterministic filesystem and command helpers.

Current tools:

- `repo_tree_tool.py`
- `workspace_scan_tool.py`
- `context_builder_tool.py`
- `file_reader_tool.py`
- `file_writer_tool.py`
- `file_snapshot_tool.py`
- `command_tool.py`

### Important rule for tools
A tool should:

- perform deterministic side effects or reads
- not contain semantic planning logic
- not reinterpret the task
- not become a hidden subagent

The LLM should decide **what** to do. The tool should do **how** to do it deterministically.

---

## Backends

### `engines/legacy_local_engine.py`
Compatibility wrapper around the existing legacy local code executor.

Purpose:

- preserve compatibility with the previous flow
- translate legacy executor results into `ExecutionResult`

### `engines/orchestrated_engine.py`
The new orchestrated backend.

Purpose:

- instantiate runtime
- register subagents
- run the orchestrator loop

### `factory.py`
Selects the backend depending on configuration.

At the moment supported values are:

- `legacy_local`
- `orchestrated`

---

## Integration with the existing system

### `request_adapter.py`
Builds an `ExecutionRequest` from the existing `Task` model and workspace/storage services.

This is one of the most important integration points because it isolates the engine from the rest of the application’s persistence and task models.

### `task_execution_service.py`
This service is currently the main caller of the engine.

Responsibilities at this boundary:

- ensure the task is executable
- create execution runs
- call the engine
- translate engine results into validator-compatible execution artifacts
- preserve the existing workflow contracts

This means the engine has been introduced without forcing a full rewrite of the outer execution flow.

---

## Testing status

A focused test suite was created for the current engine behavior.

Main tested areas:

- `FileOperationPlan` ordering
- `ResolutionState` tracking of pending/applied/failed operations
- `CodeChangeAgent` successful multi-file application
- rollback behavior when a write fails
- orchestrator trace generation
- `finish` behavior with and without pending operations

### What these tests prove
They prove that the current structure is not just conceptual. The main mechanics are now behaving consistently.

### What they do not yet prove
They do not yet prove:

- real quality of LLM reasoning
- end-to-end resolution quality on real tasks
- true integration robustness under repeated workloads
- concurrency safety
- trace persistence

---

## Mandatory section: how to add a new subagent

This section is intentionally exhaustive because this is one of the most important operational concerns for the future of the module.

### Goal
A new subagent should be added when a bounded responsibility appears that should not be absorbed into the orchestrator or into an existing subagent.

Examples:

- `repair_agent`
- `document_draft_agent`
- `slides_builder_agent`
- `image_generation_agent`

### Before creating a new subagent, answer these questions

1. Is this responsibility really separate from the existing subagents?
2. Is it tactical rather than strategic?
3. Does it need its own prompt/runtime interaction?
4. Does it need deterministic tools that are not already present?
5. Is it domain-specific or engine-generic?

If the answer is “this is really just another orchestrator decision,” then do **not** create a subagent.

### Minimum files to touch when adding a new subagent

#### 1. Create the subagent file
Path:

```text
app/execution_engine/subagents/<new_subagent>.py
```

The class must implement:

- `name`
- `supports_step_kind(...)`
- `execute_step(...)`

#### 2. Export it from subagents init
Path:

```text
app/execution_engine/subagents/__init__.py
```

Add the import and include it in `__all__`.

#### 3. Register it in the orchestrated backend
Path:

```text
app/execution_engine/engines/orchestrated_engine.py
```

Instantiate the subagent and add it to `SubagentRegistry(...)`.

#### 4. Add or reuse a step kind / action mapping if needed
Potential files:

- `app/execution_engine/next_action.py`
- `app/execution_engine/execution_plan.py`
- `app/execution_engine/orchestrator.py`

You need to decide:

- does the new subagent correspond to a new next action?
- does the orchestrator need to map that action into a step kind?
- does the runtime prompt need to know that this action exists?

#### 5. Add required tools if the subagent needs deterministic helpers
Path:

```text
app/execution_engine/tools/<new_tool>.py
```

And export it from:

```text
app/execution_engine/tools/__init__.py
```

#### 6. Add tests
Prefer adding tests to the current focused test file unless there is a strong reason to split.

Recommended test coverage:

- subagent supports the expected step kind
- valid structured output updates state correctly
- invalid structured output is rejected properly
- side effects are deterministic and recoverable where relevant

### What a new subagent should not do
A subagent should not:

- change task semantics
- decide global workflow transitions
- persist external task state
- perform final validation
- silently create its own execution loop
- call unrelated subagents internally without explicit design

### What a good new subagent should do
A good subagent should:

- have one clear operational responsibility
- accept structured state
- return updated structured state
- use deterministic tools for real side effects
- be easy to trace and test

---

## Mandatory section: how to add a new tool

### When a new tool is appropriate
Add a tool when you need deterministic behavior such as:

- filesystem access
- command execution
- local parsing
- artifact writing
- snapshot/rollback
- format-specific materialization

A tool should not be used to hide LLM reasoning.

### Minimum files to touch when adding a new tool

#### 1. Create the tool file
Path:

```text
app/execution_engine/tools/<new_tool>.py
```

#### 2. Export the tool
Path:

```text
app/execution_engine/tools/__init__.py
```

#### 3. Import it where needed
Usually in one of:

- a subagent file
- a helper module
- possibly future domain-specific operation handlers

#### 4. Add tests
Test deterministic behavior directly.

Examples:

- safe path enforcement
- rollback correctness
- snapshot correctness
- command output capture

### What to consider before adding a tool

1. Does this tool have side effects?
2. Does it need path safety checks?
3. Can it be rolled back if partially applied?
4. Does it belong in the generic engine or in a future domain namespace?
5. Is this really a tool, or is it actually subagent reasoning that should live in an LLM-backed component?

---

## What to consider before adding new capabilities

This module is already dense. Before adding anything, check whether the change belongs to:

- the orchestrator
- a subagent
- a tool
- the runtime
- the integration boundary outside the engine

### Use this rule of thumb

#### Put it in the orchestrator if:
- it decides what to do next
- it manages iteration
- it manages bounded high-level strategy

#### Put it in a subagent if:
- it performs one tactical operation
- it needs a dedicated prompt or tool set

#### Put it in a tool if:
- it performs deterministic filesystem/command work

#### Keep it outside the engine if:
- it is final validation
- it is workflow ownership
- it is persistence of official task state
- it is planner/refiner semantics

---

## Current weaknesses and caveats

### 1. Still too code-shaped
The engine core is more generic than before, but many concepts are still code-biased:

- file operations
- repo context
- command execution
- source/test integration

This is acceptable short-term, but should evolve.

### 2. Trace persistence is provisional
Trace events currently end up in `ExecutionEvidence.notes`.
That is useful, but not yet a real observability solution.

### 3. Repo inspection overlap
`repo_inspector_agent` and `context_selection_agent` overlap conceptually.
This should be simplified later.

### 4. Full-file materialization is still blunt
`CodeChangeAgent` currently works with full file content.
This is fine to start, but a more nuanced patching strategy may eventually be needed.

### 5. Multi-domain abstraction is not finished
The long-term engine should become artifact-oriented rather than file-operation-oriented.

---

## Recommended next steps

These are the recommended evolution steps for the module.

### Short-term next steps

#### 1. Run real end-to-end executions
The current tests validate mechanics, but now real tasks should be executed through the orchestrated backend to observe:

- action choices
- context quality
- placement quality
- code generation quality
- loop behavior

#### 2. Improve trace consumption
The monitoring trace should become easier to inspect, possibly with:

- structured persistence on `ExecutionRun`
- dedicated trace field or artifact
- UI/log-friendly formatting

#### 3. Allow finer-grained partial application
`CodeChangeAgent` already applies the pending subset, but the orchestrator still lacks truly fine-grained route selection for smaller operation groups. That should be improved carefully, without overcomplicating the module.

### Medium-term next steps

#### 4. Split generic engine core from code-domain behavior
A cleaner separation should emerge between:

- engine core
- code-domain subagents and operation plans

A likely future structure is:

```text
app/execution_engine/
app/execution_domains/code/
```

#### 5. Redesign operations around artifacts, not only files
To support future executors for:

- images
- documents
- slides
- spreadsheets

we should evolve toward something closer to:

- `ArtifactOperationPlan`
- `ArtifactMaterializationResult`

with file operations as one specific implementation.

#### 6. Add repair iteration
A dedicated repair agent should eventually exist so that the orchestrator can react better to failed command evidence or bad integration outcomes.

### Longer-term next steps

#### 7. Persist orchestrator traces formally
Instead of burying them in notes, they should become first-class execution telemetry.

#### 8. Introduce better context retrieval
The current context selection is a strong start, but it should improve further using:

- smarter file selection
- symbol-level selection
- better dependency awareness
- stronger historical task context

#### 9. Revisit final validation boundary
For now validation stays outside the engine, which is correct. Later, some internal assessment mechanisms may be strengthened without collapsing the boundary.

#### 10. Multi-domain executors
Once the engine stabilizes for code, new domain-specific subagents can be introduced for:

- documentation generation
- image generation
- slide generation
- spreadsheet creation

But this should happen only after the engine core is proven stable enough.

---

## Recommended operating rule going forward

Do not keep adding structure speculatively.

The current module is already rich enough to justify a new discipline:

1. test it
2. run real tasks
3. inspect traces
4. identify the actual bottleneck
5. evolve only the part that is truly limiting behavior

That is the best way to prevent the execution engine from turning into an overdesigned system that is hard to reason about and even harder to trust.
