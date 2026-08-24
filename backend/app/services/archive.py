from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from backend.app.domain.models import ArchivePhase, Event, TaskStatus, now_iso
from backend.app.services.workspace import WorkspaceManager


class ArchiveError(RuntimeError):
    pass


class ArchiveStateError(ArchiveError):
    pass


class TaskArchiveStore(Protocol):
    def get(self, task_id: str) -> dict[str, Any] | None: ...

    def update(self, task_id: str, **values: Any) -> dict[str, Any] | None: ...

    def create(
        self,
        *,
        project_id: str,
        title: str,
        raw_request: str,
        source_task_id: str | None = None,
        spec: dict[str, Any] | None = None,
        status: str = TaskStatus.DRAFT.value,
    ) -> dict[str, Any]: ...


class ProjectArchiveStore(Protocol):
    def get(self, project_id: str) -> dict[str, Any] | None: ...


class RunArchiveStore(Protocol):
    def list_for_task(self, task_id: str) -> list[dict[str, Any]]: ...


class SnapshotArchiveStore(Protocol):
    def latest(self, task_id: str) -> dict[str, Any] | None: ...


class ArchiveOperationStore(Protocol):
    """Persistence contract implemented by the repository integration layer.

    ``create`` must be idempotent under the unique task_id constraint. ``complete``
    must update the Task, operation, and TaskArchived event in one SQLite transaction.
    """

    def get(self, operation_id: str) -> dict[str, Any] | None: ...

    def get_for_task(self, task_id: str) -> dict[str, Any] | None: ...

    def create(
        self, *, task_id: str, actor: str, original_workspace_path: str
    ) -> dict[str, Any]: ...

    def update(self, operation_id: str, **values: Any) -> dict[str, Any] | None: ...

    def list_incomplete(self) -> list[dict[str, Any]]: ...

    def complete(
        self,
        operation_id: str,
        *,
        task_id: str,
        actor: str,
        archive_ref: str,
        archive_commit: str,
        completed_at: str,
    ) -> dict[str, Any]: ...


class ObserverArchivePort(Protocol):
    def capture(
        self,
        *,
        task: dict[str, Any],
        project: dict[str, Any],
        run_id: str | None,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]: ...


class EventArchiveStore(Protocol):
    def record(self, event: Event) -> None: ...


ForbiddenValues = Iterable[str] | Callable[[], Iterable[str]]


