from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.app.main import create_app


def git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def fixture_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "fixture-repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.email", "tests@agent-cockpit.local")
    git(repository, "config", "user.name", "Agent Cockpit Tests")
    (repository / "README.md").write_text("# Fixture\n", encoding="utf-8")
    (repository / "backend").mkdir()
    (repository / "backend" / "feature.py").write_text(
        "def enabled():\n    return False\n", encoding="utf-8"
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Initial fixture")
    return repository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "cockpit-data",
        database_path=tmp_path / "cockpit-data" / "test.db",
        command_timeout_seconds=1,
        api_key="test-secret-from-environment",
        langfuse_enabled=False,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def registered_project(client: TestClient, fixture_repository: Path) -> dict[str, Any]:
    response = client.post(
        "/api/projects",
        json={
            "name": "Fixture Project",
            "repository_path": str(fixture_repository),
            "environment_spec": "Use Python and run focused tests.",
            "module_mapping": [
                {"name": "backend", "paths": ["backend/**"]},
                {"name": "docs", "paths": ["**/*.md"]},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_ready_task(client: TestClient, project_id: str) -> dict[str, Any]:
    created = client.post(
        f"/api/projects/{project_id}/tasks",
        json={"title": "Enable fixture feature", "raw_request": "Enable the fixture feature"},
    )
    assert created.status_code == 201
    refined = created.json()
    task_id = refined["id"]
    answers = [
        {
            "question_id": question["id"],
            "answer": question.get("suggested_answer") or f"Decision for {question['id']}",
        }
        for question in refined["questions"]
    ]
    if answers:
        answered = client.post(f"/api/tasks/{task_id}/answers", json={"answers": answers})
        assert answered.status_code == 200
    confirmed = client.post(f"/api/tasks/{task_id}/confirm")
    assert confirmed.status_code == 200
    return confirmed.json()


def wait_for_run(
    client: TestClient, task_id: str, run_id: str, *, timeout_seconds: float = 5
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        runs = client.get(f"/api/tasks/{task_id}/runs").json()
        run = next(item for item in runs if item["id"] == run_id)
        if run["status"] != "RUNNING":
            return run
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} did not finish within {timeout_seconds} seconds")
