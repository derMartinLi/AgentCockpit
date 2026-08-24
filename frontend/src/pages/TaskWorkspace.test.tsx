import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { Task } from '../types'
import { TaskWorkspace } from './TaskWorkspace'

vi.mock('../api/client', () => ({
  api: {
    task: vi.fn(async () => null),
    runs: vi.fn(async () => []),
    events: vi.fn(async () => []),
    workspace: vi.fn(async () => ({
      captured_at: '2026-08-21T00:00:01Z',
      identity: {
        verified: true,
        workspace_path: 'G:/worktrees/task-1',
        git_common_dir: 'G:/repo/.git',
        branch: 'agent/task-task-1',
        head: 'abc123',
        locked: false,
        prunable: false,
      },
      files: [{ path: 'src/change.ts', status: 'Modified', added: 2, deleted: 1, modules: ['frontend'] }],
      modules: ['frontend'],
      diff: '+const changed = true',
    })),
    checks: vi.fn(async () => [{ id: 'check-1', run_id: 'run-1', name: 'tests', command: ['pnpm', 'test'], cwd: '.', status: 'passed', exit_code: 0, output_excerpt: '', started_at: '2026-08-21T00:00:01Z', finished_at: '2026-08-21T00:00:02Z' }]),
    inputs: vi.fn(async () => []),
    review: vi.fn(async () => ({
      task: null,
      run: null,
      workspace: {
        captured_at: '2026-08-21T00:00:01Z',
        identity: {
          verified: true,
          workspace_path: 'G:/worktrees/task-1',
          git_common_dir: 'G:/repo/.git',
          branch: 'agent/task-task-1',
          head: 'abc123',
          locked: false,
          prunable: false,
        },
        files: [{ path: 'src/change.ts', status: 'Modified', added: 2, deleted: 1, modules: ['frontend'] }],
        modules: ['frontend'],
        diff: '+const changed = true',
      },
      checks: [{ id: 'check-1', run_id: 'run-1', name: 'tests', command: ['pnpm', 'test'], cwd: '.', status: 'passed', exit_code: 0, output_excerpt: '', started_at: '2026-08-21T00:00:01Z', finished_at: '2026-08-21T00:00:02Z' }],
      inputs: [],
      decisions: [],
    })),
    archiveTask: vi.fn(async () => ({
      id: 'archive-1', task_id: 'task-1', phase: 'FAILED', actor: 'user',
      original_workspace_path: 'G:/worktrees/task-1', archive_ref: null, archive_commit: null,
      error: 'Worktree identity changed', started_at: '2026-08-22T00:00:00Z',
      updated_at: '2026-08-22T00:00:01Z', completed_at: null,
    })),
    archiveOperation: vi.fn(async () => null),
    restartTask: vi.fn(async () => null),
    resumeTask: vi.fn(async () => null),
  },
}))

const task: Task = {
  id: 'task-1',
  project_id: 'project-1',
  title: 'Remember me',
  raw_request: 'Add remember me to login',
  spec: null,
  status: 'DRAFT',
  runtime_phase: 'REFINING',
  refinement_round: 0,
  inspection: null,
  questions: [],
  answers: [],
  workspace_path: null,
  branch_name: null,
  baseline_commit: null,
  created_at: '2026-08-21T00:00:00Z',
  updated_at: '2026-08-21T00:00:00Z',
}

