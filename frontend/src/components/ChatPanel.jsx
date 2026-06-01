import { useEffect, useRef, useState } from 'react'

const PHASE_LABELS = {
  gathering_requirements: 'Recogiendo requisitos',
  ready_to_start: 'Listo para iniciar',
  executing: 'Ejecutando',
  awaiting_review: 'Revisión pendiente',
  paused: 'Pausado',
  completed: 'Completado',
}

const INPUT_PLACEHOLDERS = {
  gathering_requirements: 'Cuéntale a Aria sobre tu proyecto… (Enter para enviar, Shift+Enter para nueva línea)',
  ready_to_start: 'Responde "sí" para confirmar, o edita la descripción de la derecha…',
  executing: 'Pregunta a Aria sobre el estado del proyecto…',
  awaiting_review: 'Responde a Aria sobre la tarea bloqueada…',
  paused: 'Pregunta a Aria sobre el estado del proyecto…',
  completed: 'Pregunta a Aria sobre el proyecto completado…',
}

export function ChatPanel({ messages, phase, wsStatus, onSendMessage }) {
  const [input, setInput] = useState('')
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const inputDisabled = false

  const handleSubmit = (e) => {
    e.preventDefault()
    const text = input.trim()
    if (!text || inputDisabled) return
    onSendMessage(text)
    setInput('')
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit(e)
    }
  }

  const phaseLabel = PHASE_LABELS[phase] || phase
  const placeholder = INPUT_PLACEHOLDERS[phase] || 'Escribe un mensaje…'

  return (
    <div className="chat-panel">
      <div className="chat-header">
        <div className="aria-indicator">
          <div className="aria-avatar">A</div>
          <span className="aria-name">Aria</span>
          <span className={`aria-status ${wsStatus}`} title={wsStatus} />
        </div>
        <span className={`phase-badge ${phase}`}>{phaseLabel}</span>
      </div>

      {phase === 'executing' && (
        <div className="executing-banner">
          <span className="executing-spinner" />
          <span>Proyecto en ejecución — puedes preguntarle a Aria sobre el estado</span>
        </div>
      )}

      {phase === 'awaiting_review' && (
        <div className="review-banner">
          <span>⚠</span>
          <span>Una tarea necesita tu atención — responde a Aria para resolverla</span>
        </div>
      )}

      {phase === 'ready_to_start' && (
        <div className="ready-banner">
          <span>✓</span>
          <span>Aria tiene suficiente información. Revisa la descripción y confirma cuando estés listo.</span>
        </div>
      )}

      <div className="chat-messages">
        {messages.length === 0 && (
          <div style={{ color: 'var(--text-muted)', fontSize: 13, textAlign: 'center', marginTop: 32 }}>
            Conectando con Aria…
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`message ${msg.role}`}>
            {msg.role === 'assistant' && <div className="message-avatar">A</div>}
            <div className="message-bubble">{msg.content}</div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <form className="chat-input-area" onSubmit={handleSubmit}>
        <textarea
          className="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          disabled={inputDisabled}
          rows={2}
        />
        <button
          className="send-btn"
          type="submit"
          disabled={!input.trim() || inputDisabled}
          title="Enviar (Enter)"
        >
          ↑
        </button>
      </form>
    </div>
  )
}
