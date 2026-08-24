from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from backend.app.core import database as database_module
from backend.app.core.database import MIGRATIONS, Database
from backend.app.domain.models import ArchivePhase, TaskStatus, now_iso
from backend.app.services.archive import ArchiveService, ArchiveStateError
from backend.app.services.workspace import WorkspaceError, WorkspaceManager
from backend.tests.conftest import git


class MemoryTasks:
    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self.rows = {task["id"]: task for task in tasks}
        self.created = 0

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self.rows.get(task_id)

    def update(self, task_id: str, **values: Any) -> dict[str, Any] | None:
        row = self.rows.get(task_id)
        if row:
            row.update(values)
        return row

    def create(self, **values: Any) -> dict[str, Any]:
        self.created += 1
        task_id = f"restart-{self.created}"
        row = {
            "id": task_id,
            "workspace_path": None,
            "branch_name": None,
            "baseline_commit": None,
            **values,
        }
        self.rows[task_id] = row
        return row


class MemoryProjects:
    def __init__(self, project: dict[str, Any]) -> None:
        self.project = project

    def get(self, project_id: str) -> dict[str, Any] | None:
        return self.project if project_id == self.project["id"] else None


class NoRuns:
    def list_for_task(self, task_id: str) -> list[dict[str, Any]]:
        return []


class MemorySnapshots:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def latest(self, task_id: str) -> dict[str, Any] | None:
        return self.rows.get(task_id)


class SnapshotObserver:
    def __init__(self, snapshots: MemorySnapshots) -> None:
        self.snapshots = snapshots

    def capture(self, **values: Any) -> dict[str, Any]:
        task_id = values["task"]["id"]
        row = {"id": f"snapshot-{task_id}", "payload": {"captured_at": now_iso()}}
        self.snapshots.rows[task_id] = row
        return row["payload"]


class MemoryArchives:
    def __init__(self, tasks: MemoryTasks, *, fail_complete_once: bool = False) -> None:
        self.tasks = tasks
        self.rows: dict[str, dict[str, Any]] = {}
        self.fail_complete_once = fail_complete_once

    def get(self, operation_id: str) -> dict[str, Any] | None:
        return self.rows.get(operation_id)

    def get_for_task(self, task_id: str) -> dict[str, Any] | None:
        return next((row for row in self.rows.values() if row["task_id"] == task_id), None)

    def create(
        self, *, task_id: str, actor: str, original_workspace_path: str
    ) -> dict[str, Any]:
        existing = self.get_for_task(task_id)
        if existing:
            return existing
        timestamp = now_iso()
        row = {
            "id": f"archive-{task_id}",
            "task_id": task_id,
            "phase": ArchivePhase.PREPARING.value,
            "last_successful_phase": None,
            "actor": actor,
            "original_workspace_path": original_workspace_path,
            "final_snapshot_id": None,
            "archive_ref": None,
            "archive_commit": None,
            "error": None,
            "started_at": timestamp,
            "updated_at": timestamp,
            "completed_at": None,
        }
        self.rows[row["id"]] = row
        return row

    def update(self, operation_id: str, **values: Any) -> dict[str, Any] | None:
        row = self.rows.get(operation_id)
        if row:
            row.update(values, updated_at=now_iso())
        return row

    def list_incomplete(self) -> list[dict[str, Any]]:
        return [
            row for row in self.rows.values() if row["phase"] != ArchivePhase.COMPLETED.value
        ]

    def complete(self, operation_id: str, **values: Any) -> dict[str, Any]:
        if self.fail_complete_once:
            self.fail_complete_once = False
            raise RuntimeError("simulated database interruption")
        row = self.rows[operation_id]
        task = self.tasks.rows[values["task_id"]]
        task.update(
            status=TaskStatus.ARCHIVED.value,
            workspace_path=None,
            archived_at=values["completed_at"],
            archived_by=values["actor"],
        )
        row.update(
            phase=ArchivePhase.COMPLETED.value,
            last_successful_phase=ArchivePhase.REMOVING.value,
            completed_at=values["completed_at"],
            updated_at=values["completed_at"],
            error=None,
        )
        return row


def task_record(project_id: str, task_id: str, workspace: dict[str, str]) -> dict[str, Any]:
    return {
        "id": task_id,
        "project_id": project_id,
        "title": "Archive this task",
        "raw_request": "Preserve the result",
        "spec": {
            "goal": "Preserve the result",
            "scope": ["."],
            "acceptance_criteria": ["It is recoverable"],
        },
        "status": TaskStatus.DONE.value,
        **workspace,
    }


