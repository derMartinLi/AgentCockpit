from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.app.domain.models import (
    AgentCheck,
    AgentResult,
    AgentResultStatus,
    Event,
    TaskSpec,
    now_iso,
)
from backend.app.main import create_app
from backend.app.services.agent import AgentAdapter, WorkspaceTools
from backend.tests.conftest import create_ready_task, wait_for_run
from backend.tests.test_v1_release_api import wait_for_archive


class RoundBarrierAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.release = threading.Event()

    async def execute(
        self,
        *,
        spec: TaskSpec,
        environment_spec: str,
        tools: WorkspaceTools,
        resume_context: object | None = None,
    ) -> AgentResult:
        del resume_context
        del environment_spec
        await asyncio.to_thread(self.release.wait, 5)
        tools.write_file("v1-result.txt", f"Completed {spec.goal}\n")
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            summary=f"Completed {spec.goal}",
            checks=[AgentCheck(name="v1 fixture", status="passed")],
        )


def test_three_concurrent_tasks_complete_three_release_rounds(
    settings: Settings,
    fixture_repository: Path,
) -> None:
    adapter = RoundBarrierAdapter()
    app = create_app(settings, adapter_override=adapter)
    with TestClient(app) as client:
        project = client.post(
            "/api/projects",
            json={
                "name": "V1 soak",
                "repository_path": str(fixture_repository),
                "module_mapping": [{"name": "backend", "paths": ["backend/**"]}],
            },
        ).json()
        completed_ids: list[str] = []
        for round_number in range(3):
            adapter.release = threading.Event()
            tasks = [create_ready_task(client, project["id"]) for _ in range(3)]
            runs = [client.post(f"/api/tasks/{task['id']}/run") for task in tasks]
            assert all(response.status_code == 202 for response in runs)
            cockpit = client.get(f"/api/projects/{project['id']}/cockpit").json()
            assert cockpit["capacity"] == {"limit": 3, "running": 3, "available": 0}
            adapter.release.set()
            for task, run_response in zip(tasks, runs, strict=True):
                run = wait_for_run(
                    client, task["id"], run_response.json()["id"], timeout_seconds=8
                )
                assert run["status"] == "COMPLETED"
                reviewed = client.get(f"/api/tasks/{task['id']}/review").json()
                assert reviewed["workspace"]["identity"]["verified"] is True
                assert reviewed["checks"][0]["status"] == "passed"
                done = client.post(f"/api/tasks/{task['id']}/review/done", json={})
                assert done.json()["status"] == "DONE"
                completed_ids.append(task["id"])
            assert len(completed_ids) == (round_number + 1) * 3

        final_cockpit = client.get(f"/api/projects/{project['id']}/cockpit").json()
        assert len(final_cockpit["sections"]["done"]) == 9
        assert final_cockpit["capacity"]["running"] == 0


