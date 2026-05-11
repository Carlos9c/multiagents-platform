async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try { detail = (await res.json()).detail || detail } catch {}
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  listProjects: () =>
    request('/projects'),

  getProject: (id) =>
    request(`/projects/${id}`),

  createProject: (name = 'New Project') =>
    request('/projects', { method: 'POST', body: { name, description: '' } }),

  updateProject: (id, data) =>
    request(`/projects/${id}`, { method: 'PATCH', body: data }),

  startProject: (payload) =>
    request('/projects/start', { method: 'POST', body: payload }),

  getProjectTasks: (id) =>
    request(`/projects/${id}/tasks`),

  getActiveConversation: (id) =>
    request(`/projects/${id}/conversations/active`),

  confirmStart: (projectId, payload) =>
    request(`/projects/${projectId}/conversations/confirm-start`, {
      method: 'POST',
      body: payload,
    }),
}