def archive_service(
    *,
    task: dict[str, Any],
    project: dict[str, Any],
    manager: WorkspaceManager,
    fail_complete_once: bool = False,
    forbidden_values: list[str] | None = None,
) -> tuple[ArchiveService, MemoryTasks, MemoryArchives]:
    tasks = MemoryTasks([task])
    snapshots = MemorySnapshots()
    archives = MemoryArchives(tasks, fail_complete_once=fail_complete_once)
    service = ArchiveService(
        tasks=tasks,
        projects=MemoryProjects(project),
        runs=NoRuns(),
        snapshots=snapshots,
        archives=archives,
        observer=SnapshotObserver(snapshots),
        workspaces=manager,
        forbidden_values=forbidden_values,
    )
    return service, tasks, archives


def test_migration_three_preserves_beta_records_and_enables_wal(tmp_path: Path) -> None:
    database_path = tmp_path / "beta.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(MIGRATIONS[0])
    connection.executescript(MIGRATIONS[1])
    connection.executemany(
        "INSERT INTO schema_migrations(version) VALUES (?)",
        [(1,), (2,)],
    )
    connection.execute(
        """INSERT INTO projects(
            id, name, repository_path, default_branch, created_at, updated_at
        ) VALUES ('project', 'Existing', 'G:/existing', 'main', 'now', 'now')"""
    )
    connection.execute(
        """INSERT INTO tasks(
            id, project_id, title, raw_request, status, created_at, updated_at
        ) VALUES ('task', 'project', 'Existing', 'Keep me', 'DONE', 'now', 'now')"""
    )
    connection.commit()
    connection.close()

    database = Database(database_path)
    database.initialize()

    assert database.fetch_one("SELECT title FROM tasks WHERE id = 'task'") == {
        "title": "Existing"
    }
    columns = {row["name"] for row in database.fetch_all("PRAGMA table_info(tasks)")}
    assert {"archived_at", "archived_by", "source_task_id"} <= columns
    archive_columns = {
        row["name"] for row in database.fetch_all("PRAGMA table_info(archive_operations)")
    }
    assert {"phase", "last_successful_phase", "archive_ref", "archive_commit"} <= archive_columns
    with database.connect() as upgraded:
        assert upgraded.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert upgraded.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_failed_migration_rolls_back_as_one_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "atomic.db")
    database.initialize()
    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        [
            *MIGRATIONS,
            """
            CREATE TABLE must_be_rolled_back(id TEXT PRIMARY KEY);
            THIS IS NOT VALID SQL;
            """,
        ],
    )

    with pytest.raises(sqlite3.OperationalError):
        database.initialize()

    version = database.fetch_one("SELECT MAX(version) AS version FROM schema_migrations")
    assert version and version["version"] == 4
    assert database.fetch_one(
        "SELECT name FROM sqlite_master WHERE name = 'must_be_rolled_back'"
    ) is None


def test_archive_snapshot_preserves_all_non_ignored_content_without_moving_branch(
    settings: Any, fixture_repository: Path
) -> None:
    manager = WorkspaceManager(settings)
    project = {"id": "project-1", "repository_path": str(fixture_repository)}
    details = manager.prepare(project=project, task_id="task-1")
    workspace = Path(details["workspace_path"])
    (workspace / "README.md").write_text("# Staged change\n", encoding="utf-8")
    git(workspace, "add", "README.md")
    (workspace / "backend" / "feature.py").write_text("ENABLED = True\n", encoding="utf-8")
    (workspace / "asset.bin").write_bytes(b"\x00archive\xff")
    (workspace / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (workspace / "ignored.txt").write_text("do not archive", encoding="utf-8")
    status_before = git(workspace, "status", "--porcelain=v1")
    branch_before = git(fixture_repository, "rev-parse", details["branch_name"])

    snapshot = manager.create_archive_snapshot(
        project=project,
        task_id="task-1",
        workspace=workspace,
        expected_branch=details["branch_name"],
    )

    assert git(workspace, "status", "--porcelain=v1") == status_before
    assert git(fixture_repository, "rev-parse", details["branch_name"]) == branch_before
    assert git(fixture_repository, "show", f"{snapshot['archive_commit']}:README.md") == (
        "# Staged change"
    )
    assert git(
        fixture_repository, "show", f"{snapshot['archive_commit']}:backend/feature.py"
    ) == "ENABLED = True"
    binary = subprocess.run(
        ["git", "show", f"{snapshot['archive_commit']}:asset.bin"],
        cwd=fixture_repository,
        capture_output=True,
        timeout=10,
        check=True,
    ).stdout
    assert binary == b"\x00archive\xff"
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{snapshot['archive_commit']}:ignored.txt"],
        cwd=fixture_repository,
        timeout=10,
        check=False,
    ).returncode != 0


