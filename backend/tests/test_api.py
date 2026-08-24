from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.app.core.credentials import CredentialStore
from backend.app.core.database import MIGRATIONS, Database
from backend.app.main import create_app
from backend.app.repositories.store import CheckRepository
from backend.app.services.projects import ProjectValidationError, normalize_local_path
from backend.tests.conftest import create_ready_task


def test_health_initializes_database(client: TestClient, settings: Settings) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "migration-4"}
    assert settings.resolved_database_path.exists()


def test_beta_migration_preserves_alpha_data(tmp_path: Path) -> None:
    database_path = tmp_path / "alpha.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(MIGRATIONS[0])
    connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
    connection.execute(
        """INSERT INTO projects(
            id, name, repository_path, default_branch, created_at, updated_at
        ) VALUES ('project', 'Existing', 'G:/existing', 'main', 'now', 'now')"""
    )
    connection.commit()
    connection.close()

    database = Database(database_path)
    database.initialize()

    existing = database.fetch_one("SELECT name FROM projects WHERE id = 'project'")
    assert existing and existing["name"] == "Existing"
    assert database.fetch_one("SELECT MAX(version) AS version FROM schema_migrations")[  # type: ignore[index]
        "version"
    ] == 4
    columns = database.fetch_all("PRAGMA table_info(agent_runs)")
    assert "previous_run_id" in {column["name"] for column in columns}


def test_startup_marks_orphaned_running_runs_resumable(
    settings: Settings, fixture_repository: Path
) -> None:
    first_app = create_app(settings)
    with TestClient(first_app) as first_client:
        project = first_client.post(
            "/api/projects",
            json={"name": "Recovery", "repository_path": str(fixture_repository)},
        ).json()
        task = first_app.state.container.tasks.create(
            project_id=project["id"], title="Interrupted", raw_request="Recover me"
        )
        run = first_app.state.container.runs.create(task["id"])
        first_app.state.container.tasks.update(task["id"], status="RUNNING")

    second_app = create_app(settings)
    with TestClient(second_app) as second_client:
        recovered_run = second_client.get(f"/api/runs/{run['id']}").json()
        recovered_task = second_client.get(f"/api/tasks/{task['id']}").json()
        assert recovered_run["status"] == "FAILED"
        assert recovered_task["status"] == "NEEDS_YOU"


def test_project_validation_and_persistence(
    client: TestClient, settings: Settings, fixture_repository: Path
) -> None:
    invalid = client.post(
        "/api/projects",
        json={"name": "Missing", "repository_path": str(fixture_repository / "missing")},
    )
    assert invalid.status_code == 422

    created = client.post(
        "/api/projects",
        json={
            "name": "Fixture",
            "repository_path": str(fixture_repository),
            "environment_spec": "Run pytest.",
            "module_mapping": [{"name": "backend", "paths": ["backend/**"]}],
        },
    )
    assert created.status_code == 201
    assert created.json()["default_branch"] == "main"

    second_app = create_app(settings)
    with TestClient(second_app) as second_client:
        projects = second_client.get("/api/projects").json()
        assert len(projects) == 1
        assert projects[0]["environment_spec"] == "Run pytest."


