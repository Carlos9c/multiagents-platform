import { useState } from 'react'

const STATUS_LABELS = {
  pending: 'Pendiente',
  running: 'Ejecutando',
  awaiting_validation: 'Validando',
  awaiting_review: 'Revisión',
  partial: 'Parcial',
  completed: 'Completado',
  failed: 'Fallido',
  reatomized: 'Reatomizado',
  followed_up: 'Continuado',
  superseded: 'Reemplazado',
  cancelled: 'Cancelado',
}

function TaskRow({ task, indented }) {
  return (
    <div className={`task-row status-${task.status} ${indented ? 'indented' : ''}`}>
      <span className={`status-dot ${task.status}`} />
      {task.sequence_order != null && (
        <span className="task-seq">#{task.sequence_order}</span>
      )}
      <span className="task-title" title={task.title}>{task.title}</span>
      <span className={`status-badge ${task.status}`}>
        {STATUS_LABELS[task.status] || task.status}
      </span>
    </div>
  )
}

function TaskGroup({ parent, tasks }) {
  const [collapsed, setCollapsed] = useState(false)
  return (
    <div className="task-group">
      {parent && (
        <div className="task-group-header" onClick={() => setCollapsed(c => !c)}>
          <span className="task-group-chevron">{collapsed ? '▸' : '▾'}</span>
          <span className="task-group-name" title={parent.title}>{parent.title}</span>
          <span className={`status-dot ${parent.status}`} />
        </div>
      )}
      {!collapsed && tasks.map(t => (
        <TaskRow key={t.id} task={t} indented={!!parent} />
      ))}
    </div>
  )
}

function TaskTree({ tasks }) {
  const atomic = tasks.filter(t => t.planning_level === 'atomic')
  const parentMap = Object.fromEntries(
    tasks.filter(t => t.planning_level !== 'atomic').map(t => [t.id, t])
  )

  const groups = {}
  atomic.forEach(t => {
    const key = t.parent_task_id ?? '__root__'
    if (!groups[key]) groups[key] = []
    groups[key].push(t)
  })

  Object.values(groups).forEach(g =>
    g.sort((a, b) => (a.sequence_order ?? 999) - (b.sequence_order ?? 999))
  )

  const groupEntries = Object.entries(groups)

  if (groupEntries.length === 0) {
    return (
      <div className="text-muted" style={{ fontSize: 13, padding: '8px 0' }}>
        Sin tareas todavía.
      </div>
    )
  }

  return (
    <div className="task-tree">
      {groupEntries.map(([key, groupTasks]) => (
        <TaskGroup
          key={key}
          parent={key !== '__root__' ? parentMap[key] : null}
          tasks={groupTasks}
        />
      ))}
    </div>
  )
}

export function ProjectPanel({
  projectName,
  onProjectNameChange,
  description,
  onDescriptionChange,
  lastAriaDraft,
  descDirty,
  onSyncDraft,
  sourcePath,
  onSourcePathChange,
  manualUpdate,
  onManualUpdateChange,
  tasks,
  phase,
  startError,
}) {
  const [showOptions, setShowOptions] = useState(false)
  const isExecuting = phase === 'executing' || phase === 'awaiting_review' || phase === 'completed'
  const doneCount = tasks.filter(t => t.status === 'completed').length
  const totalAtomic = tasks.filter(t => t.planning_level === 'atomic').length

  return (
    <div className="project-panel">
      {/* Nombre del proyecto */}
      <div className="project-section">
        <label className="section-label">Nombre del proyecto</label>
        <input
          className="project-name-input"
          value={projectName}
          onChange={e => onProjectNameChange(e.target.value)}
          placeholder="Escribe el nombre del proyecto…"
          disabled={isExecuting}
        />
      </div>

      {/* Descripción / Requisitos */}
      <div className="project-section">
        <div className="section-header">
          <label className="section-label" style={{ marginBottom: 0 }}>Descripción</label>
          {descDirty && lastAriaDraft && !isExecuting && (
            <button className="sync-btn" onClick={onSyncDraft} title="Restaurar la versión de Aria">
              ↩ Restaurar versión de Aria
            </button>
          )}
        </div>
        <textarea
          className="description-textarea"
          value={description}
          onChange={e => onDescriptionChange(e.target.value)}
          placeholder="Aria irá rellenando esto a medida que conversen. También puedes editarlo directamente."
          disabled={isExecuting}
        />
      </div>

      {/* Opciones (solo antes de iniciar) */}
      {!isExecuting && (
        <div className="project-section">
          <button className="options-toggle" onClick={() => setShowOptions(s => !s)}>
            {showOptions ? '▾' : '▸'}&nbsp; Opciones
          </button>
          {showOptions && (
            <div className="options-content">
              <label className="option-label">
                Directorio fuente (opcional)
                <input
                  className="source-input"
                  value={sourcePath}
                  onChange={e => onSourcePathChange(e.target.value)}
                  placeholder="C:\ruta\al\proyecto\existente"
                />
                <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
                  Solo necesario si quieres importar código existente desde el disco.
                </span>
              </label>
              {sourcePath && (
                <label className="checkbox-label" style={{ marginTop: 8 }}>
                  <input
                    type="checkbox"
                    checked={manualUpdate}
                    onChange={e => onManualUpdateChange(e.target.checked)}
                  />
                  Re-analizar el código fuente
                  <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 4 }}>
                    (desactiva si el análisis ya está guardado)
                  </span>
                </label>
              )}
            </div>
          )}
        </div>
      )}

      {/* Error de inicio */}
      {startError && <div className="error-banner">{startError}</div>}

      {/* Árbol de tareas */}
      {(isExecuting || tasks.length > 0) && (
        <div className="project-section tasks-section">
          <div className="section-header" style={{ marginBottom: 10 }}>
            <span className="section-label" style={{ marginBottom: 0 }}>Tareas</span>
            {totalAtomic > 0 && (
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                {doneCount} / {totalAtomic} completadas
              </span>
            )}
          </div>
          <TaskTree tasks={tasks} />
        </div>
      )}
    </div>
  )
}
