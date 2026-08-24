import { type FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { BrowserRouter, useLocation, useMatch, useNavigate } from 'react-router-dom'
import {
  Activity,
  Bot,
  ChevronRight,
  CircleGauge,
  FolderGit2,
  GitBranch,
  LayoutDashboard,
  Plus,
  Settings,
  Shield,
  Sparkles,
} from 'lucide-react'

import { api } from './api/client'
import { Modal } from './components/Modal'
import { ProjectCockpit } from './components/ProjectCockpit'
import { TaskWorkspace } from './pages/TaskWorkspace'
import type { Project, ProjectCockpit as ProjectCockpitData, ProviderSettings, Task } from './types'

export default function App() {
  return (
    <BrowserRouter>
      <CockpitApp />
    </BrowserRouter>
  )
}

export function CockpitApp() {
  const navigate = useNavigate()
  const location = useLocation()
  const taskMatch = useMatch('/projects/:projectId/tasks/:taskId')
  const projectMatch = useMatch('/projects/:projectId')
  const selectedProjectId = taskMatch?.params.projectId ?? projectMatch?.params.projectId ?? null
  const selectedTaskId = taskMatch?.params.taskId ?? null
  const [health, setHealth] = useState<'checking' | 'ok' | 'error'>('checking')
  const [projects, setProjects] = useState<Project[]>([])
  const [cockpit, setCockpit] = useState<ProjectCockpitData | null>(null)
  const [routeTask, setRouteTask] = useState<Task | null>(null)
  const [showProjectModal, setShowProjectModal] = useState(false)
  const [showTaskModal, setShowTaskModal] = useState(false)
  const [showProviderModal, setShowProviderModal] = useState(false)
  const [showProjectSettings, setShowProjectSettings] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const selectedTaskIdRef = useRef(selectedTaskId)

  const selectedProject = projects.find((project) => project.id === selectedProjectId) ?? null

  useEffect(() => {
    Promise.all([api.health(), api.projects()])
      .then(([healthResult, projectResults]) => {
        setHealth(healthResult.status === 'ok' ? 'ok' : 'error')
        setProjects(projectResults)
      })
      .catch((caught) => {
        setHealth('error')
        setError(caught instanceof Error ? caught.message : '无法连接后端')
      })
  }, [])

  useEffect(() => {
    if (health === 'checking' || location.pathname !== '/' || !projects[0]) return
    navigate(`/projects/${projects[0].id}`, { replace: true })
  }, [health, location.pathname, navigate, projects])

  useEffect(() => {
    selectedTaskIdRef.current = selectedTaskId
  }, [selectedTaskId])

  useEffect(() => {
    if (!selectedTaskId) return
    let active = true
    void api.task(selectedTaskId)
      .then((next) => {
        if (!active) return
        if (selectedProjectId && next.project_id !== selectedProjectId) {
          setError('Task 不属于当前 Project')
          return
        }
        setRouteTask(next)
      })
      .catch((caught) => {
        if (active) setError(caught instanceof Error ? caught.message : '无法读取 Task')
      })
    return () => { active = false }
  }, [selectedProjectId, selectedTaskId])

  const loadCockpit = useCallback(async (projectId: string) => {
    try {
      setCockpit(await api.cockpit(projectId))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '无法读取 Project Cockpit')
    }
  }, [])

  useEffect(() => {
    if (!selectedProjectId) return
    api.cockpit(selectedProjectId)
      .then(setCockpit)
      .catch((caught) => setError(caught instanceof Error ? caught.message : '无法读取 Project Cockpit'))
  }, [selectedProjectId])

  useEffect(() => {
    if (!selectedProjectId || typeof window.WebSocket === 'undefined') return
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    let active = true
    let cursor = 0
    let socket: WebSocket | null = null
    let reconnectTimer: number | null = null
    const connect = () => {
      socket = new window.WebSocket(
        `${protocol}//${window.location.host}/api/projects/${selectedProjectId}/events?after=${cursor}`,
      )
      socket.onopen = () => void loadCockpit(selectedProjectId)
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(String(message.data)) as { id?: number }
          if (typeof event.id === 'number') cursor = Math.max(cursor, event.id)
        } catch {
          // The full Cockpit projection below remains the source of truth.
        }
        void loadCockpit(selectedProjectId)
      }
      socket.onerror = () => socket?.close()
      socket.onclose = () => {
        if (active) reconnectTimer = window.setTimeout(connect, 500)
      }
    }
    connect()
    return () => {
      active = false
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      socket?.close()
    }
  }, [loadCockpit, selectedProjectId])

  const updateTask = useCallback((next: Task) => {
    const currentTaskId = selectedTaskIdRef.current
    if (!currentTaskId) return
    setRouteTask(next)
    if (next.id !== currentTaskId) {
      navigate(`/projects/${next.project_id}/tasks/${next.id}`)
    }
  }, [navigate])

  if (selectedTaskId) {
    if (!routeTask || routeTask.id !== selectedTaskId) {
      return (
        <main className="task-workspace task-route-loading">
          {error ? <div className="error-banner">{error}</div> : <section className="cockpit-loading"><Sparkles className="spin" size={22} /><span>正在读取 Task…</span></section>}
          <button className="back-button" type="button" onClick={() => navigate(selectedProjectId ? `/projects/${selectedProjectId}` : '/')}>
            返回项目
          </button>
        </main>
      )
    }
    return (
      <TaskWorkspace
        key={routeTask.id}
        initialTask={routeTask}
        onBack={() => {
          navigate(`/projects/${routeTask.project_id}`)
          void loadCockpit(routeTask.project_id)
        }}
        onChanged={updateTask}
      />
    )
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><CircleGauge size={21} /></div>
          <div><strong>Agent Cockpit</strong><span>LOCAL ENGINEERING CONTROL</span></div>
        </div>

        <nav className="main-nav" aria-label="主导航">
          <button className="nav-item active" type="button"><LayoutDashboard size={17} /> Cockpit</button>
          <button className="nav-item" type="button"><Activity size={17} /> Activity</button>
        </nav>

        <div className="sidebar-section">
          <div className="sidebar-label"><span>Projects</span><button type="button" onClick={() => setShowProjectModal(true)} aria-label="添加项目"><Plus size={15} /></button></div>
          <div className="project-nav-list">
            {projects.map((project) => (
              <button
                key={project.id}
                className={`project-nav ${selectedProjectId === project.id ? 'selected' : ''}`}
                type="button"
                onClick={() => navigate(`/projects/${project.id}`)}
              >
                <span>{project.name.slice(0, 2).toUpperCase()}</span>
                <div><strong>{project.name}</strong><small>{project.default_branch}</small></div>
              </button>
            ))}
            {!projects.length && <p className="sidebar-empty">还没有接入本地仓库。</p>}
          </div>
        </div>

        <footer className="sidebar-footer">
          <button className="provider-row" type="button" onClick={() => setShowProviderModal(true)}>
            <Bot size={17} /><span><strong>Model provider</strong><small>Demo adapter ready</small></span><ChevronRight size={15} />
          </button>
          <div className="health-row"><i className={health} /><span>Local runtime</span><small>{health}</small></div>
        </footer>
      </aside>

      <main className="cockpit-main">
        <header className="topbar">
          <div className="breadcrumb"><span>Projects</span><ChevronRight size={14} /><strong>{selectedProject?.name ?? 'Welcome'}</strong></div>
          <div className="topbar-actions">
            {selectedProject && <button className="secondary-button" type="button" onClick={() => setShowProjectSettings(true)}><Settings size={16} /> Project settings</button>}
            <button className="secondary-button" type="button" onClick={() => setShowProviderModal(true)}><Settings size={16} /> Provider</button>
            <button className="primary-button" type="button" disabled={!selectedProject} onClick={() => setShowTaskModal(true)}><Plus size={17} /> New Task</button>
          </div>
        </header>

        {error && <div className="error-banner global-error" onClick={() => setError(null)}>{error}</div>}

        {!selectedProject ? (
          <EmptyProject onCreate={() => setShowProjectModal(true)} />
        ) : (
          <div className="cockpit-content">
            <section className="project-hero">
              <div>
                <span className="eyebrow">Project cockpit · V1 Release</span>
                <h1>{selectedProject.name}</h1>
                <p>{selectedProject.repository_path}</p>
              </div>
              <div className="project-hero-meta">
                <div className="branch-chip"><GitBranch size={15} /> {selectedProject.default_branch}</div>
                <div className="verification-summary-chip"><Shield size={14} />
                  {selectedProject.repository_profile?.frameworks?.join(', ') || 'Verification not detected'}
                </div>
              </div>
            </section>

            {cockpit?.project_id === selectedProjectId ? (
              <ProjectCockpit cockpit={cockpit} onSelectTask={(task) => navigate(`/projects/${task.project_id}/tasks/${task.id}`)} />
            ) : (
              <section className="cockpit-loading"><Sparkles className="spin" size={22} /><span>正在投影 Project Cockpit…</span></section>
            )}
          </div>
        )}
      </main>

      {showProjectModal && (
        <ProjectModal
          onClose={() => setShowProjectModal(false)}
          onCreated={(project) => {
            setProjects((current) => [project, ...current])
            navigate(`/projects/${project.id}`)
            setShowProjectModal(false)
          }}
        />
      )}
      {showTaskModal && selectedProject && (
        <TaskModal
          project={selectedProject}
          onClose={() => setShowTaskModal(false)}
          onCreated={(task) => {
            setShowTaskModal(false)
            navigate(`/projects/${task.project_id}/tasks/${task.id}`)
          }}
        />
      )}
      {showProviderModal && <ProviderModal onClose={() => setShowProviderModal(false)} />}
      {showProjectSettings && selectedProject && (
        <ProjectSettingsModal
          project={selectedProject}
          onClose={() => setShowProjectSettings(false)}
          onSaved={(project) => {
            setProjects((current) => current.map((item) => item.id === project.id ? project : item))
            setShowProjectSettings(false)
          }}
          onDeleted={(projectId) => {
            const remaining = projects.filter((item) => item.id !== projectId)
            setProjects(remaining)
            navigate(remaining[0] ? `/projects/${remaining[0].id}` : '/')
            setShowProjectSettings(false)
          }}
        />
      )}
    </div>
  )
}