def test_invalid_provider_credentials_fail_one_task_without_losing_workspace(
    settings: Settings,
    fixture_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.app.core.credentials.keyring.get_password", lambda *_: None)
    no_key_settings = settings.model_copy(update={"api_key": None})
    app = create_app(no_key_settings)
    with TestClient(app) as client:
        project = client.post(
            "/api/projects",
            json={"name": "No key", "repository_path": str(fixture_repository)},
        ).json()
        app.state.container.providers.save(
            provider="openai-compatible",
            model="unavailable-model",
            base_url=None,
            has_api_key=False,
        )
        task = create_ready_task(client, project["id"])
        started = client.post(f"/api/tasks/{task['id']}/run")
        assert started.status_code == 202
        failed = wait_for_run(client, task["id"], started.json()["id"])
        assert failed["status"] == "FAILED"
        failed_task = client.get(f"/api/tasks/{task['id']}").json()
        assert failed_task["status"] == "FAILED"
        assert Path(failed_task["workspace_path"]).is_dir()
        assert "API Key" in failed["summary"]


def test_worktree_conflict_is_audited_and_does_not_touch_repository(
    client: TestClient,
    registered_project: dict[str, object],
    fixture_repository: Path,
    settings: Settings,
) -> None:
    task = create_ready_task(client, str(registered_project["id"]))
    conflict = (
        settings.worktrees_dir
        / str(registered_project["id"])
        / f"task-{task['id']}"
    )
    conflict.mkdir(parents=True)
    (conflict / "not-a-worktree.txt").write_text("collision", encoding="utf-8")

    prepared = client.post(f"/api/tasks/{task['id']}/prepare")
    assert prepared.status_code == 422
    assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "FAILED"
    events = client.get(f"/api/tasks/{task['id']}/events").json()
    assert events[-1]["type"] == "WorkspacePreparationFailed"
    assert (fixture_repository / "README.md").read_text(encoding="utf-8") == "# Fixture\n"


def test_archive_remove_failure_preserves_other_task_and_is_retryable(
    client: TestClient,
    registered_project: dict[str, object],
    fixture_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = [create_ready_task(client, str(registered_project["id"])) for _ in range(2)]
    workspaces: list[Path] = []
    for task in tasks:
        started = client.post(f"/api/tasks/{task['id']}/run").json()
        wait_for_run(client, task["id"], started["id"])
        done = client.post(f"/api/tasks/{task['id']}/review/done", json={}).json()
        workspaces.append(Path(done["workspace_path"]))

    container = cast(FastAPI, client.app).state.container
    manager = container.task_runtime.workspaces
    original_git = manager._git

    def failing_git(
        cwd: Path, arguments: list[str], *, env: dict[str, str] | None = None
    ) -> str:
        if arguments[:3] == ["worktree", "remove", "--force"]:
            raise subprocess.CalledProcessError(1, ["git", *arguments])
        return original_git(cwd, arguments, env=env)

    monkeypatch.setattr(manager, "_git", failing_git)
    requested = client.post(f"/api/tasks/{tasks[0]['id']}/archive")
    operation_id = requested.json()["id"]
    deadline = time.monotonic() + 8
    failed: dict[str, object] | None = None
    while time.monotonic() < deadline:
        candidate = client.get(f"/api/archive-operations/{operation_id}").json()
        if candidate["phase"] == "FAILED":
            failed = candidate
            break
        time.sleep(0.02)
    assert failed and "WorkspaceError" in str(failed["error"])
    assert client.get(f"/api/tasks/{tasks[0]['id']}").json()["status"] == "DONE"
    assert all(workspace.exists() for workspace in workspaces)
    assert fixture_repository.is_dir()

    monkeypatch.setattr(manager, "_git", original_git)
    deadline = time.monotonic() + 2
    while container.archive_jobs and time.monotonic() < deadline:
        time.sleep(0.02)
    retried = client.post(f"/api/tasks/{tasks[0]['id']}/archive")
    completed = wait_for_archive(client, retried.json()["id"])
    assert completed["phase"] == "COMPLETED"
    assert not workspaces[0].exists()
    assert workspaces[1].exists()


def test_sqlite_wal_handles_concurrent_event_writers(
    client: TestClient,
    registered_project: dict[str, object],
) -> None:
    container = cast(FastAPI, client.app).state.container
    task = container.tasks.create(
        project_id=registered_project["id"], title="Concurrent writes", raw_request="Write"
    )

    def write_event(index: int) -> None:
        container.events.record(
            Event(
                timestamp=now_iso(),
                task_id=task["id"],
                run_id=None,
                type="ConcurrentWrite",
                source="test",
                payload={"index": index},
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write_event, range(80)))
    events = container.events.list_for_task(task["id"])
    assert len(events) == 80
    assert {event["payload"]["index"] for event in events} == set(range(80))


def test_archive_api_rejects_configured_secret_and_succeeds_after_cleanup(
    client: TestClient,
    registered_project: dict[str, object],
    settings: Settings,
) -> None:
    task = create_ready_task(client, str(registered_project["id"]))
    started = client.post(f"/api/tasks/{task['id']}/run").json()
    wait_for_run(client, task["id"], started["id"])
    done = client.post(f"/api/tasks/{task['id']}/review/done", json={}).json()
    secret_file = Path(done["workspace_path"], "accidental-secret.txt")
    assert settings.api_key
    secret_file.write_text(settings.api_key, encoding="utf-8")

    requested = client.post(f"/api/tasks/{task['id']}/archive").json()
    deadline = time.monotonic() + 8
    failed: dict[str, object] | None = None
    while time.monotonic() < deadline:
        operation = client.get(f"/api/archive-operations/{requested['id']}").json()
        if operation["phase"] == "FAILED":
            failed = operation
            break
        time.sleep(0.02)
    assert failed
    assert settings.api_key not in str(failed["error"])
    assert "configured secret" in str(failed["error"])
    assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "DONE"
    assert secret_file.exists()

    secret_file.unlink()
    retried = client.post(f"/api/tasks/{task['id']}/archive").json()
    assert wait_for_archive(client, retried["id"])["phase"] == "COMPLETED"


def test_restart_replays_tasks_snapshots_events_checks_and_review(
    settings: Settings,
    fixture_repository: Path,
) -> None:
    first_app = create_app(settings)
    with TestClient(first_app) as first_client:
        project = first_client.post(
            "/api/projects",
            json={"name": "Replay", "repository_path": str(fixture_repository)},
        ).json()
        task = create_ready_task(first_client, project["id"])
        started = first_client.post(f"/api/tasks/{task['id']}/run").json()
        wait_for_run(first_client, task["id"], started["id"])
        first_client.post(f"/api/tasks/{task['id']}/review/done", json={})
        before_review = first_client.get(f"/api/tasks/{task['id']}/review").json()
        before_events = first_client.get(f"/api/tasks/{task['id']}/events").json()

    second_app = create_app(settings)
    with TestClient(second_app) as second_client:
        restored_task = second_client.get(f"/api/tasks/{task['id']}").json()
        restored_review = second_client.get(f"/api/tasks/{task['id']}/review").json()
        restored_events = second_client.get(f"/api/tasks/{task['id']}/events").json()
        assert restored_task["status"] == "DONE"
        assert restored_review["workspace"] == before_review["workspace"]
        assert restored_review["checks"] == before_review["checks"]
        assert restored_review["decisions"] == before_review["decisions"]
        assert restored_events == before_events
