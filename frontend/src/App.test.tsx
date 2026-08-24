import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'

describe('App', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/')
    vi.stubGlobal('WebSocket', undefined)
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url === '/api/health') {
          return new Response(JSON.stringify({ status: 'ok', database: 'migration-1' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        if (url === '/api/projects') {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        throw new Error(`Unexpected request: ${url}`)
      }),
    )
  })

  afterEach(() => vi.unstubAllGlobals())

  it('shows the product-specific empty project experience', async () => {
    render(<App />)
    expect(await screen.findByText(/让每一次 AI 软件变化/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /接入本地项目/ })).toBeEnabled()
    await waitFor(() => expect(screen.getByText('ok')).toBeInTheDocument())
  })

  it('loads the persisted cockpit projection for the selected project', async () => {
    const project = {
      id: 'project-1', name: 'Cockpit project', repository_path: 'G:/repo',
      default_branch: 'main', environment_spec: '', module_mapping: [],
      repository_profile: { manifests: [], ecosystems: [], frameworks: [] }, verification_commands: [],
      created_at: '2026-08-22T00:00:00Z', updated_at: '2026-08-22T00:00:00Z',
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const payload = url === '/api/health'
        ? { status: 'ok', database: 'migration-3' }
        : url === '/api/projects'
          ? [project]
          : url === '/api/projects/project-1/cockpit'
            ? {
                project_id: project.id,
                generated_at: '2026-08-22T00:00:01Z',
                capacity: { limit: 3, running: 0, available: 3 },
                sections: { active: [], needs_you: [], review: [], done: [], archived: [] },
                history: { done: 0, archived: 0, total: 0 },
                risks: [],
              }
            : null
      if (payload === null) throw new Error(`Unexpected request: ${url}`)
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(await screen.findByText('Run capacity')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/projects/project-1')
    expect(screen.getByRole('tab', { name: /完成历史/ })).toBeInTheDocument()
    expect(screen.getByText('未检测到重叠')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/projects/project-1/cockpit',
      expect.objectContaining({ headers: expect.objectContaining({ 'Content-Type': 'application/json' }) }),
    )
  })

  it('reconnects the project event stream from the last delivered event', async () => {
    class FakeWebSocket {
      static instances: FakeWebSocket[] = []
      onopen: (() => void) | null = null
      onmessage: ((message: { data: string }) => void) | null = null
      onerror: (() => void) | null = null
      onclose: (() => void) | null = null

      constructor(public readonly url: string) {
        FakeWebSocket.instances.push(this)
      }

      close() {}
    }
    const project = {
      id: 'project-reconnect', name: 'Reconnect project', repository_path: 'G:/repo',
      default_branch: 'main', environment_spec: '', module_mapping: [],
      repository_profile: { manifests: [], ecosystems: [], frameworks: [] }, verification_commands: [],
      created_at: '2026-08-22T00:00:00Z', updated_at: '2026-08-22T00:00:00Z',
    }
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const payload = url === '/api/health'
        ? { status: 'ok', database: 'migration-3' }
        : url === '/api/projects'
          ? [project]
          : {
              project_id: project.id,
              generated_at: '2026-08-22T00:00:01Z',
              capacity: { limit: 3, running: 0, available: 3 },
              sections: { active: [], needs_you: [], review: [], done: [], archived: [] },
              history: { done: 0, archived: 0, total: 0 },
              risks: [],
            }
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }))

    render(<App />)
    expect(await screen.findByText('Run capacity')).toBeInTheDocument()
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const first = FakeWebSocket.instances[0]
    expect(first.url).toContain('after=0')
    first.onmessage?.({ data: JSON.stringify({ id: 42 }) })
    first.onclose?.()
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2), { timeout: 1500 })
    expect(FakeWebSocket.instances[1].url).toContain('after=42')
  })
})
