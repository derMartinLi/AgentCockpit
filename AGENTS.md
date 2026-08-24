# Repository Guidelines

## Project Structure & Module Organization

This repository contains the V1 local-first coding cockpit. `docs/product/AI调度看板.md` is the product source of truth; implementation and acceptance records live under `docs/roadmap/`.

- `backend/`: Python, FastAPI, SQLite, `asyncio`, and WebSocket services.
- `frontend/`: React, TypeScript, Vite, and pnpm.
- `backend/tests/`: API, runtime, migration, archive, concurrency, and V1 acceptance tests.
- `frontend/src/**/*.test.tsx`: colocated component and workflow tests.

Keep Agent providers behind `AgentAdapter`. SQLite is the fact source; Markdown context files are projections only. Workspace lifecycle operations belong in `WorkspaceManager`/`ArchiveService`.

## Build, Test, and Development Commands

```powershell
uv sync --dev                              # install backend dependencies
uv run uvicorn backend.app.main:app --reload --port 8000
pnpm --dir frontend install                # install frontend dependencies
pnpm --dir frontend dev                    # start Vite
uv run ruff check backend                  # lint Python
uv run pytest                              # run backend tests
pnpm --dir frontend lint                   # lint TypeScript/React
pnpm --dir frontend test                   # run Vitest
pnpm --dir frontend build                  # type-check and production build
```

## Coding Style & Naming Conventions

Use four spaces for Python and two for TypeScript/JSON/YAML. Use `snake_case` for Python modules/functions, `PascalCase` for classes/components, and `camelCase` for TypeScript values. Run Ruff and ESLint; prefer structured events/results over provider-specific payloads.

## Testing Guidelines

Add tests with every behavioral change. Name Python tests `test_<behavior>.py`; use `*.test.ts`/`*.test.tsx` for frontend tests. Cover legal and illegal state transitions, worktree identity, cancellation, persistence, overlaps, archive retry, and secret redaction. Filesystem tests must stay inside pytest temporary directories.

## Commit & Pull Request Guidelines

Use concise imperative subjects, for example `Add archive recovery state machine`. Pull requests should state outcome, affected modules, verification, and linked issue/Task Spec. Include screenshots for UI changes and flag migrations, security boundaries, or compatibility impacts.

## Security & Configuration

Never commit API keys or place secrets in Specs, Events, diffs, or generated context. Keep tools inside the assigned verified worktree. Commands require explicit cwd, timeout, cancellation, and audit records. Never bypass the controlled Archive path with recursive deletion.
