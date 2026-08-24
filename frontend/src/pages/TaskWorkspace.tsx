import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowLeft,
  Archive,
  Bot,
  Check,
  CircleDot,
  FileCode2,
  GitBranch,
  LoaderCircle,
  Play,
  RotateCcw,
  Search,
  ShieldCheck,
  Sparkles,
  TerminalSquare,
} from 'lucide-react'

import { api } from '../api/client'
import { StatusBadge } from '../components/StatusBadge'
import type {
  ActivityEvent,
  AgentRun,
  ArchiveOperation,
  CheckResult,
  HumanInput,
  ReviewDecision,
  Task,
  TaskSpec,
  WorkspaceState,
} from '../types'

interface TaskWorkspaceProps {
  initialTask: Task
  onBack: () => void
  onChanged: (task: Task) => void
}

export function TaskWorkspace({ initialTask, onBack, onChanged }: TaskWorkspaceProps) {
  const [task, setTask] = useState(initialTask)
  const [answers, setAnswers] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      initialTask.questions.map((question) => [question.id, question.suggested_answer ?? '']),
    ),
  )
  const [specDraft, setSpecDraft] = useState<TaskSpec | null>(initialTask.spec)
  const [editingSpec, setEditingSpec] = useState(false)
  const [runs, setRuns] = useState<AgentRun[]>([])
  const [events, setEvents] = useState<ActivityEvent[]>([])
  const [workspace, setWorkspace] = useState<WorkspaceState | null>(null)
  const [checks, setChecks] = useState<CheckResult[]>([])
  const [inputs, setInputs] = useState<HumanInput[]>([])
  const [decisions, setDecisions] = useState<ReviewDecision[]>([])
  const [humanAnswer, setHumanAnswer] = useState('')
  const [reviewReason, setReviewReason] = useState('')
  const [resumeInstruction, setResumeInstruction] = useState('')
  const [archiveOperation, setArchiveOperation] = useState<ArchiveOperation | null>(null)
  const [confirmingArchive, setConfirmingArchive] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const onChangedRef = useRef(onChanged)
  const mountedRef = useRef(true)

  const latestRun = runs[0]
  const progressEvents = events.filter((event) =>
    ['AgentProgress', 'AgentStepStarted', 'AgentStepFinished'].includes(event.type),
  )
  const progress = useMemo(() => {
    if (task.status === 'DRAFT') return task.spec ? 58 : task.questions.length ? 34 : 16
    if (task.status === 'READY') return task.workspace_path ? 76 : 68
    if (task.status === 'RUNNING') return 86
    return 100
  }, [task])

  const refreshEvidence = useCallback(async () => {
    const review = await api.review(task.id)
    if (!mountedRef.current) return
    setWorkspace(review.workspace)
    setChecks(review.checks)
    setInputs(review.inputs)
    setDecisions(review.decisions)
  }, [task.id])

  useEffect(() => {
    onChangedRef.current = onChanged
  }, [onChanged])

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  useEffect(() => {
    if (typeof window.WebSocket === 'undefined') return
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    let active = true
    let cursor = 0
    let socket: WebSocket | null = null
    let reconnectTimer: number | null = null
    const connect = () => {
      socket = new window.WebSocket(
        `${protocol}//${window.location.host}/api/projects/${task.project_id}/events?after=${cursor}`,
      )
      socket.onmessage = (message) => {
        if (!active) return
        const event = JSON.parse(String(message.data)) as ActivityEvent
        cursor = Math.max(cursor, event.id)
        if (event.task_id !== task.id) return
        setEvents((current) =>
          current.some((item) => item.id === event.id) ? current : [...current, event],
        )
        if (event.source === 'workspace') void refreshEvidence()
        if (event.source === 'runtime' || event.source === 'agent' || event.source === 'archive') {
          void api.task(task.id).then((next) => {
            if (!active || !mountedRef.current) return
            setTask(next)
            onChangedRef.current(next)
          })
        }
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
  }, [refreshEvidence, task.id, task.project_id])

  useEffect(() => {
    let active = true
    void Promise.all([api.runs(task.id), api.events(task.id)]).then(([nextRuns, nextEvents]) => {
      if (!active || !mountedRef.current) return
      setRuns(nextRuns)
      setEvents(nextEvents)
    })
    return () => { active = false }
  }, [task.id])

  useEffect(() => {
    if (!task.workspace_path && task.status !== 'ARCHIVED') return
    const timeout = window.setTimeout(() => {
      refreshEvidence().catch((caught) => {
        setError(caught instanceof Error ? caught.message : '无法读取 Workspace 证据')
      })
    }, 0)
    return () => window.clearTimeout(timeout)
  }, [refreshEvidence, task.workspace_path, task.status])

  useEffect(() => {
    if (!archiveOperation) return
    const phase = archiveOperation.phase.toUpperCase()
    if (phase === 'FAILED') return
    if (phase === 'COMPLETED') {
      let active = true
      void api.task(task.id).then((next) => {
        if (!active) return
        setTask(next)
        onChangedRef.current(next)
      }).catch((caught) => {
        if (active) setError(caught instanceof Error ? caught.message : '无法刷新归档后的 Task')
      })
      return () => { active = false }
    }
    const timeout = window.setTimeout(() => {
      void api.archiveOperation(archiveOperation.id)
        .then(setArchiveOperation)
        .catch((caught) => setError(caught instanceof Error ? caught.message : '无法读取归档进度'))
    }, 700)
    return () => window.clearTimeout(timeout)
  }, [archiveOperation, task.id])

  useEffect(() => {
    if (task.status !== 'RUNNING') return
    let active = true
    const interval = window.setInterval(async () => {
      try {
        const [nextTask, nextRuns, nextEvents] = await Promise.all([
          api.task(task.id),
          api.runs(task.id),
          api.events(task.id),
        ])
        if (!active) return
        setTask(nextTask)
        setRuns(nextRuns)
        setEvents(nextEvents)
        if (nextTask.workspace_path) await refreshEvidence()
        if (!active || !mountedRef.current) return
        onChangedRef.current(nextTask)
      } catch (caught) {
        if (active) setError(caught instanceof Error ? caught.message : '无法刷新运行状态')
      }
    }, 500)
    return () => {
      active = false
      window.clearInterval(interval)
    }
  }, [refreshEvidence, task.id, task.status])

  async function perform(action: () => Promise<Task>) {
    setBusy(true)
    setError(null)
    try {
      const next = await action()
      setTask(next)
      if (next.spec) setSpecDraft(next.spec)
      if (next.questions.length) {
        setAnswers(Object.fromEntries(
          next.questions.map((question) => [question.id, question.suggested_answer ?? '']),
        ))
      }
      onChanged(next)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }

  async function submitAnswers() {
    await perform(() =>
      api.answerTask(
        task.id,
        task.questions.map((question) => ({
          question_id: question.id,
          answer: answers[question.id] ?? question.suggested_answer ?? '',
        })),
      ),
    )
  }

  async function saveSpec() {
    if (!specDraft) return
    await perform(() => api.updateSpec(task.id, specDraft))
    setEditingSpec(false)
  }

  async function runTask() {
    setBusy(true)
    setError(null)
    try {
      if (!task.workspace_path) {
        const prepared = await api.prepareTask(task.id)
        setTask(prepared)
        onChanged(prepared)
      }
      const run = await api.runTask(task.id)
      setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)])
      const startedTask = await api.task(task.id)
      setTask(startedTask)
      onChanged(startedTask)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '执行失败')
    } finally {
      setBusy(false)
    }
  }

  async function cancelRun() {
    if (!latestRun) return
    setBusy(true)
    setError(null)
    try {
      await api.cancelRun(latestRun.id)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '取消失败')
    } finally {
      setBusy(false)
    }
  }

  async function resumeTask() {
    setBusy(true)
    setError(null)
    try {
      const run = await api.resumeTask(task.id, resumeInstruction.trim() || undefined)
      setRuns((current) => [run, ...current])
      setResumeInstruction('')
      const next = await api.task(task.id)
      setTask(next)
      onChanged(next)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '恢复失败')
    } finally {
      setBusy(false)
    }
  }

  async function submitHumanInput() {
    if (!humanAnswer.trim()) return
    setBusy(true)
    try {
      await api.answerInput(task.id, humanAnswer.trim())
      setHumanAnswer('')
      await resumeTask()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '提交人工决定失败')
      setBusy(false)
    }
  }

  async function archiveTask() {
    setBusy(true)
    setError(null)
    setConfirmingArchive(false)
    try {
      setArchiveOperation(await api.archiveTask(task.id))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '归档启动失败')
    } finally {
      setBusy(false)
    }
  }

  async function restartArchivedTask() {
    setBusy(true)
    setError(null)
    try {
      const next = await api.restartTask(task.id)
      setTask(next)
      if (next.spec) setSpecDraft(next.spec)
      onChanged(next)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '无法从归档快照重新开始')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="task-workspace">
      <button className="back-button" type="button" onClick={onBack}>
        <ArrowLeft size={16} /> 返回项目
      </button>

      <header className="task-heading">
        <div>
          <div className="heading-meta">
            <StatusBadge status={task.status} />
            <span>{task.runtime_phase ?? 'TASK SPEC'}</span>
          </div>
          <h1>{task.title}</h1>
          <p>{task.raw_request}</p>
        </div>
        <div className="task-progress" aria-label={`任务进度 ${progress}%`}>
          <strong>{progress}%</strong>
          <span>Observable workflow</span>
          <div><i style={{ width: `${progress}%` }} /></div>
        </div>
      </header>

      {error && <div className="error-banner">{error}</div>}

      <div className="task-layout">
        <section className="task-primary">
          {task.status === 'DRAFT' && !task.questions.length && !task.spec && (
            <article className="action-panel">
              <div className="action-icon"><Search size={24} /></div>
              <span className="eyebrow">Repository inspection</span>
              <h2>AI 需求分析尚未完成</h2>
              <p>通常创建 Task 时会自动检查仓库并生成可编辑 Spec。你可以在这里安全重试。</p>
              <button className="primary-button" type="button" disabled={busy} onClick={() => perform(() => api.refineTask(task.id))}>
                {busy ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />}
                重新运行 AI 需求分析
              </button>
            </article>
          )}

          {task.status === 'DRAFT' && task.questions.length > 0 && (
            <article className="content-card refinement-card">
              <span className="eyebrow">Refinement · Round {task.refinement_round}/2</span>
              <h2>确认关键决策</h2>
              <p className="section-lead">回答这些问题后，系统将生成最小充分清晰的 Task Spec。</p>
              <div className="question-list">
                {task.questions.map((question, index) => (
                  <label key={question.id} className="question-field">
                    <span className="question-number">0{index + 1}</span>
                    <span className="question-copy">
                      <strong>{question.question}</strong>
                      <small>{question.reason}</small>
                      <textarea
                        rows={3}
                        value={answers[question.id] ?? question.suggested_answer ?? ''}
                        onChange={(event) => setAnswers((current) => ({ ...current, [question.id]: event.target.value }))}
                        placeholder="AI 会预填写建议；只需在需要时修改。"
                      />
                    </span>
                  </label>
                ))}
              </div>
              <button
                className="primary-button"
                type="button"
                disabled={busy || task.questions.some((question) => !(answers[question.id] ?? question.suggested_answer)?.trim())}
                onClick={submitAnswers}
              >
                <Check size={17} /> 生成 Task Spec
              </button>
            </article>
          )}

          {task.spec && task.status === 'DRAFT' && task.questions.length === 0 && (
            <article className="content-card spec-card">
              <div className="card-title-row">
                <div>
                  <span className="eyebrow">Task Spec</span>
                  <h2>执行契约已准备</h2>
                </div>
                <button className="secondary-button" type="button" onClick={() => setEditingSpec((value) => !value)}>
                  {editingSpec ? '查看预览' : '编辑 AI 草案'}
                </button>
              </div>
              {editingSpec ? (
                <>
                  {specDraft && <SpecEditor spec={specDraft} onChange={setSpecDraft} />}
                  <button className="secondary-button" type="button" onClick={saveSpec}>保存修改</button>
                </>
              ) : (
                <SpecView spec={task.spec} />
              )}
              <div className="confirm-bar">
                <div><ShieldCheck size={20} /><span><strong>需要你的确认</strong><small>确认后 Spec 锁定，Task 进入 READY。</small></span></div>
                <button className="primary-button" type="button" disabled={busy} onClick={() => perform(() => api.confirmTask(task.id))}>
                  确认并标记为可执行
                </button>
              </div>
            </article>
          )}

          {task.status === 'READY' && (
            <article className="action-panel ready-panel">
              <div className="action-icon"><Bot size={24} /></div>
              <span className="eyebrow">Ready to run</span>
              <h2>Task 已达到最小充分清晰度</h2>
              <p>启动时将创建独立 Git worktree。Agent 的文件和命令访问只允许发生在该目录中。</p>
              {task.workspace_path && (
                <div className="workspace-proof">
                  <GitBranch size={17} />
                  <span><strong>{task.branch_name}</strong><small>{task.workspace_path}</small></span>
                </div>
              )}
              <button className="primary-button run-button" type="button" disabled={busy} onClick={runTask}>
                {busy ? <LoaderCircle className="spin" size={17} /> : <Play size={17} />}
                {busy ? 'Agent 执行中…' : '创建 Workspace 并运行 Agent'}
              </button>
            </article>
          )}

          {task.status === 'RUNNING' && (
            <>
              <article className="content-card running-panel">
                <div className="card-title-row">
                  <div>
                    <span className="eyebrow">Background agent run</span>
                    <h2>Agent 正在后台执行</h2>
                  </div>
                  <LoaderCircle className="spin" size={24} />
                </div>
                <p className="section-lead">这里展示可审计的执行步骤、工具目标和结果，不记录模型隐藏思维链。</p>
                <div className="live-progress" aria-label="Agent 执行进度">
                  {progressEvents.length === 0 && <p>等待 Agent 产生第一个可观察动作…</p>}
                  {progressEvents.slice(-6).map((event) => (
                    <div key={event.id} className="progress-row">
                      <i />
                      <span>
                        <strong>{eventDescription(event)}</strong>
                        <small>{new Date(event.timestamp).toLocaleTimeString()}</small>
                      </span>
                    </div>
                  ))}
                </div>
                <div className="workspace-proof">
                  <GitBranch size={17} />
                  <span><strong>{task.branch_name}</strong><small>{task.workspace_path}</small></span>
                </div>
                <button className="secondary-button" type="button" disabled={busy} onClick={cancelRun}>
                  <CircleDot size={17} /> 取消运行并保留 Workspace
                </button>
              </article>
              {workspace && <WorkspaceEvidence workspace={workspace} />}
            </>
          )}

          {['REVIEW', 'DONE', 'FAILED', 'NEEDS_YOU', 'ARCHIVED'].includes(task.status) && (
            <>
              <article className="content-card result-card">
                <span className="eyebrow">Agent summary</span>
                <div className="result-heading">
                  <div className={`result-mark result-${latestRun?.result?.status?.toLowerCase() ?? 'failed'}`}>
                    {['REVIEW', 'DONE', 'ARCHIVED'].includes(task.status) ? <Check size={24} /> : <CircleDot size={24} />}
                  </div>
                  <div>
                    <h2>{latestRun?.result?.summary ?? latestRun?.summary ?? '执行结果已记录'}</h2>
                    <p>Run {latestRun?.id.slice(0, 8)} · {latestRun?.status}</p>
                  </div>
                </div>
                {latestRun?.result && (
                  <div className="result-grid">
                    <div>
                      <strong>Agent-reported Changes</strong>
                      {latestRun.result.changes.length === 0 && <span>No changes reported by Agent</span>}
                      {latestRun.result.changes.map((item) => <span key={item}>{item}</span>)}
                    </div>
                    <div>
                      <strong>Known Issues</strong>
                      {latestRun.result.known_issues.length === 0 && <span>No known issues reported</span>}
                      {latestRun.result.known_issues.map((item) => <span key={item}>{item}</span>)}
                    </div>
                    <div>
                      <strong>Potential Risks</strong>
                      {(latestRun.result.risks ?? []).length === 0 && <span>No risks reported</span>}
                      {(latestRun.result.risks ?? []).map((item) => <span key={item}>{item}</span>)}
                    </div>
                  </div>
                )}
              </article>

              {workspace && <WorkspaceEvidence workspace={workspace} />}

              {task.spec && (
                <article className="content-card">
                  <span className="eyebrow">Task Spec</span>
                  <h2>执行契约</h2>
                  <SpecView spec={task.spec} />
                </article>
              )}

              <ChecksEvidence checks={checks} />

              {['NEEDS_YOU', 'FAILED'].includes(task.status) && (
                <article className="content-card intervention-card">
                  <span className="eyebrow">Human intervention</span>
                  <h2>{inputs.find((item) => item.status === 'PENDING')?.question ?? '任务可基于现有修改继续'}</h2>
                  <p>Workspace 会保持原样；继续时创建关联到上次 Run 的新 Run。</p>
                  {inputs.some((item) => item.status === 'PENDING') && (
                    <textarea rows={3} value={humanAnswer} onChange={(event) => setHumanAnswer(event.target.value)} placeholder="输入决定…" />
                  )}
                  {!inputs.some((item) => item.status === 'PENDING') && (
                    <ResumeInstructionInput value={resumeInstruction} onChange={setResumeInstruction} />
                  )}
                  <div className="workspace-proof">
                    <GitBranch size={17} />
                    <span><strong>{task.branch_name}</strong><small>{task.workspace_path}</small></span>
                  </div>
                  <button className="primary-button" type="button" disabled={busy} onClick={inputs.some((item) => item.status === 'PENDING') ? submitHumanInput : resumeTask}>
                    {inputs.some((item) => item.status === 'PENDING') ? '提交并 Resume' : 'Resume'}
                  </button>
                </article>
              )}

              {task.status === 'REVIEW' && (
                <article className="content-card review-action-card">
                  <span className="eyebrow">Human review</span>
                  <h2>记录审查决定</h2>
                  <textarea rows={3} value={reviewReason} onChange={(event) => setReviewReason(event.target.value)} placeholder="可选：记录拒绝或完成原因…" />
                  <ResumeInstructionInput value={resumeInstruction} onChange={setResumeInstruction} />
                  <div className="review-actions">
                    <button className="secondary-button" type="button" disabled={busy} onClick={() => perform(() => api.rejectReview(task.id, reviewReason))}>Reject</button>
                    <button className="secondary-button" type="button" disabled={busy} onClick={resumeTask}>Resume</button>
                    <button className="primary-button" type="button" disabled={busy} onClick={() => perform(() => api.completeReview(task.id, reviewReason))}>Done</button>
                  </div>
                </article>
              )}

              {task.status === 'DONE' && (
                <article className="content-card done-card">
                  <span className="eyebrow">Done · Workspace retained</span>
                  <h2>任务已完成，工作现场仍然保留</h2>
                  <p>Summary、实际文件、Diff、Checks 和审查历史保持可见。需要继续修改时会复用当前 Workspace 创建新 Run。</p>
                  <ResumeInstructionInput value={resumeInstruction} onChange={setResumeInstruction} />
                  <div className="decision-list">
                    {decisions.map((decision) => (
                      <span key={decision.id}>
                        <strong>{decision.decision}</strong> · {decision.actor} · {new Date(decision.created_at).toLocaleString()}
                        {decision.reason && <small>{decision.reason}</small>}
                      </span>
                    ))}
                  </div>
                  <div className="done-actions">
                    <button className="secondary-button" type="button" disabled={busy || Boolean(archiveOperation)} onClick={resumeTask}>继续修改</button>
                    <button className="archive-button" type="button" disabled={busy || Boolean(archiveOperation)} onClick={() => setConfirmingArchive(true)}><Archive size={16} /> 归档</button>
                  </div>
                  {confirmingArchive && (
                    <div className="archive-confirmation" role="alert">
                      <Archive size={20} />
                      <span><strong>归档后将释放本地 Workspace</strong><small>Task Spec、Runs、Events、Diff、Checks 和 Git 归档快照都会保留。此操作不会自动合并代码。</small></span>
                      <div><button className="secondary-button" type="button" onClick={() => setConfirmingArchive(false)}>取消</button><button className="archive-button" type="button" onClick={archiveTask}>确认归档</button></div>
                    </div>
                  )}
                  {archiveOperation && (
                    <ArchiveProgress operation={archiveOperation} onRetry={archiveTask} />
                  )}
                </article>
              )}

              {task.status === 'ARCHIVED' && (
                <article className="content-card archived-card">
                  <span className="eyebrow">Archived · Read-only history</span>
                  <h2>Workspace 已释放，历史证据仍然保留</h2>
                  <p>此 Task 不能直接编辑或 Resume。重新开始会从保存的 Git archive commit 创建新 Task、Branch 和 Workspace，原归档记录保持不变。</p>
                  <dl className="archive-metadata">
                    <div><dt>Archive commit</dt><dd>{task.archive_commit ?? archiveOperation?.archive_commit ?? 'Recorded in archive history'}</dd></div>
                    <div><dt>Archive ref</dt><dd>{task.archive_ref ?? archiveOperation?.archive_ref ?? 'Recorded in archive history'}</dd></div>
                    <div><dt>Archived at</dt><dd>{task.archived_at ? new Date(task.archived_at).toLocaleString() : 'Recorded'}</dd></div>
                    <div><dt>Original branch</dt><dd>{task.branch_name ?? 'Recorded in Task history'}</dd></div>
                  </dl>
                  <button className="primary-button" type="button" disabled={busy} onClick={restartArchivedTask}><RotateCcw size={16} /> 重新开始</button>
                </article>
              )}
            </>
          )}
        </section>

        <aside className="task-rail">
          <section className="rail-card">
            <span className="eyebrow">Repository inspection</span>
            {task.inspection ? (
              <>
                <strong className="metric-number">{task.inspection.files_scanned}</strong>
                <small>files scanned</small>
                {task.inspection.refinement && (
                  <p className="refinement-engine">
                    {task.inspection.refinement.mode === 'agent' ? 'AI Provider' : 'Local fallback'}
                    {' · '}{task.inspection.refinement.questions_required} questions
                  </p>
                )}
                <div className="mini-list">
                  {task.inspection.manifests.slice(0, 4).map((file) => <span key={file}><FileCode2 size={13} />{file}</span>)}
                </div>
              </>
            ) : <p>等待首次仓库检查。</p>}
          </section>
          <section className="rail-card">
            <span className="eyebrow">Activity</span>
            <div className="event-list">
              {events.length === 0 && <p>Agent 启动后，工具与命令活动会记录在这里。</p>}
              {[...events].reverse().map((event) => (
                <div className="event-item" key={event.id}>
                  <i />
                  <span><strong>{eventDescription(event)}</strong><small>{event.source} · {new Date(event.timestamp).toLocaleTimeString()}</small></span>
                </div>
              ))}
            </div>
          </section>
          <section className="rail-card">
            <span className="eyebrow">Run history</span>
            <div className="run-list">
              {runs.length === 0 && <p>尚无 Agent Run。</p>}
              {runs.map((run) => (
                <div key={run.id}>
                  <strong>{run.status}</strong>
                  <small>{run.id.slice(0, 8)} · {new Date(run.started_at).toLocaleString()}</small>
                  {run.summary && <p>{run.summary}</p>}
                </div>
              ))}
            </div>
          </section>
          <section className="rail-card boundary-card">
            <TerminalSquare size={18} />
            <div><strong>Workspace boundary</strong><p>命令带 cwd 与 timeout，禁止 shell 和 Git 生命周期操作。</p></div>
          </section>
        </aside>
      </div>
    </main>
  )
}