describe('TaskWorkspace', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('offers a retry only when automatic AI refinement did not finish', async () => {
    render(<TaskWorkspace initialTask={task} onBack={vi.fn()} onChanged={vi.fn()} />)
    expect(screen.getByText('AI 需求分析尚未完成')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重新运行 AI 需求分析/ })).toBeEnabled()
  })

  it('separates agent summary from verified workspace evidence in review', async () => {
    vi.mocked(api.runs).mockResolvedValueOnce([
      {
        id: 'run-1',
        task_id: task.id,
        status: 'COMPLETED',
        summary: 'Agent says the change is complete.',
        result: {
          status: 'COMPLETED',
          summary: 'Agent says the change is complete.',
          changes: ['Reported change'],
          checks: [{ name: 'tests', status: 'passed' }],
          known_issues: [],
          needs_human: false,
        },
        started_at: '2026-08-21T00:00:00Z',
        finished_at: '2026-08-21T00:00:02Z',
        cancel_requested: false,
        previous_run_id: null,
      },
    ])
    const reviewTask: Task = {
      ...task,
      status: 'REVIEW',
      runtime_phase: null,
      workspace_path: 'G:/worktrees/task-1',
      branch_name: 'agent/task-task-1',
      baseline_commit: 'abc123',
    }

    render(<TaskWorkspace initialTask={reviewTask} onBack={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/Actual Workspace · Source of truth/)).toBeInTheDocument()
    expect(screen.getByText(/src\/change.ts/)).toBeInTheDocument()
    expect(screen.getByText('+const changed = true')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Done' })).toBeEnabled()
  })

  it('keeps the intervention panel and workspace proof in normal flow', async () => {
    vi.mocked(api.runs).mockResolvedValueOnce([
      {
        id: 'run-cancelled', task_id: task.id, status: 'CANCELLED',
        summary: 'Cancelled safely.',
        result: { status: 'CANCELLED', summary: 'Cancelled safely.', changes: [], checks: [], known_issues: [], needs_human: true },
        started_at: '2026-08-21T00:00:00Z', finished_at: '2026-08-21T00:00:02Z',
        cancel_requested: true, previous_run_id: null,
      },
    ])
    const needsTask: Task = {
      ...task,
      status: 'NEEDS_YOU',
      runtime_phase: 'WAITING',
      workspace_path: 'G:/worktrees/task-1',
      branch_name: 'agent/task-task-1',
      baseline_commit: 'abc123',
    }

    const rendered = render(<TaskWorkspace initialTask={needsTask} onBack={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText('Human intervention')).toBeInTheDocument()
    expect(rendered.container.querySelector('.intervention-card .workspace-proof')).not.toBeNull()
    expect(rendered.container.querySelector('.intervention-card .confirm-bar')).toBeNull()
  })

  it('sends an editable continuation instruction when resuming', async () => {
    const failedTask: Task = {
      ...task,
      status: 'FAILED',
      runtime_phase: null,
      workspace_path: 'G:/worktrees/task-1',
      branch_name: 'agent/task-task-1',
      baseline_commit: 'abc123',
    }
    const runningTask = { ...failedTask, status: 'RUNNING', runtime_phase: 'IMPLEMENTING' } as Task
    vi.mocked(api.resumeTask).mockResolvedValueOnce({
      id: 'run-resumed', task_id: task.id, status: 'RUNNING', summary: null, result: null,
      started_at: '2026-08-21T00:00:03Z', finished_at: null,
      cancel_requested: false, previous_run_id: 'run-failed',
    })
    vi.mocked(api.task).mockResolvedValueOnce(runningTask)

    render(<TaskWorkspace initialTask={failedTask} onBack={vi.fn()} onChanged={vi.fn()} />)

    fireEvent.change(await screen.findByLabelText('Resume instruction'), {
      target: { value: 'Keep the implementation and fix only the failed test.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))

    await waitFor(() => expect(api.resumeTask).toHaveBeenCalledWith(
      failedTask.id,
      'Keep the implementation and fix only the failed test.',
    ))
  })

  it('shows structured progress without exposing hidden reasoning', async () => {
    vi.mocked(api.events).mockResolvedValue([
      {
        id: 1,
        timestamp: '2026-08-21T00:00:01Z',
        task_id: task.id,
        run_id: 'run-running',
        type: 'AgentProgress',
        source: 'agent',
        payload: { step: 1, phase: 'planning', message: '正在选择下一步可观察动作。' },
      },
    ])
    vi.mocked(api.runs).mockResolvedValue([
      {
        id: 'run-running', task_id: task.id, status: 'RUNNING', summary: null, result: null,
        started_at: '2026-08-21T00:00:00Z', finished_at: null,
        cancel_requested: false, previous_run_id: null,
      },
    ])
    const runningTask: Task = {
      ...task,
      status: 'RUNNING',
      runtime_phase: 'IMPLEMENTING',
      workspace_path: 'G:/worktrees/task-1',
      branch_name: 'agent/task-task-1',
      baseline_commit: 'abc123',
    }
    vi.mocked(api.task).mockResolvedValue(runningTask)

    render(<TaskWorkspace initialTask={runningTask} onBack={vi.fn()} onChanged={vi.fn()} />)

    expect((await screen.findAllByText('正在选择下一步可观察动作。')).length).toBeGreaterThan(0)
    expect(screen.getByText(/不记录模型隐藏思维链/)).toBeInTheDocument()
  })

  it('ignores a task refresh that finishes after leaving a running task', async () => {
    class FakeWebSocket {
      static instances: FakeWebSocket[] = []
      onmessage: ((message: { data: string }) => void) | null = null
      onerror: (() => void) | null = null
      onclose: (() => void) | null = null

      constructor(public readonly url: string) {
        FakeWebSocket.instances.push(this)
      }

      close() {}
    }
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const runningTask = {
      ...task,
      status: 'RUNNING',
      runtime_phase: 'IMPLEMENTING',
      workspace_path: 'G:/worktrees/task-1',
    } as Task
    let resolveTask!: (value: Task) => void
    const pendingTask = new Promise<Task>((resolve) => { resolveTask = resolve })
    vi.mocked(api.task).mockReturnValueOnce(pendingTask)
    const onChanged = vi.fn()
    const rendered = render(
      <TaskWorkspace initialTask={runningTask} onBack={vi.fn()} onChanged={onChanged} />,
    )
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))

    act(() => {
      FakeWebSocket.instances[0].onmessage?.({
        data: JSON.stringify({
          id: 99,
          timestamp: '2026-08-21T00:00:01Z',
          task_id: task.id,
          run_id: 'run-running',
          type: 'AgentProgress',
          source: 'agent',
          payload: {},
        }),
      })
    })
    rendered.unmount()
    await act(async () => {
      resolveTask(runningTask)
      await pendingTask
    })

    expect(onChanged).not.toHaveBeenCalled()
  })

  it('keeps summary, actual changes, checks, and continuation visible after Done', async () => {
    vi.mocked(api.runs).mockResolvedValueOnce([
      {
        id: 'run-done', task_id: task.id, status: 'COMPLETED',
        summary: 'Completed the requested change.',
        result: { status: 'COMPLETED', summary: 'Completed the requested change.', changes: ['Reported change'], checks: [], known_issues: [], needs_human: false },
        started_at: '2026-08-21T00:00:00Z', finished_at: '2026-08-21T00:00:02Z',
        cancel_requested: false, previous_run_id: null,
      },
    ])
    const doneTask: Task = {
      ...task,
      status: 'DONE',
      runtime_phase: null,
      workspace_path: 'G:/worktrees/task-1',
      branch_name: 'agent/task-task-1',
      baseline_commit: 'abc123',
    }

    const rendered = render(<TaskWorkspace initialTask={doneTask} onBack={vi.fn()} onChanged={vi.fn()} />)
    const doneView = within(rendered.container)

    expect((await doneView.findAllByText('Completed the requested change.')).length).toBeGreaterThan(0)
    expect(await doneView.findByText('src/change.ts')).toBeInTheDocument()
    expect(doneView.getByText('验证结果')).toBeInTheDocument()
    expect(doneView.getByText('任务已完成，工作现场仍然保留')).toBeInTheDocument()
    expect(doneView.getByRole('button', { name: '继续修改' })).toBeEnabled()
    expect(doneView.getByRole('button', { name: '归档' })).toBeEnabled()
  })

  it('requires confirmation and exposes archive failure with a retry action', async () => {
    const doneTask: Task = {
      ...task,
      status: 'DONE',
      workspace_path: 'G:/worktrees/task-1',
      branch_name: 'agent/task-task-1',
      baseline_commit: 'abc123',
    }
    render(<TaskWorkspace initialTask={doneTask} onBack={vi.fn()} onChanged={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: '归档' }))
    expect(screen.getByText('归档后将释放本地 Workspace')).toBeInTheDocument()
    expect(api.archiveTask).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '确认归档' }))
    expect(await screen.findByText('Worktree identity changed')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试归档' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '重试归档' }))
    await waitFor(() => expect(api.archiveTask).toHaveBeenCalledTimes(2))
  })

  it('keeps Archived details read-only and restarts into a linked new Task', async () => {
    const archivedTask: Task = {
      ...task,
      status: 'ARCHIVED',
      workspace_path: null,
      branch_name: 'agent/task-task-1',
      baseline_commit: 'abc123',
      archived_at: '2026-08-22T00:00:00Z',
      archive_ref: 'refs/agent-cockpit/archives/task-1',
      archive_commit: 'archive123',
    }
    const restartedTask: Task = {
      ...task,
      id: 'task-restarted',
      status: 'READY',
      source_task_id: archivedTask.id,
      workspace_path: 'G:/worktrees/task-restarted',
      branch_name: 'agent/task-restarted',
    }
    vi.mocked(api.restartTask).mockResolvedValueOnce(restartedTask)
    const onChanged = vi.fn()

    render(<TaskWorkspace initialTask={archivedTask} onBack={vi.fn()} onChanged={onChanged} />)

    expect(await screen.findByText('Workspace 已释放，历史证据仍然保留')).toBeInTheDocument()
    expect(screen.getByText('archive123')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '继续修改' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '编辑 JSON' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '重新开始' }))
    await waitFor(() => expect(api.restartTask).toHaveBeenCalledWith(archivedTask.id))
    expect(onChanged).toHaveBeenCalledWith(restartedTask)
  })
})
