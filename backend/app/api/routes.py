from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from backend.app.api.schemas import (
    ArchivedTaskRestartRequest,
    ArchiveRequest,
    HumanInputAnswer,
    ModulePreviewRequest,
    ProjectCreate,
    ProjectUpdate,
    ProviderUpdate,
    RefinementAnswers,
    ResumeRequest,
    ReviewDecisionRequest,
    TaskCreate,
    TaskSpecUpdate,
    dump_rules,
)
from backend.app.container import Container
from backend.app.domain.models import ArchivePhase
from backend.app.services.archive import ArchiveError, ArchiveStateError
from backend.app.services.projects import ModuleMapper, ProjectValidationError
from backend.app.services.tasks import RunCapacityError, TaskStateError
from backend.app.services.workspace import WorkspaceError

router = APIRouter(prefix="/api")


@router.websocket("/projects/{project_id}/events")
async def project_event_stream(websocket: WebSocket, project_id: str, after: int = 0) -> None:
    container: Container = websocket.app.state.container
    if not container.projects.get(project_id):
        await websocket.close(code=1008, reason="Project not found")
        return
    await websocket.accept()
    cursor = after
    try:
        while True:
            events = await asyncio.to_thread(container.events.list_for_project, project_id, cursor)
            for event in events:
                await websocket.send_json(event)
                cursor = event["id"]
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        return


def get_container(request: Request) -> Container:
    return request.app.state.container


ContainerDependency = Annotated[Container, Depends(get_container)]


def not_found(entity: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{entity} not found")


@router.get("/health")
def health(container: ContainerDependency) -> dict[str, str]:
    version = container.database.fetch_one("SELECT MAX(version) AS version FROM schema_migrations")
    return {"status": "ok", "database": f"migration-{version['version'] if version else 0}"}


@router.get("/projects")
def list_projects(container: ContainerDependency) -> list[dict[str, Any]]:
    return container.projects.list()


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, container: ContainerDependency) -> dict[str, Any]:
    try:
        return container.project_service.create(
            name=payload.name,
            repository_path=payload.repository_path,
            environment_spec=payload.environment_spec,
            module_mapping=dump_rules(payload.module_mapping) or [],
            verification_commands=(
                [item.model_dump() for item in payload.verification_commands]
                if payload.verification_commands is not None
                else None
            ),
        )
    except (ProjectValidationError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error


@router.get("/projects/{project_id}")
def get_project(project_id: str, container: ContainerDependency) -> dict[str, Any]:
    project = container.projects.get(project_id)
    if not project:
        raise not_found("Project")
    return project


@router.put("/projects/{project_id}")
def update_project(
    project_id: str, payload: ProjectUpdate, container: ContainerDependency
) -> dict[str, Any]:
    values = payload.model_dump(exclude_unset=True)
    if "module_mapping" in values:
        values["module_mapping"] = dump_rules(payload.module_mapping)
    try:
        return container.project_service.update(project_id, values)
    except KeyError as error:
        raise not_found("Project") from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, container: ContainerDependency) -> None:
    try:
        container.project_service.delete(project_id)
    except KeyError as error:
        raise not_found("Project") from error
    except ProjectValidationError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/projects/{project_id}/module-preview")
def preview_module(
    project_id: str, payload: ModulePreviewRequest, container: ContainerDependency
) -> dict[str, Any]:
    project = container.projects.get(project_id)
    if not project:
        raise not_found("Project")
    return {
        "file_path": payload.file_path,
        "modules": ModuleMapper(project["module_mapping"]).match(payload.file_path),
    }


@router.post("/projects/{project_id}/verification/discover")
def discover_project_verification(
    project_id: str, container: ContainerDependency
) -> dict[str, Any]:
    try:
        return container.project_service.discover_verification(project_id)
    except KeyError as error:
        raise not_found("Project") from error