function EmptyProject({ onCreate }: { onCreate: () => void }) {
  return (
    <section className="welcome-surface">
      <div className="welcome-orbit"><FolderGit2 size={33} /><i /><i /><i /></div>
      <span className="eyebrow">Local-first engineering control</span>
      <h1>让每一次 AI 软件变化<br />都可理解、可观察、可控制。</h1>
      <p>接入一个本地 Git Repository，开始构建第一个隔离 Task。</p>
      <button className="primary-button welcome-button" type="button" onClick={onCreate}><Plus size={18} /> 接入本地项目</button>
      <div className="welcome-points"><span><Shield size={15} /> Worktree 隔离</span><span><Sparkles size={15} /> Task Refinement</span><span><Bot size={15} /> Built-in Agent</span></div>
    </section>
  )
}

function ProjectModal({ onClose, onCreated }: { onClose: () => void; onCreated: (project: Project) => void }) {
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [environment, setEnvironment] = useState('Follow the repository README and existing package manifests. Run the smallest relevant test suite.')
  const [mapping, setMapping] = useState('[\n  { "name": "application", "paths": ["backend/**", "frontend/**"] }\n]')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const project = await api.createProject({ name, repository_path: path, environment_spec: environment, module_mapping: JSON.parse(mapping) })
      onCreated(project)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '创建失败')
    } finally { setBusy(false) }
  }

  return (
    <Modal title="接入本地项目" eyebrow="Project setup" onClose={onClose}>
      <form className="modal-form" onSubmit={submit}>
        <label><span>项目名称</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 Agent Cockpit" required /></label>
        <label>
          <span>Git Repository 路径</span>
          <input value={path} onChange={(event) => setPath(event.target.value)} placeholder="G:\\Projects\\my-app" required />
          <small className="field-hint">支持 `G:\Projects\my-app`、`G:/Projects/my-app`、引号包裹路径、`~`、环境变量和相对路径。`G:project` 属于歧义路径，将被拒绝。</small>
        </label>
        <label><span>Environment Spec</span><textarea rows={4} value={environment} onChange={(event) => setEnvironment(event.target.value)} /></label>
        <div className="form-message">接入后会从 package.json、pyproject.toml 和锁文件检测测试框架及可自动执行的命令。</div>
        <label><span>Module Mapping · JSON</span><textarea className="mono-field" rows={5} value={mapping} onChange={(event) => setMapping(event.target.value)} /></label>
        {error && <div className="form-error">{error}</div>}
        <div className="form-actions"><button className="secondary-button" type="button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy} type="submit">{busy ? '正在校验…' : '校验并接入'}</button></div>
      </form>
    </Modal>
  )
}

