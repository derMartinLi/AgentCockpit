from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.tests.conftest import create_ready_task, wait_for_run


def wait_for_archive(
    client: TestClient, operation_id: str, *, timeout_seconds: float = 10
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        operation = client.get(f"/api/archive-operations/{operation_id}")
        assert operation.status_code == 200, operation.text
        payload = operation.json()
        if payload["phase"] == "COMPLETED":
            return payload
        if payload["phase"] == "FAILED":
            raise AssertionError(payload["error"])
        time.sleep(0.02)
    raise AssertionError(f"Archive {operation_id} did not finish")


def test_v1_archive_api_preserves_history_reclaims_workspace_and_restarts(
    client: TestClient,
    registered_project: dict[str, object],
    fixture_repository: Path,
    settings: Settings,
) -> None:
    task = create_ready_task(client, str(registered_project["id"]))
    started = client.post(f"/api/tasks/{task['id']}/run")
    assert started.status_code == 202, started.text
    wait_for_run(client, task["id"], started.json()["id"])
    completed = client.post(f"/api/tasks/{task['id']}/review/done", json={})
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "DONE"
    original_workspace = Path(completed.json()["workspace_path"])
    assert original_workspace.is_dir()

    requested = client.post(f"/api/tasks/{task['id']}/archive")
    assert requested.status_code == 202, requested.text
    operation = wait_for_archive(client, requested.json()["id"])
    assert operation["archive_ref"] == f"refs/agent-cockpit/archives/{task['id']}"
    assert operation["archive_commit"]
    assert operation["final_snapshot_id"]
    assert not original_workspace.exists()

    archived = client.get(f"/api/tasks/{task['id']}").json()
    assert archived["status"] == "ARCHIVED"
    assert archived["workspace_path"] is None
    assert archived["archive_operation_id"] == operation["id"]
    assert archived["archive_commit"] == operation["archive_commit"]
    assert archived["archived_by"] == "local-user"
    assert client.post(f"/api/tasks/{task['id']}/resume").status_code == 409

    review = client.get(f"/api/tasks/{task['id']}/review")
    assert review.status_code == 200, review.text
    assert any(
        item["path"] == "agent-cockpit-demo.md"
        for item in review.json()["workspace"]["files"]
    )
    persisted_workspace = client.get(f"/api/tasks/{task['id']}/workspace")
    assert persisted_workspace.status_code == 200
    assert persisted_workspace.json() == review.json()["workspace"]
    events = client.get(f"/api/tasks/{task['id']}/events").json()
    assert events[-1]["type"] == "TaskArchived"
    assert events[-1]["payload"]["workspace_removed"] is True
    assert settings.api_key
    assert settings.api_key not in str(events)
    assert settings.api_key not in str(review.json())
    for database_file in settings.resolved_database_path.parent.glob(
        f"{settings.resolved_database_path.name}*"
    ):
        assert settings.api_key.encode() not in database_file.read_bytes()
    archive_secret_scan = subprocess.run(
        [
            "git",
            "grep",
            "-I",
            "-F",
            settings.api_key,
            operation["archive_commit"],
        ],
        cwd=fixture_repository,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert archive_secret_scan.returncode == 1

    duplicate = client.post(f"/api/tasks/{task['id']}/archive")
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == operation["id"]
    assert duplicate.json()["phase"] == "COMPLETED"

    restarted_response = client.post(f"/api/tasks/{task['id']}/restart")
    assert restarted_response.status_code == 201, restarted_response.text
    restarted = restarted_response.json()
    assert restarted["id"] != task["id"]
    assert restarted["source_task_id"] == task["id"]
    assert restarted["status"] == "READY"
    assert restarted["workspace_path"] != str(original_workspace)
    assert Path(restarted["workspace_path"], "agent-cockpit-demo.md").is_file()
    assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "ARCHIVED"

    cockpit = client.get(f"/api/projects/{registered_project['id']}/cockpit")
    assert cockpit.status_code == 200, cockpit.text
    sections = cockpit.json()["sections"]
    assert any(item["task"]["id"] == task["id"] for item in sections["archived"])
    assert any(item["task"]["id"] == restarted["id"] for item in sections["active"])


def test_project_cockpit_route_and_safe_project_delete(
    client: TestClient,
    fixture_repository: Path,
) -> None:
    created = client.post(
        "/api/projects",
        json={"name": "Disposable", "repository_path": str(fixture_repository)},
    )
    assert created.status_code == 201
    project_id = created.json()["id"]
    cockpit = client.get(f"/api/projects/{project_id}/cockpit")
    assert cockpit.status_code == 200
    assert cockpit.json()["capacity"]["limit"] == 3
    assert cockpit.json()["sections"] == {
        "active": [],
        "needs_you": [],
        "review": [],
        "done": [],
        "archived": [],
    }
    deleted = client.delete(f"/api/projects/{project_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/projects/{project_id}").status_code == 404


def test_project_delete_refuses_to_orphan_task_history(
    client: TestClient,
    registered_project: dict[str, object],
    fixture_repository: Path,
) -> None:
    created = client.post(
        f"/api/projects/{registered_project['id']}/tasks",
        json={"title": "Keep history", "raw_request": "Keep this task history"},
    )
    assert created.status_code == 201
    deleted = client.delete(f"/api/projects/{registered_project['id']}")
    assert deleted.status_code == 409
    assert client.get(f"/api/projects/{registered_project['id']}").status_code == 200
    assert fixture_repository.is_dir()