function ResumeInstructionInput({
  value,
  onChange,
}: {
  value: string
  onChange: (value: string) => void
}) {
  return (
    <label className="resume-instruction">
      <span>Resume instruction · optional</span>
      <textarea
        aria-label="Resume instruction"
        rows={3}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder="例如：保留现有实现，只修复失败测试并重新验证。"
      />
      <small>该指令会与上次结果及现有 Workspace 状态一起发送给 Agent。</small>
    </label>
  )
}

function SpecView({ spec }: { spec: TaskSpec }) {
  return (
    <div className="spec-sections">
      <section><span>Goal</span><p>{spec.goal}</p></section>
      <section><span>Scope</span><ul>{spec.scope.map((item) => <li key={item}>{item}</li>)}</ul></section>
      <section><span>Acceptance Criteria</span><ol>{spec.acceptance_criteria.map((item, index) => <li key={item}><b>AC{String(index + 1).padStart(2, '0')}</b>{item}</li>)}</ol></section>
      <section><span>Constraints</span><ul>{spec.constraints.map((item) => <li key={item}>{item}</li>)}</ul></section>
      <section><span>Decisions</span><ul>{spec.decisions.map((item) => <li key={item}>{item}</li>)}</ul></section>
      {spec.interface && <section><span>Interface</span><pre>{spec.interface}</pre></section>}
    </div>
  )
}