function TaskModal({ project, onClose, onCreated }: { project: Project; onClose: () => void; onCreated: (task: Task) => void }) {
  const [title, setTitle] = useState('')
  const [request, setRequest] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try { onCreated(await api.createTask(project.id, { title, raw_request: request })) }
    catch (caught) { setError(caught instanceof Error ? caught.message : '创建失败') }
    finally { setBusy(false) }
  }

  return (
    <Modal title="创建软件变更" eyebrow={project.name} onClose={onClose}>
      <form className="modal-form" onSubmit={submit}>
        <label><span>Task 标题</span><input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如 Add remember-me login" required /></label>
        <label><span>原始需求</span><textarea rows={7} value={request} onChange={(event) => setRequest(event.target.value)} placeholder="AI 会先检查仓库，直接生成可编辑草案；只有无法安全推断时才保留澄清问题。" required /></label>
        {error && <div className="form-error">{error}</div>}
        <div className="form-actions"><button className="secondary-button" type="button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy} type="submit">{busy ? 'AI 正在检查仓库…' : '创建并生成 AI 草案'}</button></div>
      </form>
    </Modal>
  )
}

function ProviderModal({ onClose }: { onClose: () => void }) {
  const [settings, setSettings] = useState<ProviderSettings | null>(null)
  const [provider, setProvider] = useState('demo')
  const [model, setModel] = useState('deterministic-alpha')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => { api.provider().then((value) => { setSettings(value); setProvider(value.provider); setModel(value.model); setBaseUrl(value.base_url ?? '') }) }, [])

  async function submit(event: FormEvent) {
    event.preventDefault()
    try {
      const next = await api.updateProvider({ provider, model, base_url: baseUrl || null, api_key: apiKey || undefined })
      setSettings(next)
      setApiKey('')
      setMessage('Provider 配置已保存。')
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : '保存失败') }
  }

  return (
    <Modal title="Model Provider" eyebrow="Agent runtime" onClose={onClose}>
      <form className="modal-form" onSubmit={submit}>
        <div className="provider-choice">
          <button className={provider === 'demo' ? 'selected' : ''} type="button" onClick={() => { setProvider('demo'); setModel('deterministic-alpha') }}><Sparkles size={18} /><span><strong>Demo</strong><small>无需密钥，可重复验收</small></span></button>
          <button className={provider !== 'demo' ? 'selected' : ''} type="button" onClick={() => { setProvider('openai-compatible'); setModel('gpt-5') }}><Bot size={18} /><span><strong>OpenAI compatible</strong><small>LangChain tool calling</small></span></button>
        </div>
        <label><span>Model</span><input value={model} onChange={(event) => setModel(event.target.value)} /></label>
        {provider !== 'demo' && <><label><span>Base URL · 可选</span><input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.openai.com/v1" /></label><label><span>API Key {settings?.has_api_key && '· 已安全保存'}</span><input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={settings?.has_api_key ? '留空以保留现有密钥' : '仅写入系统凭据存储'} /></label></>}
        {message && <div className="form-message">{message}</div>}
        <div className="form-actions"><button className="secondary-button" type="button" onClick={onClose}>关闭</button><button className="primary-button" type="submit">保存配置</button></div>
      </form>
    </Modal>
  )
}

