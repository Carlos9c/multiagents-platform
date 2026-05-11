import { useState } from 'react'
import { api } from '../api.js'

export function ProjectSelector({ onSelectProject, onNewProject, onEvolutionProject }) {
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState(null)

  // Nuevo proyecto desde cero
  const handleNew = async () => {
    setCreating(true)
    setCreateError(null)
    try {
      const project = await api.createProject('Nuevo Proyecto')
      onNewProject(project.id)
    } catch (e) {
      setCreateError(e.message)
      setCreating(false)
    }
  }

  // Proyecto externo: sin project_id en BD → nueva conversación con Aria, source_path preestablecida
  const [externalPath, setExternalPath] = useState('')
  const [importing, setImporting] = useState(false)
  const [importError, setImportError] = useState(null)

  const handleImportExternal = async () => {
    if (!externalPath.trim()) {
      setImportError('Introduce la ruta del proyecto externo.')
      return
    }
    setImporting(true)
    setImportError(null)
    try {
      const project = await api.createProject('Nuevo Proyecto')
      onNewProject(project.id, externalPath.trim())
    } catch (e) {
      setImportError(e.message)
      setImporting(false)
    }
  }

  // Evolutivo: project_id ya en BD → nueva conversación con Aria, misma project_id
  const [evoId, setEvoId] = useState('')
  const [evoPath, setEvoPath] = useState('')
  const [evoManualUpdate, setEvoManualUpdate] = useState(true)
  const [evoError, setEvoError] = useState(null)

  const handleEvolution = () => {
    if (!evoId.trim()) {
      setEvoError('Introduce el ID del proyecto.')
      return
    }
    const id = parseInt(evoId.trim(), 10)
    if (isNaN(id) || id <= 0) {
      setEvoError('ID de proyecto inválido.')
      return
    }
    setEvoError(null)
    onEvolutionProject(id, evoPath.trim(), evoManualUpdate)
  }

  return (
    <div className="selector">
      <div className="selector-hero">
        <div className="selector-logo">⚡</div>
        <h1 className="selector-title">Agente Desarrollador</h1>
        <p className="selector-subtitle">Desarrollo autónomo con Aria</p>
      </div>

      <div className="selector-content">
        {/* — Nuevo proyecto — */}
        <div className="selector-actions">
          <button className="btn btn-primary" onClick={handleNew} disabled={creating}>
            {creating ? <><span className="spinner" /> Creando…</> : '+ Nuevo Proyecto'}
          </button>
          {createError && <span style={{ fontSize: 13, color: 'var(--red)' }}>{createError}</span>}
        </div>

        {/* — Proyecto externo (sin ID en BD) — */}
        <div className="selector-card">
          <p className="selector-section-title">Proyecto externo (primera vez)</p>
          <p className="selector-hint">
            Código que no fue creado con este sistema. Aria analizará el proyecto y te preguntará
            qué cambios quieres hacer.
          </p>
          <div className="continue-form">
            <input
              type="text"
              className="input"
              placeholder="C:\ruta\al\proyecto\existente"
              value={externalPath}
              onChange={e => { setExternalPath(e.target.value); setImportError(null) }}
            />
            <button
              className="btn btn-secondary"
              onClick={handleImportExternal}
              disabled={importing || !externalPath.trim()}
            >
              {importing ? <><span className="spinner" /> Importando…</> : '↗ Abrir con Aria'}
            </button>
          </div>
          {importError && <span className="form-error">{importError}</span>}
        </div>

        {/* — Evolutivo (project_id ya en BD) — */}
        <div className="selector-card">
          <p className="selector-section-title">Evolutivo (proyecto ya registrado)</p>
          <p className="selector-hint">
            Genera un nuevo ciclo de desarrollo sobre un proyecto existente. Aria conversará
            contigo para definir qué se quiere hacer en esta iteración.
          </p>
          <div className="continue-form">
            <input
              type="number"
              className="input input-short"
              placeholder="ID (ej. 76)"
              value={evoId}
              onChange={e => { setEvoId(e.target.value); setEvoError(null) }}
              min="1"
            />
            <input
              type="text"
              className="input"
              placeholder="C:\ruta\al\proyecto (si cambió de ubicación)"
              value={evoPath}
              onChange={e => { setEvoPath(e.target.value); setEvoError(null) }}
            />
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={evoManualUpdate}
                onChange={e => setEvoManualUpdate(e.target.checked)}
              />
              Re-analizar el código fuente
            </label>
            <button
              className="btn btn-secondary"
              onClick={handleEvolution}
              disabled={!evoId.trim()}
            >
              ↻ Iniciar evolutivo
            </button>
          </div>
          {evoError && <span className="form-error">{evoError}</span>}
        </div>

        {/* — Abrir proyecto existente por ID — */}
        <div className="selector-card">
          <p className="selector-section-title">Abrir proyecto por ID</p>
          <p className="selector-hint">
            Retoma la conversación activa de un proyecto ya en curso.
          </p>
          <OpenByIdForm onSelectProject={onSelectProject} />
        </div>
      </div>
    </div>
  )
}

function OpenByIdForm({ onSelectProject }) {
  const [id, setId] = useState('')
  const [error, setError] = useState(null)

  const handleOpen = () => {
    const parsed = parseInt(id.trim(), 10)
    if (!id.trim() || isNaN(parsed) || parsed <= 0) {
      setError('Introduce un ID válido.')
      return
    }
    setError(null)
    onSelectProject(parsed)
  }

  return (
    <>
      <div className="continue-form">
        <input
          type="number"
          className="input input-short"
          placeholder="ID (ej. 76)"
          value={id}
          onChange={e => { setId(e.target.value); setError(null) }}
          min="1"
        />
        <button
          className="btn btn-secondary"
          onClick={handleOpen}
          disabled={!id.trim()}
        >
          ▶ Abrir
        </button>
      </div>
      {error && <span className="form-error">{error}</span>}
    </>
  )
}