function SpecEditor({ spec, onChange }: { spec: TaskSpec; onChange: (spec: TaskSpec) => void }) {
  const setList = (field: 'scope' | 'acceptance_criteria' | 'constraints' | 'decisions', value: string) => {
    onChange({
      ...spec,
      [field]: value.split('\n').map((item) => item.trim()).filter(Boolean),
    })
  }
  return (
    <div className="spec-editor-fields">
      <label><span>Goal</span><textarea rows={3} value={spec.goal} onChange={(event) => onChange({ ...spec, goal: event.target.value })} /></label>
      <label><span>Scope · 每行一项</span><textarea rows={5} value={spec.scope.join('\n')} onChange={(event) => setList('scope', event.target.value)} /></label>
      <label><span>Acceptance Criteria · 每行一项</span><textarea rows={6} value={spec.acceptance_criteria.join('\n')} onChange={(event) => setList('acceptance_criteria', event.target.value)} /></label>
      <label><span>Constraints · 每行一项</span><textarea rows={4} value={spec.constraints.join('\n')} onChange={(event) => setList('constraints', event.target.value)} /></label>
      <label><span>Decisions · 每行一项</span><textarea rows={4} value={spec.decisions.join('\n')} onChange={(event) => setList('decisions', event.target.value)} /></label>
      <label><span>Stable interface · 可选</span><textarea rows={3} value={spec.interface ?? ''} onChange={(event) => onChange({ ...spec, interface: event.target.value || null })} /></label>
    </div>
  )
}