function ProjectSettingsModal({ project, onClose, onSaved, onDeleted }: { project: Project; onClose: () => void; onSaved: (project: Project) => void; onDeleted: (projectId: string) => void }) {
  const [name, setName] = useState(project.name)
  const [environment, setEnvironment] = useState(project.environment_spec)
  const [mapping, setMapping] = useState(JSON.stringify(project.module_mapping, null, 2))
  const [verification, setVerification] = useState(JSON.stringify(project.verification_commands ?? [], null, 2))
  const [profile, setProfile] = useState(project.repository_profile)
  const [previewPath, setPreviewPath] = useState('backend/example.py')
  const [previewModules, setPreviewModules] = useState<string[] | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  async function save(event: FormEvent) {
    event.preventDefault()
    try {
      onSaved(await api.updateProject(project.id, {
        name,
        environment_spec: environment,
        module_mapping: JSON.parse(mapping),
        verification_commands: JSON.parse(verification),
      }))
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : '保存失败') }
  }

  async function preview() {
    try {
      const result = await api.modulePreview(project.id, previewPath)
      setPreviewModules(result.modules)
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : '匹配失败') }
  }

  async function discoverVerification() {
    try {
      const next = await api.discoverVerification(project.id)
      setProfile(next.repository_profile)
      setVerification(JSON.stringify(next.verification_commands, null, 2))
      setMessage('已重新检测并保存仓库验证配置；仍可继续编辑。')
    } catch (caught) { setMessage(caught instanceof Error ? caught.message : '检测失败') }
  }

  async function removeProject() {
    if (!window.confirm('仅空 Project 可以删除。确认删除这个 Project 记录吗？')) return
    try {
      await api.deleteProject(project.id)
      onDeleted(project.id)
    } catch (caught) {
      setMessage(caught instanceof Error ? caught.message : '删除失败')
    }
  }

  return (
    <Modal title="Project Settings" eyebrow={project.repository_path} onClose={onClose}>
      <form className="modal-form" onSubmit={save}>
        <label><span>项目名称</span><input value={name} onChange={(event) => setName(event.target.value)} required /></label>
        <label><span>Environment Spec</span><textarea rows={5} value={environment} onChange={(event) => setEnvironment(event.target.value)} /></label>
        <section className="verification-settings">
          <div className="settings-section-heading">
            <span><strong>Project Verification</strong><small>Agent 可通过专用工具发现并运行这些命令；auto_run 会在实现完成后自动执行。</small></span>
            <button className="secondary-button" type="button" onClick={discoverVerification}>重新检测</button>
          </div>
          <div className="framework-list">
            {(profile?.frameworks ?? []).map((framework) => <span key={framework}>{framework}</span>)}
            {!profile?.frameworks?.length && <small>尚未检测到测试框架</small>}
          </div>
          <label><span>Verification Commands · JSON</span><textarea className="mono-field" rows={12} value={verification} onChange={(event) => setVerification(event.target.value)} /></label>
          <small className="field-hint">每项包含 id、name、kind、command 参数数组、cwd、auto_run 和 source。命令不通过 shell 执行。</small>
        </section>
        <label><span>Module Mapping · JSON</span><textarea className="mono-field" rows={8} value={mapping} onChange={(event) => setMapping(event.target.value)} /></label>
        <div className="module-preview-field">
          <label><span>路径匹配预览</span><input value={previewPath} onChange={(event) => setPreviewPath(event.target.value)} /></label>
          <button className="secondary-button" type="button" onClick={preview}>匹配</button>
        </div>
        {previewModules !== null && <div className="form-message">匹配模块：{previewModules.length ? previewModules.join(', ') : '无'}</div>}
        {message && <div className="form-error">{message}</div>}
        <div className="form-actions"><button className="archive-button" type="button" onClick={removeProject}>删除空项目</button><button className="secondary-button" type="button" onClick={onClose}>取消</button><button className="primary-button" type="submit">保存项目配置</button></div>
      </form>
    </Modal>
  )
}
