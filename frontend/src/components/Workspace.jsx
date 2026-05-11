import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useWebSocket } from '../hooks/useWebSocket.js'
import { ChatPanel } from './ChatPanel.jsx'
import { ProjectPanel } from './ProjectPanel.jsx'

export function Workspace({ projectId, initialSourcePath = '', initialManualUpdate = true, onBack }) {
  const [conversationId, setConversationId] = useState(null)
  const [projectName, setProjectName] = useState('')
  const [messages, setMessages] = useState([])
  const [phase, setPhase] = useState('gathering_requirements')
  const [description, setDescription] = useState('')
  const [lastAriaDraft, setLastAriaDraft] = useState(null)
  const [descDirty, setDescDirty] = useState(false)
  const [tasks, setTasks] = useState([])
  const [sourcePath, setSourcePath] = useState(initialSourcePath)
  const [manualUpdate, setManualUpdate] = useState(initialManualUpdate)
  const [isStarting, setIsStarting] = useState(false)
  const [startError, setStartError] = useState(null)
  const [readyToStart, setReadyToStart] = useState(false) // Aria says requirements are ready

  const descDirtyRef = useRef(false)

  const loadTasks = useCallback(async () => {
    try {
      const ts = await api.getProjectTasks(projectId)
      setTasks(ts)
    } catch {}
  }, [projectId])

  // Cargar tareas al entrar en fases de ejecución
  useEffect(() => {
    if (phase === 'executing' || phase === 'awaiting_review' || phase === 'completed') {
      loadTasks()
    }
  }, [phase, loadTasks])

  // Polling de tareas durante la ejecución (cada 10s)
  useEffect(() => {
    if (phase !== 'executing') return
    const interval = setInterval(loadTasks, 10000)
    return () => clearInterval(interval)
  }, [phase, loadTasks])

  // Cargar datos iniciales del proyecto
  useEffect(() => {
    api.getProject(projectId).then(p => {
      setProjectName(p.name === 'New Project' ? '' : (p.name || ''))
      if (p.description) setDescription(p.description)
    }).catch(() => {})
  }, [projectId])

  const handleWsMessage = useCallback((msg) => {
    switch (msg.type) {
      case 'conversation_state': {
        if (msg.conversation_id) setConversationId(msg.conversation_id)
        setMessages(msg.messages || [])
        if (msg.phase) setPhase(msg.phase)
        if (msg.phase === 'ready_to_start') setReadyToStart(true)
        if (msg.requirements_draft) {
          setLastAriaDraft(msg.requirements_draft)
          if (!descDirtyRef.current) {
            setDescription(msg.requirements_draft)
          }
        }
        break
      }
      case 'assistant_message': {
        setMessages(prev => [...prev, { role: 'assistant', content: msg.content }])
        if (msg.phase) setPhase(msg.phase)
        if (msg.requirements_draft) {
          setLastAriaDraft(msg.requirements_draft)
          if (!descDirtyRef.current) {
            setDescription(msg.requirements_draft)
          }
        }
        if (msg.event === 'requirements_ready') {
          setReadyToStart(true)
        }
        if (msg.event === 'project_started') {
          setReadyToStart(false)
          loadTasks()
        }
        if (msg.event === 'project_completed') {
          setPhase('completed')
        }
        break
      }
      case 'review_required': {
        setMessages(prev => [
          ...prev,
          { role: 'assistant', content: msg.content, task_id: msg.task_id },
        ])
        setPhase('awaiting_review')
        break
      }
      case 'execution_event': {
        setTasks(prev =>
          prev.map(t => (t.id === msg.task_id ? { ...t, status: msg.status } : t))
        )
        break
      }
    }
  }, [loadTasks])

  const { status: wsStatus, send } = useWebSocket(projectId, handleWsMessage)

  const sendMessage = useCallback((content) => {
    send({ type: 'user_message', content })
    setMessages(prev => [...prev, { role: 'user', content }])
  }, [send])

  const handleStart = async () => {
    if (!projectName.trim()) {
      setStartError('Introduce un nombre para el proyecto.')
      return
    }
    if (!conversationId) {
      setStartError('Esperando conexión con Aria...')
      return
    }
    setIsStarting(true)
    setStartError(null)
    try {
      await api.updateProject(projectId, { name: projectName, description })
      await api.confirmStart(projectId, {
        conversation_id: conversationId,
        name: projectName,
        description,
        source_path: sourcePath.trim() || undefined,
        manual_update: manualUpdate,
      })
      // La transición de fase llega por WebSocket (assistant_message con event=project_started)
    } catch (e) {
      setStartError(e.message)
    } finally {
      setIsStarting(false)
    }
  }

  const isExecuting = phase === 'executing' || phase === 'completed'
  const canStart = (
    (phase === 'gathering_requirements' || phase === 'ready_to_start') &&
    !!projectName.trim() &&
    !isStarting
  )
  const canClear = phase === 'gathering_requirements' || phase === 'ready_to_start'

  const handleDescriptionChange = (val) => {
    setDescription(val)
    descDirtyRef.current = true
    setDescDirty(true)
  }

  const handleSyncDraft = () => {
    if (lastAriaDraft) {
      setDescription(lastAriaDraft)
      descDirtyRef.current = false
      setDescDirty(false)
    }
  }

  return (
    <div className="workspace">
      <header className="topbar">
        <div className="topbar-brand">
          <span className="topbar-icon">⚡</span>
          <span className="topbar-title">Agente Desarrollador</span>
          {projectName && (
            <>
              <span className="topbar-separator">›</span>
              <span className="topbar-project-name">{projectName}</span>
            </>
          )}
        </div>

        <div className={`ws-status ${wsStatus}`}>
          <span className="ws-dot" />
          {wsStatus === 'connected' ? 'Conectado' : wsStatus === 'connecting' ? 'Conectando…' : 'Sin conexión'}
        </div>

        <button
          className="btn btn-ghost btn-sm"
          onClick={onBack}
          disabled={!canClear}
          title={canClear ? 'Volver al selector de proyectos' : 'No se puede cancelar mientras el proyecto está en curso'}
        >
          ✕ Cancelar
        </button>

        <button
          className={`btn btn-sm ${readyToStart ? 'btn-ready' : 'btn-primary'}`}
          onClick={handleStart}
          disabled={!canStart}
          title={canStart ? 'Iniciar ejecución con el nombre y descripción actuales' : 'La ejecución ya está en curso'}
        >
          {isStarting
            ? <><span className="spinner" /> Iniciando…</>
            : readyToStart
              ? '✓ Confirmar inicio'
              : '▶ Iniciar'}
        </button>
      </header>

      <div className="workspace-main">
        <ChatPanel
          messages={messages}
          phase={phase}
          wsStatus={wsStatus}
          onSendMessage={sendMessage}
        />
        <ProjectPanel
          projectName={projectName}
          onProjectNameChange={setProjectName}
          description={description}
          onDescriptionChange={handleDescriptionChange}
          lastAriaDraft={lastAriaDraft}
          descDirty={descDirty}
          onSyncDraft={handleSyncDraft}
          sourcePath={sourcePath}
          onSourcePathChange={setSourcePath}
          manualUpdate={manualUpdate}
          onManualUpdateChange={setManualUpdate}
          tasks={tasks}
          phase={phase}
          startError={startError}
        />
      </div>
    </div>
  )
}