def test_archive_candidate_rejects_locked_branch_and_repository_mismatch(
    settings: Any, fixture_repository: Path, tmp_path: Path
) -> None:
    manager = WorkspaceManager(settings)
    project = {"id": "project-1", "repository_path": str(fixture_repository)}
    details = manager.prepare(project=project, task_id="task-1")
    workspace = Path(details["workspace_path"])

    with pytest.raises(WorkspaceError, match="branch mismatch"):
        manager.verify_archive_candidate(
            project=project,
            task_id="task-1",
            workspace=workspace,
            expected_branch="agent/task-someone-else",
        )

    git(fixture_repository, "worktree", "lock", str(workspace))
    with pytest.raises(WorkspaceError, match="Locked"):
        manager.verify_archive_candidate(
            project=project,
            task_id="task-1",
            workspace=workspace,
            expected_branch=details["branch_name"],
        )
    git(fixture_repository, "worktree", "unlock", str(workspace))

    other_repository = tmp_path / "other-repository"
    other_repository.mkdir()
    git(other_repository, "init", "-b", "main")
    (other_repository / "README.md").write_text("other\n", encoding="utf-8")
    git(other_repository, "config", "user.email", "tests@agent-cockpit.local")
    git(other_repository, "config", "user.name", "Agent Cockpit Tests")
    git(other_repository, "add", ".")
    git(other_repository, "commit", "-m", "Other fixture")
    wrong_project = {"id": "project-1", "repository_path": str(other_repository)}
    with pytest.raises(WorkspaceError, match="not registered|different Git repository"):
        manager.verify_archive_candidate(
            project=wrong_project,
            task_id="task-1",
            workspace=workspace,
            expected_branch=details["branch_name"],
        )


def test_archive_recovers_after_removal_before_database_completion_and_restarts(
    settings: Any, fixture_repository: Path
) -> None:
    manager = WorkspaceManager(settings)
    project = {"id": "project-1", "repository_path": str(fixture_repository)}
    details = manager.prepare(project=project, task_id="task-1")
    workspace = Path(details["workspace_path"])
    (workspace / "restored.txt").write_text("recoverable\n", encoding="utf-8")
    task = task_record(project["id"], "task-1", details)
    service, tasks, archives = archive_service(
        task=task,
        project=project,
        manager=manager,
        fail_complete_once=True,
    )

    with pytest.raises(RuntimeError, match="interruption"):
        service.archive(task["id"])
    failed = archives.get_for_task(task["id"])
    assert failed and failed["phase"] == ArchivePhase.FAILED.value
    assert failed["last_successful_phase"] == ArchivePhase.REMOVING.value
    assert tasks.get(task["id"])["status"] == TaskStatus.DONE.value  # type: ignore[index]
    assert not workspace.exists()

    completed = service.archive(task["id"])
    assert completed["phase"] == ArchivePhase.COMPLETED.value
    assert service.archive(task["id"])["id"] == completed["id"]
    assert tasks.get(task["id"])["workspace_path"] is None  # type: ignore[index]

    restarted = service.restart(task["id"])
    restarted_workspace = Path(restarted["workspace_path"])
    assert restarted["source_task_id"] == task["id"]
    assert restarted["status"] == TaskStatus.READY.value
    assert restarted_workspace != workspace
    assert (restarted_workspace / "restored.txt").read_text(encoding="utf-8") == (
        "recoverable\n"
    )
    assert tasks.get(task["id"])["status"] == TaskStatus.ARCHIVED.value  # type: ignore[index]


def test_archive_rejects_non_done_forged_paths_and_configured_secrets(
    settings: Any, fixture_repository: Path
) -> None:
    manager = WorkspaceManager(settings)
    project = {"id": "project-1", "repository_path": str(fixture_repository)}
    details = manager.prepare(project=project, task_id="task-1")
    task = task_record(project["id"], "task-1", details)
    service, _, _ = archive_service(task=task, project=project, manager=manager)
    task["status"] = TaskStatus.READY.value
    with pytest.raises(ArchiveStateError, match="DONE"):
        service.begin_archive(task["id"])

    task["status"] = TaskStatus.DONE.value
    task["workspace_path"] = str(fixture_repository)
    with pytest.raises(WorkspaceError, match="managed worktree"):
        service.begin_archive(task["id"])

    task["workspace_path"] = details["workspace_path"]
    secret = "exact-test-api-key"
    (Path(details["workspace_path"]) / "secret.txt").write_text(secret, encoding="utf-8")
    guarded, guarded_tasks, guarded_archives = archive_service(
        task=task,
        project=project,
        manager=manager,
        forbidden_values=[secret],
    )
    with pytest.raises(WorkspaceError, match="configured secret"):
        guarded.archive(task["id"])
    operation = guarded_archives.get_for_task(task["id"])
    assert operation and operation["phase"] == ArchivePhase.FAILED.value
    assert operation["archive_commit"] is None
    assert secret not in operation["error"]
    assert guarded_tasks.get(task["id"])["status"] == TaskStatus.DONE.value  # type: ignore[index]
    assert Path(details["workspace_path"]).exists()