@router.get("/projects/{project_id}/tasks")
def list_tasks(project_id: str, container: ContainerDependency) -> list[dict[str, Any]]:
    if not container.projects.get(project_id):
        raise not_found("Project")
    return container.tasks.list_for_project(project_id)


@router.get("/projects/{project_id}/cockpit")
def get_project_cockpit(project_id: str, container: ContainerDependency) -> dict[str, Any]:
    try:
        return container.cockpit.project(project_id)
    except KeyError as error:
        raise not_found("Project") from error


@router.post("/projects/{project_id}/tasks", status_code=status.HTTP_201_CREATED)
def create_task(
    project_id: str, payload: TaskCreate, container: ContainerDependency
) -> dict[str, Any]:
    try:
        return container.task_service.create(project_id, payload.title, payload.raw_request)
    except KeyError as error:
        raise not_found("Project") from error


@router.get("/tasks/{task_id}")
def get_task(task_id: str, container: ContainerDependency) -> dict[str, Any]:
    task = container.tasks.get(task_id)
    if not task:
        raise not_found("Task")
    return task


@router.post("/tasks/{task_id}/refine")
def refine_task(task_id: str, container: ContainerDependency) -> dict[str, Any]:
    try:
        return container.task_service.refine(task_id)
    except KeyError as error:
        raise not_found("Task") from error
    except TaskStateError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/tasks/{task_id}/answers")
def answer_task(
    task_id: str, payload: RefinementAnswers, container: ContainerDependency
) -> dict[str, Any]:
    try:
        return container.task_service.answer(
            task_id, [answer.model_dump() for answer in payload.answers]
        )
    except KeyError as error:
        raise not_found("Task") from error
    except TaskStateError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.put("/tasks/{task_id}/spec")
def update_task_spec(
    task_id: str, payload: TaskSpecUpdate, container: ContainerDependency
) -> dict[str, Any]:
    try:
        return container.task_service.update_spec(task_id, payload.spec)
    except KeyError as error:
        raise not_found("Task") from error
    except TaskStateError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/tasks/{task_id}/confirm")
def confirm_task(task_id: str, container: ContainerDependency) -> dict[str, Any]:
    try:
        return container.task_service.confirm(task_id)
    except KeyError as error:
        raise not_found("Task") from error
    except TaskStateError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/tasks/{task_id}/prepare")
def prepare_task(task_id: str, container: ContainerDependency) -> dict[str, Any]:
    try:
        return container.task_runtime.prepare(task_id)
    except KeyError as error:
        raise not_found("Task") from error
    except TaskStateError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except WorkspaceError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error


@router.post("/tasks/{task_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_task(task_id: str, container: ContainerDependency) -> dict[str, Any]:
    try:
        return await container.task_runtime.start(task_id)
    except KeyError as error:
        raise not_found("Task") from error
    except RunCapacityError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": error.code, "message": str(error), "limit": error.limit},
        ) from error
    except TaskStateError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except WorkspaceError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error


@router.get("/tasks/{task_id}/runs")
def list_runs(task_id: str, container: ContainerDependency) -> list[dict[str, Any]]:
    if not container.tasks.get(task_id):
        raise not_found("Task")
    return container.runs.list_for_task(task_id)


@router.get("/tasks/{task_id}/events")
def list_events(
    task_id: str, container: ContainerDependency, after: int = 0
) -> list[dict[str, Any]]:
    if not container.tasks.get(task_id):
        raise not_found("Task")
    return container.events.list_for_task(task_id, after)


@router.get("/runs/{run_id}")
def get_run(run_id: str, container: ContainerDependency) -> dict[str, Any]:
    run = container.runs.get(run_id)
    if not run:
        raise not_found("Run")
    return run


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_run(run_id: str, container: ContainerDependency) -> dict[str, Any]:
    try:
        return container.task_runtime.cancel(run_id)
    except KeyError as error:
        raise not_found("Run") from error
    except TaskStateError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("/tasks/{task_id}/workspace")
