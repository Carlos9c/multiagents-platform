import { useCallback, useEffect, useRef, useState } from 'react'

const PING_INTERVAL_MS = 25_000
const INITIAL_RECONNECT_MS = 1_500
const MAX_RECONNECT_MS = 12_000

export function useWebSocket(projectId, onMessage) {
  const [status, setStatus] = useState('disconnected')
  const wsRef = useRef(null)
  const reconnectDelayRef = useRef(INITIAL_RECONNECT_MS)
  const reconnectTimerRef = useRef(null)
  const pingTimerRef = useRef(null)
  const mountedRef = useRef(true)
  const onMessageRef = useRef(onMessage)

  useEffect(() => { onMessageRef.current = onMessage }, [onMessage])

  const cleanup = useCallback(() => {
    clearInterval(pingTimerRef.current)
    clearTimeout(reconnectTimerRef.current)
    if (wsRef.current) {
      wsRef.current.onclose = null
      wsRef.current.close()
      wsRef.current = null
    }
  }, [])

  const connect = useCallback(() => {
    if (!projectId || !mountedRef.current) return
    cleanup()

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host
    const url = `${protocol}//${host}/ws/projects/${projectId}/chat`

    setStatus('connecting')
    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen = () => {
      if (!mountedRef.current) return
      setStatus('connected')
      reconnectDelayRef.current = INITIAL_RECONNECT_MS
      pingTimerRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }))
        }
      }, PING_INTERVAL_MS)
    }

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (msg.type !== 'pong') onMessageRef.current?.(msg)
      } catch {}
    }

    ws.onerror = () => ws.close()

    ws.onclose = () => {
      if (!mountedRef.current) return
      clearInterval(pingTimerRef.current)
      setStatus('disconnected')
      const delay = reconnectDelayRef.current
      reconnectDelayRef.current = Math.min(delay * 1.6, MAX_RECONNECT_MS)
      reconnectTimerRef.current = setTimeout(connect, delay)
    }
  }, [projectId, cleanup])

  useEffect(() => {
    mountedRef.current = true
    if (projectId) connect()
    return () => {
      mountedRef.current = false
      cleanup()
    }
  }, [projectId, connect, cleanup])

  const send = useCallback((msg) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg))
      return true
    }
    return false
  }, [])

  return { status, send }
}