class ArchiveService:
    """Restart-safe coordinator for Git-backed Task workspace archival."""

    def __init__(
        self,
        *,
        tasks: TaskArchiveStore,
        projects: ProjectArchiveStore,
        runs: RunArchiveStore,
        snapshots: SnapshotArchiveStore,
        archives: ArchiveOperationStore,
        observer: ObserverArchivePort,
        workspaces: WorkspaceManager,
        events: EventArchiveStore | None = None,
        forbidden_values: ForbiddenValues | None = None,
        secret_guard: Callable[[Path], None] | None = None,
    ) -> None:
        self.tasks = tasks
        self.projects = projects
        self.runs = runs
        self.snapshots = snapshots
        self.archives = archives
        self.observer = observer
        self.workspaces = workspaces
        self.events = events
        self.forbidden_values = forbidden_values
        self.secret_guard = secret_guard
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def begin_archive(self, task_id: str, *, actor: str = "local-user") -> dict[str, Any]:
        """Validate and persist an operation; callers may execute it in a worker thread."""
        with self._task_lock(task_id):
            task = self._require_task(task_id)
            existing = self.archives.get_for_task(task_id)
            if existing:
                if existing["phase"] == ArchivePhase.FAILED.value:
                    return self.archives.update(
                        existing["id"],
                        phase=ArchivePhase.PREPARING.value,
                        error=None,
                    ) or existing
                return existing
            if task["status"] != TaskStatus.DONE.value:
                raise ArchiveStateError("Only DONE tasks can be archived")
            self._assert_no_active_run(task_id)
            if not task.get("workspace_path") or not task.get("branch_name"):
                raise ArchiveStateError("DONE task has no retained workspace to archive")
            project = self._require_project(task["project_id"])
            workspace = Path(task["workspace_path"])
            self.workspaces.verify_archive_candidate(
                project=project,
                task_id=task_id,
                workspace=workspace,
                expected_branch=task["branch_name"],
            )
            return self.archives.create(
                task_id=task_id,
                actor=actor,
                original_workspace_path=str(workspace.resolve()),
            )

    def archive(self, task_id: str, *, actor: str = "local-user") -> dict[str, Any]:
        operation = self.begin_archive(task_id, actor=actor)
        if operation["phase"] == ArchivePhase.COMPLETED.value:
            return operation
        return self.execute(operation["id"])

    def execute(self, operation_id: str) -> dict[str, Any]:
        operation = self._require_operation(operation_id)
        with self._task_lock(operation["task_id"]):
            operation = self._require_operation(operation_id)
            if operation["phase"] == ArchivePhase.COMPLETED.value:
                return operation
            task = self._require_task(operation["task_id"])
            project = self._require_project(task["project_id"])
            workspace = Path(operation["original_workspace_path"])
            last_successful = operation.get("last_successful_phase")
            try:
                if task["status"] not in {
                    TaskStatus.DONE.value,
                    TaskStatus.ARCHIVED.value,
                }:
                    raise ArchiveStateError("Archive operation no longer belongs to a DONE task")
                self._assert_no_active_run(task["id"])

                # If a previous attempt failed before removing the directory, recapture.
                # This prevents external edits made between attempts from being discarded.
                should_snapshot = workspace.exists()
                if should_snapshot:
                    operation = self._snapshot(
                        operation=operation,
                        task=task,
                        project=project,
                        workspace=workspace,
                    )
                    last_successful = ArchivePhase.SNAPSHOTTED.value

                archive_ref = operation.get("archive_ref")
                archive_commit = operation.get("archive_commit")
                if not archive_ref or not archive_commit:
                    raise ArchiveStateError("Archive operation has no recoverable Git snapshot")
                self.workspaces.verify_archive_ref(
                    project=project,
                    archive_ref=archive_ref,
                    archive_commit=archive_commit,
                )

                operation = self.archives.update(
                    operation_id,
                    phase=ArchivePhase.REMOVING.value,
                    error=None,
                ) or self._require_operation(operation_id)
                if workspace.exists():
                    refreshed_task = self._require_task(task["id"])
                    if refreshed_task["status"] != TaskStatus.DONE.value:
                        raise ArchiveStateError("Task changed state before workspace removal")
                    self._assert_no_active_run(task["id"])
                    self.workspaces.remove_verified(
                        project=project,
                        task_id=task["id"],
                        workspace=workspace,
                        expected_branch=task["branch_name"],
                        archive_ref=archive_ref,
                        archive_commit=archive_commit,
                    )
                else:
                    self.workspaces.verify_removed(
                        project=project,
                        task_id=task["id"],
                        workspace=workspace,
                    )
                last_successful = ArchivePhase.REMOVING.value
                self.archives.update(
                    operation_id,
                    phase=ArchivePhase.REMOVING.value,
                    last_successful_phase=last_successful,
                    error=None,
                )
                return self.archives.complete(
                    operation_id,
                    task_id=task["id"],
                    actor=operation["actor"],
                    archive_ref=archive_ref,
                    archive_commit=archive_commit,
                    completed_at=now_iso(),
                )
            except Exception as error:
                safe_error = self._safe_error(error)
                self.archives.update(
                    operation_id,
                    phase=ArchivePhase.FAILED.value,
                    last_successful_phase=last_successful,
                    error=safe_error,
                )
                raise

    def recover_incomplete(self) -> list[dict[str, Any]]:
        """Resume persisted operations independently; one failure does not block others."""
        recovered: list[dict[str, Any]] = []
        for operation in self.archives.list_incomplete():
            try:
                recovered.append(self.execute(operation["id"]))
            except Exception:
                recovered.append(self._require_operation(operation["id"]))
        return recovered

    def restart(self, task_id: str, *, actor: str = "local-user") -> dict[str, Any]:
        """Create a new READY Task/worktree from an Archived Task's immutable commit."""
        source = self._require_task(task_id)
        if source["status"] != TaskStatus.ARCHIVED.value:
            raise ArchiveStateError("Only ARCHIVED tasks can be restarted")
        operation = self.archives.get_for_task(task_id)
        if not operation or operation["phase"] != ArchivePhase.COMPLETED.value:
            raise ArchiveStateError("Archived task has no completed archive operation")
        project = self._require_project(source["project_id"])
        self.workspaces.verify_archive_ref(
            project=project,
            archive_ref=operation["archive_ref"],
            archive_commit=operation["archive_commit"],
        )
        restarted = self.tasks.create(
            project_id=source["project_id"],
            title=f"{source['title']} (restart)",
            raw_request=source["raw_request"],
            source_task_id=source["id"],
            spec=source.get("spec"),
            status=TaskStatus.READY.value,
        )
        try:
            details = self.workspaces.prepare(
                project=project,
                task_id=restarted["id"],
                base_commit=operation["archive_commit"],
            )
            restarted = self.tasks.update(restarted["id"], **details) or restarted
        except Exception:
            self.tasks.update(
                restarted["id"],
                status=TaskStatus.FAILED.value,
                runtime_phase=None,
            )
            raise
        if self.events:
            timestamp = now_iso()
            payload = {
                "source_task_id": source["id"],
                "restarted_task_id": restarted["id"],
                "archive_commit": operation["archive_commit"],
                "actor": actor,
            }
            self.events.record(
                Event(
                    timestamp=timestamp,
                    task_id=source["id"],
                    run_id=None,
                    type="ArchivedTaskRestarted",
                    source="archive",
                    payload=payload,
                )
            )
            self.events.record(
                Event(
                    timestamp=timestamp,
                    task_id=restarted["id"],
                    run_id=None,
                    type="TaskCreatedFromArchive",
                    source="archive",
                    payload=payload,
                )
            )
        return restarted

    def _snapshot(
        self,
        *,
        operation: dict[str, Any],
        task: dict[str, Any],
        project: dict[str, Any],
        workspace: Path,
    ) -> dict[str, Any]:
        self.workspaces.verify_archive_candidate(
            project=project,
            task_id=task["id"],
            workspace=workspace,
            expected_branch=task["branch_name"],
        )
        forbidden = self._resolved_forbidden_values()
        self.workspaces.assert_no_forbidden_content(
            project=project,
            task_id=task["id"],
            workspace=workspace,
            forbidden_values=forbidden,
        )
        if self.secret_guard:
            self.secret_guard(workspace)
        self.observer.capture(task=task, project=project, run_id=None)
        final_snapshot = self.snapshots.latest(task["id"])
        if not final_snapshot:
            raise ArchiveStateError("Final workspace snapshot was not persisted")
        details = self.workspaces.create_archive_snapshot(
            project=project,
            task_id=task["id"],
            workspace=workspace,
            expected_branch=task["branch_name"],
        )
        return self.archives.update(
            operation["id"],
            phase=ArchivePhase.SNAPSHOTTED.value,
            last_successful_phase=ArchivePhase.SNAPSHOTTED.value,
            final_snapshot_id=final_snapshot["id"],
            archive_ref=details["archive_ref"],
            archive_commit=details["archive_commit"],
            error=None,
        ) or self._require_operation(operation["id"])

    def _resolved_forbidden_values(self) -> list[str]:
        if self.forbidden_values is None:
            return []
        values = (
            self.forbidden_values()
            if callable(self.forbidden_values)
            else self.forbidden_values
        )
        if isinstance(values, str):
            return [values] if values else []
        return [value for value in values if value]

    def _safe_error(self, error: Exception) -> str:
        message = f"{type(error).__name__}: {error}"
        try:
            for value in self._resolved_forbidden_values():
                message = message.replace(value, "[REDACTED]")
        except Exception:
            message = type(error).__name__
        return message[:2000]

    def _assert_no_active_run(self, task_id: str) -> None:
        if any(run["status"] == "RUNNING" for run in self.runs.list_for_task(task_id)):
            raise ArchiveStateError("Task has an active Agent Run")

    def _require_task(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        return task

    def _require_project(self, project_id: str) -> dict[str, Any]:
        project = self.projects.get(project_id)
        if not project:
            raise KeyError(project_id)
        return project

    def _require_operation(self, operation_id: str) -> dict[str, Any]:
        operation = self.archives.get(operation_id)
        if not operation:
            raise KeyError(operation_id)
        return operation

    def _task_lock(self, task_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(task_id, threading.RLock())
