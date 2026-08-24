from __future__ import annotations

import asyncio
from dataclasses import dataclass

from backend.app.core.config import Settings
from backend.app.core.credentials import CredentialStore
from backend.app.core.database import Database
from backend.app.repositories.store import (
    ArchiveRepository,
    CheckRepository,
    EventRepository,
    HumanInputRepository,
    ProjectRepository,
    ProviderRepository,
    ReviewRepository,
    RunRepository,
    TaskRepository,
    WorkspaceSnapshotRepository,
)
from backend.app.services.agent import AgentAdapter
from backend.app.services.archive import ArchiveService
from backend.app.services.cockpit import CockpitProjector
from backend.app.services.observer import WorkspaceObserver
from backend.app.services.projects import ProjectService
from backend.app.services.refinement import RefinementService
from backend.app.services.tasks import TaskRuntime, TaskService
from backend.app.services.workspace import WorkspaceManager


@dataclass
class Container:
    settings: Settings
    database: Database
    projects: ProjectRepository
    providers: ProviderRepository
    tasks: TaskRepository
    runs: RunRepository
    events: EventRepository
    snapshots: WorkspaceSnapshotRepository
    checks: CheckRepository
    human_inputs: HumanInputRepository
    reviews: ReviewRepository
    archives: ArchiveRepository
    project_service: ProjectService
    cockpit: CockpitProjector
    task_service: TaskService
    task_runtime: TaskRuntime
    archive_service: ArchiveService
    archive_jobs: dict[str, asyncio.Task[None]]
    credentials: CredentialStore


def build_container(settings: Settings, adapter_override: AgentAdapter | None = None) -> Container:
    database = Database(settings.resolved_database_path)
    projects = ProjectRepository(database)
    providers = ProviderRepository(database)
    tasks = TaskRepository(database)
    runs = RunRepository(database)
    events = EventRepository(database)
    snapshots = WorkspaceSnapshotRepository(database)
    checks = CheckRepository(database)
    human_inputs = HumanInputRepository(database)
    reviews = ReviewRepository(database)
    archives = ArchiveRepository(database)
    credentials = CredentialStore(settings)
    project_service = ProjectService(projects)
    task_service = TaskService(
        tasks=tasks,
        projects=projects,
        providers=providers,
        credentials=credentials,
        events=events,
        refinement=RefinementService(),
    )
    workspace_manager = WorkspaceManager(settings)
    observer = WorkspaceObserver(
        snapshots=snapshots,
        workspaces=workspace_manager,
        redact=credentials.redact,
    )
    task_runtime = TaskRuntime(
        settings=settings,
        tasks=tasks,
        projects=projects,
        providers=providers,
        runs=runs,
        events=events,
        checks=checks,
        human_inputs=human_inputs,
        credentials=credentials,
        workspaces=workspace_manager,
        observer=observer,
        adapter_override=adapter_override,
    )
    archive_service = ArchiveService(
        tasks=tasks,
        projects=projects,
        runs=runs,
        snapshots=snapshots,
        archives=archives,
        observer=observer,
        workspaces=workspace_manager,
        events=events,
        forbidden_values=lambda: [credentials.get_api_key() or ""],
    )
    cockpit = CockpitProjector(
        settings=settings,
        database=database,
        projects=projects,
        tasks=tasks,
        snapshots=snapshots,
        events=events,
        runs=runs,
        checks=checks,
        human_inputs=human_inputs,
    )
    return Container(
        settings=settings,
        database=database,
        projects=projects,
        providers=providers,
        tasks=tasks,
        runs=runs,
        events=events,
        snapshots=snapshots,
        checks=checks,
        human_inputs=human_inputs,
        reviews=reviews,
        archives=archives,
        project_service=project_service,
        cockpit=cockpit,
        task_service=task_service,
        task_runtime=task_runtime,
        archive_service=archive_service,
        archive_jobs={},
        credentials=credentials,
    )