def get_workspace(task_id: str, container: ContainerDependency) -> dict[str, Any]:
    task = container.tasks.get(task_id)
    if not task:
        raise not_found("Task")
    if task["status"] == "ARCHIVED":
        snapshot = container.snapshots.latest(task_id)
        if not snapshot:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Archived Task has no persisted final Workspace snapshot",
            )
        return snapshot["payload"]
    if not task["workspace_path"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Workspace is not prepared"
        )
    project = container.projects.get(task["project_id"])
    assert project is not None
    try:
        return container.task_runtime.observer.capture(
            task=task,
            project=project,
            run_id=None,
            emit=lambda event_type, payload: container.task_runtime._emit(
                task_id, None, event_type, "workspace", payload
            ),
        )
    except WorkspaceError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error


async def _execute_archive(container: Container, operation_id: str) -> None:
    try:
        await asyncio.to_thread(container.archive_service.execute, operation_id)
    except Exception:
        # ArchiveService persists a redacted FAILED operation for polling and retry.
        return


@router.post("/tasks/{task_id}/archive", status_code=status.HTTP_202_ACCEPTED)
async def archive_task(
    task_id: str,
    container: ContainerDependency,
    payload: ArchiveRequest | None = None,
) -> dict[str, Any]:
    try:
        operation = await asyncio.to_thread(
            container.archive_service.begin_archive,
            task_id,
            actor=(payload.actor if payload else "local-user"),
        )
        existing_job = container.archive_jobs.get(operation["id"])
        if operation["phase"] != ArchivePhase.COMPLETED.value and (
            existing_job is None or existing_job.done()
        ):
            job = asyncio.create_task(
                _execute_archive(container, operation["id"]),
                name=f"archive-{operation['id']}",
            )
            container.archive_jobs[operation["id"]] = job
            job.add_done_callback(
                lambda _job, operation_id=operation["id"]: container.archive_jobs.pop(
                    operation_id, None
                )
            )
        return operation
    except KeyError as error:
        raise not_found("Task") from error
    except ArchiveStateError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (ArchiveError, WorkspaceError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error


@router.get("/archive-operations/{operation_id}")
def get_archive_operation(
    operation_id: str, container: ContainerDependency
) -> dict[str, Any]:
    operation = container.archives.get(operation_id)
    if not operation:
        raise not_found("Archive operation")
    return operation


@router.post("/tasks/{task_id}/restart", status_code=status.HTTP_201_CREATED)
async def restart_archived_task(
    task_id: str,
    container: ContainerDependency,
    payload: ArchivedTaskRestartRequest | None = None,
) -> dict[str, Any]:
    del payload  # Reserved for a future user-supplied title without changing V1 semantics.
    try:
        return await asyncio.to_thread(container.archive_service.restart, task_id)
    except KeyError as error:
        raise not_found("Task") from error
    except ArchiveStateError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (ArchiveError, WorkspaceError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error


@router.get("/tasks/{task_id}/checks")
def list_checks(task_id: str, container: ContainerDependency) -> list[dict[str, Any]]:
    if not container.tasks.get(task_id):
        raise not_found("Task")
    return container.checks.list_for_task(task_id)


@router.get("/tasks/{task_id}/inputs")
def list_inputs(task_id: str, container: ContainerDependency) -> list[dict[str, Any]]:
    if not container.tasks.get(task_id):
        raise not_found("Task")
    return container.human_inputs.list_for_task(task_id)


@router.post("/tasks/{task_id}/input")
def answer_human_input(
    task_id: str, payload: HumanInputAnswer, container: ContainerDependency
) -> dict[str, Any]:
    try:
        return container.task_runtime.answer_input(task_id, payload.answer)
    except KeyError as error:
        raise not_found("Task") from error
    except TaskStateError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/tasks/{task_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_task(
    task_id: str,
    container: ContainerDependency,
    payload: ResumeRequest | None = None,
) -> dict[str, Any]:
    try:
        instruction = (
            container.credentials.redact(payload.instruction.strip())
            if payload and payload.instruction and payload.instruction.strip()
            else None
        )
        task = container.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        if task["status"] not in {"NEEDS_YOU", "FAILED", "REVIEW", "DONE"}:
            raise TaskStateError("Only NEEDS_YOU, FAILED, REVIEW, or DONE tasks can resume")
        existing = container.runs.list_for_task(task_id)
        container.reviews.create(
            task_id=task_id,
            run_id=existing[0]["id"] if existing else None,
            decision="RESUME",
            reason=instruction,
        )
        return await container.task_runtime.resume(
            task_id,
            instruction=instruction,
        )
    except KeyError as error:
        raise not_found("Task") from error
    except RunCapacityError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": error.code, "message": str(error), "limit": error.limit},
        ) from error
    except TaskStateError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("/tasks/{task_id}/review")
def get_review(task_id: str, container: ContainerDependency) -> dict[str, Any]:
    task = container.tasks.get(task_id)
    if not task:
        raise not_found("Task")
    runs = container.runs.list_for_task(task_id)
    if task["workspace_path"]:
        project = container.projects.get(task["project_id"])
        assert project is not None
        container.task_runtime.observer.capture(task=task, project=project, run_id=None)
    latest_snapshot = container.snapshots.latest(task_id)
    return {
        "task": task,
        "run": runs[0] if runs else None,
        "workspace": latest_snapshot["payload"] if latest_snapshot else None,
        "checks": container.checks.list_for_task(task_id),
        "inputs": container.human_inputs.list_for_task(task_id),
        "decisions": container.reviews.list_for_task(task_id),
    }


@router.post("/tasks/{task_id}/review/reject")
def reject_review(
    task_id: str, payload: ReviewDecisionRequest, container: ContainerDependency
) -> dict[str, Any]:
    task = container.tasks.get(task_id)
    if not task:
        raise not_found("Task")
    if task["status"] != "REVIEW":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task is not in REVIEW")
    runs = container.runs.list_for_task(task_id)
    container.reviews.create(
        task_id=task_id,
        run_id=runs[0]["id"] if runs else None,
        decision="REJECT",
        reason=payload.reason,
    )
    return container.tasks.update(task_id, status="NEEDS_YOU", runtime_phase="WAITING")  # type: ignore[return-value]


@router.post("/tasks/{task_id}/review/done")
def complete_review(
    task_id: str, payload: ReviewDecisionRequest, container: ContainerDependency
) -> dict[str, Any]:
    task = container.tasks.get(task_id)
    if not task:
        raise not_found("Task")
    if task["status"] != "REVIEW":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task is not in REVIEW")
    runs = container.runs.list_for_task(task_id)
    container.reviews.create(
        task_id=task_id,
        run_id=runs[0]["id"] if runs else None,
        decision="DONE",
        reason=payload.reason,
    )
    return container.tasks.update(task_id, status="DONE", runtime_phase=None)  # type: ignore[return-value]


@router.get("/settings/provider")
def get_provider(container: ContainerDependency) -> dict[str, Any]:
    provider = container.providers.get()
    provider["has_api_key"] = provider["has_api_key"] or container.credentials.has_api_key()
    return provider


@router.put("/settings/provider")
def update_provider(payload: ProviderUpdate, container: ContainerDependency) -> dict[str, Any]:
    if payload.api_key:
        try:
            container.credentials.set_api_key(payload.api_key)
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Unable to save API Key in the operating-system credential store",
            ) from error
    has_key = container.credentials.has_api_key()
    if payload.provider != "demo" and not has_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The selected provider requires an API Key",
        )
    return container.providers.save(
        provider=payload.provider,
        model=payload.model,
        base_url=payload.base_url,
        has_api_key=has_key,
    )
