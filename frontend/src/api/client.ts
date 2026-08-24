import type {
  ActivityEvent,
  AgentRun,
  ArchiveOperation,
  CheckResult,
  HumanInput,
  Project,
  ProjectCockpit,
  ProviderSettings,
  Task,
  TaskReview,
  TaskSpec,
  WorkspaceState,
} from '../types'

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      detail?: string | { code?: string; message?: string }
    } | null
    const detail = body?.detail
    const message = typeof detail === 'string'
      ? detail
      : detail?.message
        ? `${detail.message}${detail.code ? ` (${detail.code})` : ''}`
        : `Request failed (${response.status})`
    throw new ApiError(message, response.status)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<{ status: string; database: string }>('/api/health'),
  projects: () => request<Project[]>('/api/projects'),
  createProject: (payload: {
    name: string
    repository_path: string
    environment_spec: string
    module_mapping: Array<{ name: string; paths: string[] }>
    verification_commands?: Project['verification_commands']
  }) => request<Project>('/api/projects', { method: 'POST', body: JSON.stringify(payload) }),
  updateProject: (id: string, payload: Partial<Project>) =>
    request<Project>(`/api/projects/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteProject: (id: string) =>
    request<void>(`/api/projects/${id}`, { method: 'DELETE' }),
  modulePreview: (id: string, filePath: string) =>
    request<{ file_path: string; modules: string[] }>(`/api/projects/${id}/module-preview`, {
      method: 'POST',
      body: JSON.stringify({ file_path: filePath }),
    }),
  discoverVerification: (id: string) =>
    request<Project>(`/api/projects/${id}/verification/discover`, { method: 'POST' }),
  tasks: (projectId: string) => request<Task[]>(`/api/projects/${projectId}/tasks`),
  cockpit: (projectId: string) => request<ProjectCockpit>(`/api/projects/${projectId}/cockpit`),
  task: (id: string) => request<Task>(`/api/tasks/${id}`),
  createTask: (projectId: string, payload: { title: string; raw_request: string }) =>
    request<Task>(`/api/projects/${projectId}/tasks`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  refineTask: (id: string) => request<Task>(`/api/tasks/${id}/refine`, { method: 'POST' }),
  answerTask: (id: string, answers: Array<{ question_id: string; answer: string }>) =>
    request<Task>(`/api/tasks/${id}/answers`, {
      method: 'POST',
      body: JSON.stringify({ answers }),
    }),
  updateSpec: (id: string, spec: TaskSpec) =>
    request<Task>(`/api/tasks/${id}/spec`, {
      method: 'PUT',
      body: JSON.stringify({ spec }),
    }),
  confirmTask: (id: string) => request<Task>(`/api/tasks/${id}/confirm`, { method: 'POST' }),
  prepareTask: (id: string) => request<Task>(`/api/tasks/${id}/prepare`, { method: 'POST' }),
  runTask: (id: string) => request<AgentRun>(`/api/tasks/${id}/run`, { method: 'POST' }),
  runs: (id: string) => request<AgentRun[]>(`/api/tasks/${id}/runs`),
  events: (id: string) => request<ActivityEvent[]>(`/api/tasks/${id}/events`),
  workspace: (id: string) => request<WorkspaceState>(`/api/tasks/${id}/workspace`),
  checks: (id: string) => request<CheckResult[]>(`/api/tasks/${id}/checks`),
  inputs: (id: string) => request<HumanInput[]>(`/api/tasks/${id}/inputs`),
  review: (id: string) => request<TaskReview>(`/api/tasks/${id}/review`),
  cancelRun: (id: string) => request<AgentRun>(`/api/runs/${id}/cancel`, { method: 'POST' }),
  answerInput: (id: string, answer: string) =>
    request<HumanInput>(`/api/tasks/${id}/input`, {
      method: 'POST',
      body: JSON.stringify({ answer }),
    }),
  resumeTask: (id: string, instruction?: string) =>
    request<AgentRun>(`/api/tasks/${id}/resume`, {
      method: 'POST',
      body: JSON.stringify({ instruction: instruction || null }),
    }),
  rejectReview: (id: string, reason?: string) =>
    request<Task>(`/api/tasks/${id}/review/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason: reason || null }),
    }),
  completeReview: (id: string, reason?: string) =>
    request<Task>(`/api/tasks/${id}/review/done`, {
      method: 'POST',
      body: JSON.stringify({ reason: reason || null }),
    }),
  archiveTask: (id: string) =>
    request<ArchiveOperation>(`/api/tasks/${id}/archive`, { method: 'POST' }),
  archiveOperation: (id: string) =>
    request<ArchiveOperation>(`/api/archive-operations/${id}`),
  restartTask: (id: string) =>
    request<Task>(`/api/tasks/${id}/restart`, { method: 'POST' }),
  provider: () => request<ProviderSettings>('/api/settings/provider'),
  updateProvider: (payload: {
    provider: string
    model: string
    base_url?: string | null
    api_key?: string
  }) =>
    request<ProviderSettings>('/api/settings/provider', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
}