def test_repository_path_normalization_uses_standard_library_rules(
    fixture_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = fixture_repository.resolve()
    assert normalize_local_path(f'"{fixture_repository}"') == expected
    assert normalize_local_path(str(fixture_repository).replace("\\", "/")) == expected
    assert (
        normalize_local_path(fixture_repository.name, base_directory=fixture_repository.parent)
        == expected
    )

    monkeypatch.setenv("COCKPIT_TEST_REPOSITORY", str(fixture_repository))
    environment_syntax = (
        "%COCKPIT_TEST_REPOSITORY%" if os.name == "nt" else "$COCKPIT_TEST_REPOSITORY"
    )
    assert normalize_local_path(environment_syntax) == expected


def test_repository_path_normalization_rejects_ambiguous_input() -> None:
    with pytest.raises(ProjectValidationError, match="unmatched quotes"):
        normalize_local_path('"G:\\Projects\\repo')
    with pytest.raises(ProjectValidationError, match="file://"):
        normalize_local_path("file:///G:/Projects/repo")
    with pytest.raises(ProjectValidationError, match="Drive-relative"):
        normalize_local_path("G:Projects\\repo")


def test_project_api_returns_canonical_repository_path(
    client: TestClient, fixture_repository: Path
) -> None:
    response = client.post(
        "/api/projects",
        json={
            "name": "Quoted path",
            "repository_path": f'"{str(fixture_repository).replace(chr(92), "/")}"',
        },
    )
    assert response.status_code == 201, response.text
    assert Path(response.json()["repository_path"]) == fixture_repository.resolve()


def test_project_detects_and_persists_repository_verification(
    client: TestClient, fixture_repository: Path
) -> None:
    (fixture_repository / "package.json").write_text(
        '{"scripts":{"test":"node --test","lint":"eslint ."},'
        '"devDependencies":{"vitest":"latest"}}',
        encoding="utf-8",
    )
    response = client.post(
        "/api/projects",
        json={"name": "Node checks", "repository_path": str(fixture_repository)},
    )
    assert response.status_code == 201, response.text
    project = response.json()
    assert "Vitest" in project["repository_profile"]["frameworks"]
    assert {item["id"] for item in project["verification_commands"]} == {
        "node-test",
        "node-lint",
    }
    updated = client.put(
        f"/api/projects/{project['id']}",
        json={
            "verification_commands": [
                {
                    "id": "node-test",
                    "name": "Focused Node tests",
                    "kind": "test",
                    "command": ["npm", "run", "test"],
                    "cwd": ".",
                    "auto_run": True,
                    "source": "user",
                }
            ]
        },
    )
    assert updated.status_code == 200
    assert updated.json()["verification_commands"][0]["name"] == "Focused Node tests"


def test_module_preview(client: TestClient, registered_project: dict[str, object]) -> None:
    project_id = registered_project["id"]
    backend = client.post(
        f"/api/projects/{project_id}/module-preview", json={"file_path": "backend/auth/token.py"}
    )
    docs = client.post(
        f"/api/projects/{project_id}/module-preview", json={"file_path": "guides/setup.md"}
    )
    assert backend.json()["modules"] == ["backend"]
    assert docs.json()["modules"] == ["docs"]


def test_check_failures_explain_runner_and_command_configuration_errors() -> None:
    runner = CheckRepository._diagnose(
        {
            "status": "failed",
            "exit_code": None,
            "output_excerpt": "unsupported operand type(s) for +: 'NoneType' and 'str'",
            "command": ["node", "--test"],
        }
    )
    invalid = CheckRepository._diagnose(
        {
            "status": "failed",
            "exit_code": 1,
            "output_excerpt": "Error: Cannot find module 'workspace/test'",
            "command": ["node", "--test", "test/"],
        }
    )
    assert runner["failure_kind"] == "RUNNER_ERROR"
    assert invalid["failure_kind"] == "COMMAND_INVALID"
    assert "Project Settings" in str(invalid["suggested_action"])


def test_task_refinement_and_state_guards(
    client: TestClient, registered_project: dict[str, object]
) -> None:
    project_id = registered_project["id"]
    created = client.post(
        f"/api/projects/{project_id}/tasks",
        json={"title": "Remember me", "raw_request": "Add remember me to login"},
    ).json()
    assert created["status"] == "DRAFT"
    assert created["runtime_phase"] is None
    assert created["inspection"]["refinement"]["questions_required"] == 0
    assert created["spec"]["goal"] == "Add remember me to login"

    confirmed = client.post(f"/api/tasks/{created['id']}/confirm").json()
    assert confirmed["status"] == "READY"
    assert client.post(f"/api/tasks/{created['id']}/refine").status_code == 409

    vague = client.post(
        f"/api/projects/{project_id}/tasks",
        json={"title": "Vague", "raw_request": "优化一下"},
    ).json()
    assert vague["runtime_phase"] == "REFINING"
    assert len(vague["questions"]) == 1
    assert vague["questions"][0]["suggested_answer"]
    answered = client.post(
        f"/api/tasks/{vague['id']}/answers",
        json={
            "answers": [
                {
                    "question_id": vague["questions"][0]["id"],
                    "answer": "登录成功后保持会话",
                }
            ]
        },
    ).json()
    assert "登录成功后保持会话" in answered["spec"]["acceptance_criteria"]


def test_api_key_is_not_persisted(
    client: TestClient, settings: Settings, registered_project: dict[str, object]
) -> None:
    provider = client.get("/api/settings/provider")
    assert provider.status_code == 200
    assert provider.json()["has_api_key"] is True
    database_bytes = settings.resolved_database_path.read_bytes()
    assert b"test-secret-from-environment" not in database_bytes


def test_provider_api_key_can_load_from_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_COCKPIT_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("AGENT_COCKPIT_API_KEY=dotenv-secret\n", encoding="utf-8")
    dotenv_settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert dotenv_settings.api_key == "dotenv-secret"
    assert CredentialStore(dotenv_settings).get_api_key() == "dotenv-secret"


def test_ready_task_helper(client: TestClient, registered_project: dict[str, object]) -> None:
    task = create_ready_task(client, str(registered_project["id"]))
    assert task["status"] == "READY"
    assert task["spec"]["acceptance_criteria"]


def test_refinement_has_a_hard_two_round_limit(
    client: TestClient, registered_project: dict[str, object]
) -> None:
    created = client.post(
        f"/api/projects/{registered_project['id']}/tasks",
        json={"title": "Two rounds", "raw_request": "Change a behavior safely"},
    ).json()
    assert created["refinement_round"] == 1
    second = client.post(f"/api/tasks/{created['id']}/refine")
    assert second.status_code == 200
    assert second.json()["refinement_round"] == 2
    assert client.post(f"/api/tasks/{created['id']}/refine").status_code == 409
