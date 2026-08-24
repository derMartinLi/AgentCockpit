import { useMemo, useState } from 'react'
import {
  Archive,
  ArrowRight,
  CheckCircle2,
  Clock3,
  FileDiff,
  FolderClock,
  Gauge,
  History,
  Layers3,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'

import { StatusBadge } from './StatusBadge'
import type {
  ActivityEvent,
  CockpitTaskItem,
  ProjectCockpit as ProjectCockpitData,
  Task,
} from '../types'

interface ProjectCockpitProps {
  cockpit: ProjectCockpitData
  onSelectTask: (task: Task) => void
}

export function ProjectCockpit({ cockpit, onSelectTask }: ProjectCockpitProps) {
  const [view, setView] = useState<'live' | 'history'>('live')
  const [historyLimit, setHistoryLimit] = useState(12)
  const liveItems = useMemo(
    () => [
      ...cockpit.sections.needs_you,
      ...cockpit.sections.review,
      ...cockpit.sections.active,
    ],
    [cockpit],
  )
  const historyItems = useMemo(
    () => [...cockpit.sections.done, ...cockpit.sections.archived]
      .sort((left, right) => right.task.updated_at.localeCompare(left.task.updated_at)),
    [cockpit],
  )
  const tasksById = new Map([...liveItems, ...historyItems].map((item) => [item.task.id, item.task]))

  return (
    <>
      <section className="capacity-panel" aria-label="Agent 运行容量">
        <div className="capacity-heading">
          <Gauge size={20} />
          <span><strong>Run capacity</strong><small>同一时间最多执行 {cockpit.capacity.limit} 个 Agent Run</small></span>
        </div>
        <div className="capacity-values">
          <span><strong>{cockpit.capacity.running}</strong><small>Running</small></span>
          <span><strong>{cockpit.capacity.available}</strong><small>Available</small></span>
          <span><strong>{liveItems.length}</strong><small>Live Tasks</small></span>
        </div>
        <div className="capacity-track" aria-label={`${cockpit.capacity.running}/${cockpit.capacity.limit} 个运行槽位已使用`}>
          <i style={{ width: `${Math.min(100, (cockpit.capacity.running / Math.max(1, cockpit.capacity.limit)) * 100)}%` }} />
        </div>
      </section>

      <div className="cockpit-view-tabs" role="tablist" aria-label="Task 视图">
        <button className={view === 'live' ? 'active' : ''} type="button" role="tab" aria-selected={view === 'live'} onClick={() => setView('live')}>
          <Sparkles size={15} /> 当前工作 <b>{liveItems.length}</b>
        </button>
        <button className={view === 'history' ? 'active' : ''} type="button" role="tab" aria-selected={view === 'history'} onClick={() => setView('history')}>
          <History size={15} /> 完成历史 <b>{cockpit.history.total}</b>
        </button>
        <span>首页默认只展示仍需关注的 Task；Done 与 Archived 进入历史视图。</span>
      </div>

      <div className="v1-cockpit-grid">
        <section className="cockpit-sections" aria-label="Task 工作面板">
          {view === 'live' ? (
            <>
              <TaskSection title="Needs You" description="阻塞、失败或需要你决定" items={cockpit.sections.needs_you} onSelectTask={onSelectTask} priority />
              <TaskSection title="Review" description="实现完成，等待审查证据" items={cockpit.sections.review} onSelectTask={onSelectTask} />
              <TaskSection title="Active" description="草稿、待执行和后台运行" items={cockpit.sections.active} onSelectTask={onSelectTask} />
              {liveItems.length === 0 && (
                <div className="cockpit-live-empty"><ShieldCheck size={24} /><strong>当前没有需要关注的 Task</strong><span>创建新任务，或前往完成历史查看已结束工作。</span></div>
              )}
            </>
          ) : (
            <TaskSection
              title="Completed history"
              description={`Done ${cockpit.history.done} · Archived ${cockpit.history.archived}`}
              items={historyItems.slice(0, historyLimit)}
              onSelectTask={onSelectTask}
            />
          )}
          {view === 'history' && historyItems.length > historyLimit && (
            <button className="secondary-button history-more" type="button" onClick={() => setHistoryLimit((value) => value + 12)}>
              显示更多历史（剩余 {historyItems.length - historyLimit}）
            </button>
          )}
        </section>

        <RiskPanel cockpit={cockpit} tasksById={tasksById} onSelectTask={onSelectTask} />
      </div>
    </>
  )
}

function TaskSection({ title, description, items, onSelectTask, priority = false }: {
  title: string
  description: string
  items: CockpitTaskItem[]
  onSelectTask: (task: Task) => void
  priority?: boolean
}) {
  if (items.length === 0) return null
  return (
    <section className={`cockpit-section ${priority ? 'cockpit-section-priority' : ''}`}>
      <header>
        <span><strong>{title}</strong><small>{description}</small></span>
        <b>{items.length}</b>
      </header>
      <div className="cockpit-task-list">
        {items.map((item) => <TaskCard key={item.task.id} item={item} onSelectTask={onSelectTask} />)}
      </div>
    </section>
  )
}

function TaskCard({ item, onSelectTask }: { item: CockpitTaskItem; onSelectTask: (task: Task) => void }) {
  const failure = item.verification.latest_failure
  return (
    <article className={`cockpit-task-card attention-${item.attention.level}`}>
      <span className={`task-signal signal-${item.task.status.toLowerCase()}`} />
      <div className="cockpit-task-main">
        <div className="cockpit-task-title">
          <div><strong>{item.task.title}</strong><small>{item.task.runtime_phase ?? item.run?.status ?? 'TASK'}</small></div>
          <StatusBadge status={item.task.status} />
        </div>

        <div className="task-attention">
          {item.attention.level === 'warning' || item.attention.level === 'blocked'
            ? <ShieldAlert size={16} />
            : item.attention.level === 'live'
              ? <Sparkles size={16} />
              : <CheckCircle2 size={16} />}
          <span><strong>{item.attention.title}</strong>{item.attention.detail && <small>{item.attention.detail}</small>}</span>
        </div>

        {item.run?.summary && item.run.summary !== item.attention.detail && (
          <p className="cockpit-run-summary">{item.run.summary}</p>
        )}

        <div className="cockpit-task-evidence">
          <span><FileDiff size={13} /> {item.workspace?.files_count ?? 0} files <i>+{item.workspace?.additions ?? 0}</i> <em>-{item.workspace?.deletions ?? 0}</em></span>
          <span><Layers3 size={13} /> {item.workspace?.modules.join(', ') || 'No modules detected'}</span>
          <span className={`verification-pill verification-${item.verification.status}`}>
            {item.verification.status === 'failed' ? <ShieldAlert size={12} /> : <ShieldCheck size={12} />}
            {item.verification.failed > 0
              ? `${item.verification.failed} failed / ${item.verification.passed} passed`
              : item.verification.total
                ? `${item.verification.passed} checks passed`
                : 'Checks not run'}
          </span>
        </div>

        {failure && (
          <div className="cockpit-check-failure">
            <strong>{failure.name}</strong>
            <code>{failure.command.join(' ')} · cwd {failure.cwd}</code>
            <span>{failure.failure_message ?? '验证未通过'}</span>
          </div>
        )}

        {!!item.workspace?.recent_files.length && (
          <div className="cockpit-file-strip">
            {item.workspace.recent_files.slice(0, 3).map((file) => (
              <code key={file.path}>{file.status} · {file.path}</code>
            ))}
            {item.workspace.recent_files.length > 3 && <small>+{item.workspace.recent_files.length - 3} more</small>}
          </div>
        )}

        <footer className="cockpit-task-footer">
          <span><Clock3 size={12} /> {item.current_activity ? `${activityLabel(item.current_activity)} · ${formatTime(item.current_activity.timestamp)}` : 'No agent activity yet'}</span>
          <span className={`workspace-retained ${item.workspace_retained ? 'retained' : item.task.status === 'ARCHIVED' ? 'released' : 'pending'}`}>
            {item.task.status === 'ARCHIVED' ? <Archive size={12} /> : <FolderClock size={12} />}
            {item.workspace_retained ? 'Workspace retained' : item.task.status === 'ARCHIVED' ? 'Workspace released' : 'Workspace not prepared'}
          </span>
          <button className="task-open-button" type="button" onClick={() => onSelectTask(item.task)}>
            {item.attention.action} <ArrowRight size={13} />
          </button>
        </footer>
      </div>
    </article>
  )
}

function RiskPanel({ cockpit, tasksById, onSelectTask }: {
  cockpit: ProjectCockpitData
  tasksById: Map<string, Task>
  onSelectTask: (task: Task) => void
}) {
  return (
    <aside className="risk-panel">
      <div className="risk-panel-heading">
        <span className="eyebrow">Cross-task evidence</span>
        <h2>Detected overlap</h2>
        <p>基于持久化 Workspace Snapshot 检测，不代表语义冲突。</p>
      </div>
      <div className="risk-list">
        {cockpit.risks.length === 0 && (
          <div className="no-risks"><ShieldCheck size={21} /><strong>未检测到重叠</strong><p>当前 Task 的文件和模块没有确定性交集。</p></div>
        )}
        {cockpit.risks.map((risk, index) => (
          <article className={`risk-card risk-${risk.kind.toLowerCase()}`} key={`${risk.kind}-${risk.task_ids.join('-')}-${index}`}>
            <div className="risk-kind">
              {risk.kind === 'FILE_OVERLAP' ? <FileDiff size={17} /> : <Layers3 size={17} />}
              <strong>{risk.kind === 'FILE_OVERLAP' ? 'File overlap' : 'Module overlap'}</strong>
            </div>
            <p>检测到重叠，不代表语义冲突。</p>
            <div className="risk-task-links">
              {risk.task_ids.map((taskId, taskIndex) => {
                const task = tasksById.get(taskId)
                return (
                  <button key={taskId} type="button" disabled={!task} onClick={() => task && onSelectTask(task)}>
                    {risk.task_titles[taskIndex] ?? task?.title ?? taskId}
                  </button>
                )
              })}
            </div>
            <div className="risk-items">{risk.items.map((item) => <code key={item}>{item}</code>)}</div>
            <small><Clock3 size={12} /> Snapshot {formatTime(risk.snapshot_at)}</small>
          </article>
        ))}
      </div>
      <small className="cockpit-generated">投影生成于 {formatTime(cockpit.generated_at)}</small>
    </aside>
  )
}

function activityLabel(event: ActivityEvent): string {
  const message = event.payload.message
  if (typeof message === 'string') return message
  const target = event.payload.path ?? event.payload.target
  return typeof target === 'string' ? `${event.type} · ${target}` : event.type
}

function formatTime(value: string): string {
  return new Date(value).toLocaleString()
}
