from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.api.routes import router
from backend.app.container import build_container
from backend.app.core.config import Settings, get_settings
from backend.app.services.agent import AgentAdapter


def create_app(
    settings: Settings | None = None,
    *,
    adapter_override: AgentAdapter | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    container = build_container(resolved_settings, adapter_override)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        container.database.initialize()
        interrupted = container.runs.recover_interrupted()
        if interrupted:
            container.database.execute(
                "UPDATE tasks SET status = 'NEEDS_YOU', runtime_phase = 'WAITING' "
                "WHERE status = 'RUNNING'"
            )
        for project in container.projects.list():
            if project["repository_profile"] or project["verification_commands"]:
                continue
            try:
                await asyncio.to_thread(
                    container.project_service.discover_verification, project["id"]
                )
            except (OSError, ValueError):
                # An unavailable repository must not prevent recovery of persisted history.
                pass
        resolved_settings.worktrees_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(container.archive_service.recover_incomplete)
        try:
            yield
        finally:
            for run_id in list(container.task_runtime.active_runs):
                try:
                    container.task_runtime.cancel(run_id)
                except (KeyError, ValueError):
                    pass
            active_runs = list(container.task_runtime.active_runs.values())
            if active_runs:
                await asyncio.wait(active_runs, timeout=5)
            archive_jobs = list(container.archive_jobs.values())
            if archive_jobs:
                await asyncio.wait(archive_jobs, timeout=10)

    application = FastAPI(
        title=resolved_settings.app_name,
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.container = container
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[resolved_settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(router)
    return application


app = create_app()
