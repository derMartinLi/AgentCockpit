import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { CockpitTaskItem, ProjectCockpit as ProjectCockpitData, Task } from '../types'
import { ProjectCockpit } from './ProjectCockpit'

const baseTask: Task = {
  id: 'task-active',
  project_id: 'project-1',
  title: 'Active task',
  raw_request: 'Change the application',
  spec: null,
  status: 'RUNNING',
  runtime_phase: 'IMPLEMENTING',
  refinement_round: 0,
  inspection: null,
  questions: [],
  answers: [],
  workspace_path: 'G:/worktrees/task-active',
  branch_name: 'agent/task-active',
  baseline_commit: 'abc123',
  created_at: '2026-08-22T00:00:00Z',
  updated_at: '2026-08-22T00:00:00Z',
}

function item(task: Task, retained = true): CockpitTaskItem {
  return {
    task,
    workspace: {
      files_count: 2,
      additions: 12,
      deletions: 3,
      modules: ['frontend'],
      recent_files: [{ path: 'frontend/src/App.tsx', status: 'Modified', added: 12, deleted: 3 }],
      captured_at: '2026-08-22T00:00:02Z',
    },
    last_activity: {
      id: 1,
      timestamp: '2026-08-22T00:00:03Z',
      task_id: task.id,
      run_id: 'run-1',
      type: 'WorkspaceSnapshotCaptured',
      source: 'workspace',
      payload: {},
    },
    current_activity: {
      id: 2,
      timestamp: '2026-08-22T00:00:03Z',
      task_id: task.id,
      run_id: 'run-1',
      type: 'AgentProgress',
      source: 'agent',
      payload: { message: '正在修改登录模块' },
    },
    run: {
      id: 'run-1', status: task.status === 'RUNNING' ? 'RUNNING' : 'COMPLETED',
      summary: 'Implemented the requested change.', started_at: '2026-08-22T00:00:00Z',
      finished_at: task.status === 'RUNNING' ? null : '2026-08-22T00:00:03Z', known_issues: [],
    },
    verification: { total: 1, passed: 1, failed: 0, status: 'passed', latest_failure: null },
    attention: {
      level: task.status === 'RUNNING' ? 'live' : task.status === 'REVIEW' ? 'review' : 'history',
      title: task.status === 'RUNNING' ? '正在修改登录模块' : '实现已完成，等待人工 Review',
      detail: 'Implemented the requested change.',
      action: task.status === 'REVIEW' ? '开始 Review' : '查看实时证据',
    },
    workspace_retained: retained,
  }
}

describe('ProjectCockpit', () => {
  it('renders every V1 section, capacity, evidence, and detected overlaps with task links', () => {
    const needsTask = { ...baseTask, id: 'task-needs', title: 'Needs task', status: 'NEEDS_YOU' as const }
    const reviewTask = { ...baseTask, id: 'task-review', title: 'Review task', status: 'REVIEW' as const }
    const doneTask = { ...baseTask, id: 'task-done', title: 'Done task', status: 'DONE' as const }
    const archivedTask = {
      ...baseTask,
      id: 'task-archived',
      title: 'Archived task',
      status: 'ARCHIVED' as const,
      workspace_path: null,
    }
    const cockpit: ProjectCockpitData = {
      project_id: 'project-1',
      generated_at: '2026-08-22T00:00:04Z',
      capacity: { limit: 3, running: 1, available: 2 },
      sections: {
        active: [item(baseTask)],
        needs_you: [item(needsTask)],
        review: [item(reviewTask)],
        done: [item(doneTask)],
        archived: [item(archivedTask, false)],
      },
      history: { done: 1, archived: 1, total: 2 },
      risks: [
        {
          kind: 'FILE_OVERLAP',
          task_ids: [baseTask.id, reviewTask.id],
          task_titles: [baseTask.title, reviewTask.title],
          items: ['frontend/src/App.tsx'],
          snapshot_at: '2026-08-22T00:00:02Z',
        },
        {
          kind: 'MODULE_OVERLAP',
          task_ids: [needsTask.id, baseTask.id],
          task_titles: [needsTask.title, baseTask.title],
          items: ['frontend'],
          snapshot_at: '2026-08-22T00:00:02Z',
        },
      ],
    }
    const onSelectTask = vi.fn()

    render(<ProjectCockpit cockpit={cockpit} onSelectTask={onSelectTask} />)

    for (const title of ['Active', 'Needs You', 'Review']) {
      expect(screen.getByText(title)).toBeInTheDocument()
    }
    expect(screen.queryByText('Done task')).not.toBeInTheDocument()
    expect(screen.getByText('正在修改登录模块')).toBeInTheDocument()
    expect(screen.getByText('Run capacity')).toBeInTheDocument()
    expect(screen.getAllByText('2 files').length).toBeGreaterThan(0)
    expect(screen.getAllByText('+12').length).toBeGreaterThan(0)
    expect(screen.getByText('File overlap')).toBeInTheDocument()
    expect(screen.getByText('Module overlap')).toBeInTheDocument()
    expect(screen.getAllByText('检测到重叠，不代表语义冲突。')).toHaveLength(2)
    expect(screen.getByText('frontend/src/App.tsx')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /完成历史/ }))
    expect(screen.getByText('Done task')).toBeInTheDocument()
    expect(screen.getByText('Archived task')).toBeInTheDocument()
    expect(screen.getByText('Workspace released')).toBeInTheDocument()

    fireEvent.click(screen.getAllByRole('button', { name: 'Review task' }).at(-1)!)
    expect(onSelectTask).toHaveBeenCalledWith(reviewTask)
  })
})