function WorkspaceEvidence({ workspace }: { workspace: WorkspaceState }) {
  return (
    <article className="content-card actual-workspace-card">
      <div className="card-title-row">
        <div>
          <span className="eyebrow">Actual Workspace · Source of truth</span>
          <h2>真实文件变化</h2>
        </div>
        <span className={`identity-chip ${workspace.identity.verified ? 'verified' : ''}`}>
          {workspace.identity.verified ? 'Verified worktree' : 'Unverified'}
        </span>
      </div>
      <div className="workspace-proof">
        <GitBranch size={17} />
        <span>
          <strong>{workspace.identity.branch} · {workspace.identity.head.slice(0, 10)}</strong>
          <small>{workspace.identity.workspace_path}</small>
        </span>
      </div>
      <div className="file-evidence-list">
        {workspace.files.length === 0 && <p>Workspace 当前没有未提交变化。</p>}
        {workspace.files.map((file) => (
          <div key={file.path}>
            <FileCode2 size={14} />
            <span><strong>{file.path}</strong><small>{file.modules.join(', ') || 'Unmapped module'}</small></span>
            <em>{file.status}</em>
            <code>+{file.added} / -{file.deleted}</code>
          </div>
        ))}
      </div>
      <p className="module-summary"><strong>Affected modules:</strong> {workspace.modules.join(', ') || 'No mapped modules'}</p>
      <details className="diff-disclosure" open={workspace.files.length > 0}>
        <summary>Actual Diff</summary>
        <pre className="review-diff">{workspace.diff || 'No uncommitted diff'}</pre>
      </details>
    </article>
  )
}

function ChecksEvidence({ checks }: { checks: CheckResult[] }) {
  return (
    <article className="content-card checks-card">
      <span className="eyebrow">Persisted checks</span>
      <h2>验证结果</h2>
      <div className="check-list">
        {checks.length === 0 && <p>Agent 没有归档检查结果。</p>}
        {checks.map((check) => (
          <div className={check.status === 'failed' ? 'check-row-failed' : ''} key={check.id}>
            <span className={`check-status check-${check.status}`}>{check.status}</span>
            <span>
              <strong>{check.name}</strong>
              <small>{check.command.join(' ') || 'Agent-reported check'} · cwd {check.cwd}</small>
            </span>
            <code>{check.exit_code === null ? 'no exit code' : `exit ${check.exit_code}`}</code>
            <small>{new Date(check.started_at).toLocaleString()}</small>
            {check.failure_message && (
              <div className="check-diagnosis">
                <strong>{check.failure_message}</strong>
                {check.suggested_action && <span>{check.suggested_action}</span>}
              </div>
            )}
            {check.output_excerpt && <pre>{check.output_excerpt}</pre>}
          </div>
        ))}
      </div>
    </article>
  )
}

function ArchiveProgress({ operation, onRetry }: { operation: ArchiveOperation; onRetry: () => void }) {
  const phase = operation.phase.toUpperCase()
  const failed = phase === 'FAILED'
  const completed = phase === 'COMPLETED'
  return (
    <div className={`archive-progress ${failed ? 'failed' : ''}`} aria-live="polite">
      {completed ? <Check size={18} /> : failed ? <CircleDot size={18} /> : <LoaderCircle className="spin" size={18} />}
      <span>
        <strong>{completed ? '归档完成' : failed ? '归档未完成' : `归档进行中 · ${phase}`}</strong>
        <small>
          {operation.error ?? (
            completed
              ? 'Workspace 已释放，持久化历史与 Git 快照已保留。'
              : '正在保存最终快照、验证 Git archive commit 并安全移除 Worktree。'
          )}
        </small>
        {operation.archive_commit && <code>{operation.archive_commit}</code>}
      </span>
      {failed && <button className="secondary-button" type="button" onClick={onRetry}>重试归档</button>}
    </div>
  )
}

function eventDescription(event: ActivityEvent): string {
  const message = event.payload.message
  if (typeof message === 'string') return message
  if (event.type === 'AgentStepStarted') {
    const tool = String(event.payload.tool ?? 'tool')
    const target = event.payload.target ? ` · ${String(event.payload.target)}` : ''
    return `开始 ${tool}${target}`
  }
  if (event.type === 'AgentStepFinished') {
    const status = event.payload.status === 'failed' ? '失败' : '完成'
    return `${String(event.payload.tool ?? 'Tool')} ${status} · ${String(event.payload.duration_ms ?? 0)}ms`
  }
  if (event.type === 'CommandStarted') {
    const command = Array.isArray(event.payload.command) ? event.payload.command.join(' ') : 'command'
    return `命令 · ${command}`
  }
  if (event.type.startsWith('File') && event.payload.path) {
    return `${event.type} · ${String(event.payload.path)}`
  }
  return event.type
}
