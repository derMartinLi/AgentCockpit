from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.app.domain.models import AgentResult, AgentResultStatus, TaskSpec
from backend.app.main import create_app
from backend.app.services.agent import AgentAdapter, ToolBoundaryError, WorkspaceTools
from backend.tests.conftest import create_ready_task, wait_for_run


class SlowAgentAdapter(AgentAdapter):
    async def execute(
        self,
        *,
        spec: TaskSpec,
        environment_spec: str,
        tools: WorkspaceTools,
        resume_context: object | None = None,
    ) -> AgentResult:
        del resume_context
        await asyncio.sleep(1.2)
        tools.write_file("slow-agent-result.md", spec.goal)
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            summary="Slow background agent completed.",
        )


class NeedsInputAgentAdapter(AgentAdapter):
    async def execute(
        self,
        *,
        spec: TaskSpec,
        environment_spec: str,
        tools: WorkspaceTools,
        resume_context: object | None = None,
    ) -> AgentResult:
        del resume_context
        return AgentResult(
            status=AgentResultStatus.NEEDS_INPUT,
            summary="Choose whether the public API may change.",
            needs_human=True,
        )


class NodeProjectAgentAdapter(AgentAdapter):
    async def execute(
        self,
        *,
        spec: TaskSpec,
        environment_spec: str,
        tools: WorkspaceTools,
        resume_context: object | None = None,
    ) -> AgentResult:
        del resume_context
        tools.write_file(
            "package.json",
            '{"name":"generated-task","scripts":{"test":"node --test"}}',
        )
        tools.write_file(
            "test/generated.test.js",
            "const test=require('node:test');const assert=require('node:assert');"
            "test('generated',()=>assert.equal(1,1));",
        )
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            summary="Generated a Node project with repository-native tests.",
        )


class LongCommandAgentAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.control = None

    async def execute(
        self,
        *,
        spec: TaskSpec,
        environment_spec: str,
        tools: WorkspaceTools,
        resume_context: object | None = None,
    ) -> AgentResult:
        del spec, environment_spec, resume_context
        self.control = tools.control
        await asyncio.to_thread(
            tools.run_command,
            [
                sys.executable,
                "-c",
                (
                    "import os,pathlib,time; "
                    "pathlib.Path('child-started.txt').write_text(str(os.getpid())); "
                    "time.sleep(30); pathlib.Path('child-finished.txt').write_text('finished')"
                ),
            ],
        )
        return AgentResult(status=AgentResultStatus.COMPLETED, summary="Long command completed")


def git_status(repository: Path) -> str:
    return subprocess.run(
        ["git", "status", "--short"],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()


def test_workspace_is_idempotent_and_parent_repository_stays_clean(
    client: TestClient,
    registered_project: dict[str, object],
    fixture_repository: Path,
) -> None:
    task = create_ready_task(client, str(registered_project["id"]))
    first = client.post(f"/api/tasks/{task['id']}/prepare")
    second = client.post(f"/api/tasks/{task['id']}/prepare")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["workspace_path"] == second.json()["workspace_path"]
    assert first.json()["branch_name"] == f"agent/task-{task['id']}"
    assert Path(first.json()["workspace_path"]).is_dir()
    assert git_status(fixture_repository) == ""


def test_demo_agent_end_to_end(
    client: TestClient,
    registered_project: dict[str, object],
    fixture_repository: Path,
) -> None:
    task = create_ready_task(client, str(registered_project["id"]))
    response = client.post(f"/api/tasks/{task['id']}/run")
    assert response.status_code == 202, response.text
    started_run = response.json()
    assert started_run["status"] == "RUNNING"
    run = wait_for_run(client, task["id"], started_run["id"])
    assert run["status"] == "COMPLETED"
    assert run["result"]["status"] == "COMPLETED"

    completed_task = client.get(f"/api/tasks/{task['id']}").json()
    workspace = Path(completed_task["workspace_path"])
    assert completed_task["status"] == "REVIEW"
    assert (workspace / "agent-cockpit-demo.md").exists()
    assert not (fixture_repository / "agent-cockpit-demo.md").exists()
    assert git_status(fixture_repository) == ""

    events = client.get(f"/api/tasks/{task['id']}/events").json()
    event_types = [event["type"] for event in events]
    assert event_types[0] == "RefinementCompleted"
    assert "WorkspacePrepared" in event_types
    assert "AgentStarted" in event_types
    assert "AgentProgress" in event_types
    assert "FileCreated" in event_types
    assert "CommandStarted" in event_types
    assert event_types[-1] == "AgentCompleted"
    progress = [event for event in events if event["type"] == "AgentProgress"]
    assert {event["payload"]["phase"] for event in progress} == {"implementing", "checking"}
    assert all("spec" not in event["payload"] for event in progress)


def test_run_endpoint_returns_before_background_agent_finishes(
    settings: object,
    fixture_repository: Path,
) -> None:
    app = create_app(settings, adapter_override=SlowAgentAdapter())  # type: ignore[arg-type]
    with TestClient(app) as background_client:
        project = background_client.post(
            "/api/projects",
            json={"name": "Background", "repository_path": str(fixture_repository)},
        ).json()
        task = create_ready_task(background_client, project["id"])

        started_at = time.monotonic()
        response = background_client.post(f"/api/tasks/{task['id']}/run")
        elapsed = time.monotonic() - started_at

        assert response.status_code == 202
        assert response.json()["status"] == "RUNNING"
        assert elapsed < 0.9
        assert background_client.get(f"/api/tasks/{task['id']}").json()["status"] == "RUNNING"
        assert background_client.post(f"/api/tasks/{task['id']}/run").status_code == 409

        completed = wait_for_run(
            background_client, task["id"], response.json()["id"], timeout_seconds=4
        )
        assert completed["status"] == "COMPLETED"


def test_runtime_auto_discovers_and_runs_checks_created_by_agent(
    settings: Settings,
    fixture_repository: Path,
) -> None:
    app = create_app(
        settings.model_copy(update={"command_timeout_seconds": 5}),
        adapter_override=NodeProjectAgentAdapter(),
    )
    with TestClient(app) as automatic_client:
        project = automatic_client.post(
            "/api/projects",
            json={"name": "Dynamic checks", "repository_path": str(fixture_repository)},
        ).json()
        task = create_ready_task(automatic_client, project["id"])
        started = automatic_client.post(f"/api/tasks/{task['id']}/run").json()
        run = wait_for_run(automatic_client, task["id"], started["id"], timeout_seconds=8)
        checks = automatic_client.get(f"/api/tasks/{task['id']}/checks").json()

        assert run["status"] == "COMPLETED"
        node_check = next(check for check in checks if check["name"] == "Node test")
        expected_runner = "npm.cmd" if __import__("os").name == "nt" else "npm"
        assert node_check["command"] == [expected_runner, "run", "test"]
        assert node_check["status"] == "passed"
        assert node_check["exit_code"] == 0


def test_workspace_tools_reject_escape_shell_and_git_lifecycle(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events: list[tuple[str, dict[str, object]]] = []
    tools = WorkspaceTools(
        workspace,
        timeout_seconds=1,
        emit=lambda event_type, payload: events.append((event_type, payload)),
    )

    with pytest.raises(ToolBoundaryError, match="outside"):
        tools.read_file("../secret.txt")
    with pytest.raises(ToolBoundaryError, match="Shell"):
        tools.run_command(["powershell", "-Command", "Get-ChildItem"])
    with pytest.raises(ToolBoundaryError, match="lifecycle"):
        tools.run_command(["git", "reset", "--hard"])
    rejected = [payload for event_type, payload in events if event_type == "ToolRejected"]
    assert len(rejected) == 3
    assert {item["tool"] for item in rejected} == {"path", "run_command"}


def test_workspace_tools_enforce_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tools = WorkspaceTools(workspace, timeout_seconds=1, emit=lambda *_: None)
    with pytest.raises(ToolBoundaryError, match="timeout"):
        tools.run_command(
            [
                str(Path(__import__("sys").executable)),
                "-c",
                "import time; time.sleep(2)",
            ]
        )


def test_workspace_tools_expose_project_checks_and_tolerate_missing_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "package.json").write_text(
        '{"scripts":{"test":"node --test"}}', encoding="utf-8"
    )
    tools = WorkspaceTools(workspace, timeout_seconds=1, emit=lambda *_: None)
    assert "node-test" in tools.list_project_checks()

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout: float) -> tuple[None, str]:
            return None, "stderr-only"

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(
        "backend.app.services.agent.subprocess.Popen", lambda *args, **kwargs: FakeProcess()
    )
    assert tools.run_command(["fake-command"]) == "exit_code=0\nstderr-only"


def test_alpha_flow_is_repeatable_three_times(
    client: TestClient,
    registered_project: dict[str, object],
    fixture_repository: Path,
) -> None:
    workspaces: set[str] = set()
    for _ in range(3):
        task = create_ready_task(client, str(registered_project["id"]))
        response = client.post(f"/api/tasks/{task['id']}/run")
        assert response.status_code == 202, response.text
        wait_for_run(client, task["id"], response.json()["id"])
        completed = client.get(f"/api/tasks/{task['id']}").json()
        assert completed["status"] == "REVIEW"
        assert Path(completed["workspace_path"], "agent-cockpit-demo.md").exists()
        workspaces.add(completed["workspace_path"])

    assert len(workspaces) == 3
    assert git_status(fixture_repository) == ""


def test_workspace_evidence_review_and_resume_are_persisted(
    client: TestClient,
    registered_project: dict[str, object],
    settings: Settings,
) -> None:
    task = create_ready_task(client, str(registered_project["id"]))
    started = client.post(f"/api/tasks/{task['id']}/run").json()
    wait_for_run(client, task["id"], started["id"])

    workspace_response = client.get(f"/api/tasks/{task['id']}/workspace")
    assert workspace_response.status_code == 200, workspace_response.text
    workspace = workspace_response.json()
    assert workspace["identity"]["verified"] is True
    assert workspace["identity"]["branch"] == f"agent/task-{task['id']}"
    assert any(item["path"] == "agent-cockpit-demo.md" for item in workspace["files"])
    assert "Agent Cockpit Observable Result" in workspace["diff"]

    external = Path(workspace["identity"]["workspace_path"], "external-change.txt")
    external.write_text(
        "changed outside the agent\ntest-secret-from-environment\n", encoding="utf-8"
    )
    refreshed = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert any(item["path"] == "external-change.txt" for item in refreshed["files"])
    assert "test-secret-from-environment" not in refreshed["diff"]
    for database_file in settings.resolved_database_path.parent.glob(
        f"{settings.resolved_database_path.name}*"
    ):
        assert b"test-secret-from-environment" not in database_file.read_bytes()
    events = client.get(f"/api/tasks/{task['id']}/events").json()
    assert any(
        event["source"] == "workspace"
        and event["type"] == "FileCreated"
        and event["payload"]["path"] == "external-change.txt"
        for event in events
    )
    after = client.get(f"/api/tasks/{task['id']}/events?after={events[-2]['id']}").json()
    assert all(event["id"] > events[-2]["id"] for event in after)
    with client.websocket_connect(
        f"/api/projects/{registered_project['id']}/events?after=0"
    ) as websocket:
        assert websocket.receive_json()["task_id"] == task["id"]

    checks = client.get(f"/api/tasks/{task['id']}/checks").json()
    assert checks[0]["name"] == "git status"
    review = client.get(f"/api/tasks/{task['id']}/review").json()
    assert review["workspace"]["files"] == refreshed["files"]

    rejected = client.post(f"/api/tasks/{task['id']}/review/reject", json={})
    assert rejected.json()["status"] == "NEEDS_YOU"
    resumed = client.post(
        f"/api/tasks/{task['id']}/resume",
        json={"instruction": "Only address the failed verification and preserve existing work."},
    )
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["previous_run_id"] == started["id"]
    context = Path(workspace["identity"]["workspace_path"], ".task", "context.md")
    assert context.exists()
    context_text = context.read_text(encoding="utf-8")
    assert "test-secret-from-environment" not in context_text
    assert "Only address the failed verification and preserve existing work." in context_text
    wait_for_run(client, task["id"], resumed.json()["id"])

    completed = client.post(f"/api/tasks/{task['id']}/review/done", json={})
    assert completed.json()["status"] == "DONE"
    done_workspace = completed.json()["workspace_path"]
    continued = client.post(f"/api/tasks/{task['id']}/resume")
    assert continued.status_code == 202, continued.text
    assert continued.json()["previous_run_id"] == resumed.json()["id"]
    assert client.get(f"/api/tasks/{task['id']}").json()["workspace_path"] == done_workspace
    wait_for_run(client, task["id"], continued.json()["id"])
    completed_again = client.post(f"/api/tasks/{task['id']}/review/done", json={})
    assert completed_again.json()["status"] == "DONE"
    decisions = client.get(f"/api/tasks/{task['id']}/review").json()["decisions"]
    assert [item["decision"] for item in decisions] == [
        "REJECT",
        "RESUME",
        "DONE",
        "RESUME",
        "DONE",
    ]
    assert decisions[1]["reason"] == (
        "Only address the failed verification and preserve existing work."
    )


def test_running_agent_can_be_cancelled_and_resumed(
    settings: object,
    fixture_repository: Path,
) -> None:
    app = create_app(settings, adapter_override=SlowAgentAdapter())  # type: ignore[arg-type]
    with TestClient(app) as background_client:
        project = background_client.post(
            "/api/projects",
            json={"name": "Cancellation", "repository_path": str(fixture_repository)},
        ).json()
        task = create_ready_task(background_client, project["id"])
        started = background_client.post(f"/api/tasks/{task['id']}/run").json()

        cancelled = background_client.post(f"/api/runs/{started['id']}/cancel")
        assert cancelled.status_code == 202
        finished = wait_for_run(background_client, task["id"], started["id"])
        assert finished["status"] == "CANCELLED"
        cancelled_task = background_client.get(f"/api/tasks/{task['id']}").json()
        assert cancelled_task["status"] == "NEEDS_YOU"
        workspace = Path(cancelled_task["workspace_path"])
        assert workspace.exists()
        assert not (workspace / "slow-agent-result.md").exists()

        resumed = background_client.post(f"/api/tasks/{task['id']}/resume")
        assert resumed.status_code == 202
        assert resumed.json()["previous_run_id"] == started["id"]
        assert (workspace / ".task" / "context.md").exists()
        completed = wait_for_run(
            background_client, task["id"], resumed.json()["id"], timeout_seconds=4
        )
        assert completed["status"] == "COMPLETED"


def test_cancel_terminates_a_real_long_running_child_process(
    settings: object,
    fixture_repository: Path,
) -> None:
    adapter = LongCommandAgentAdapter()
    app = create_app(settings, adapter_override=adapter)  # type: ignore[arg-type]
    with TestClient(app) as command_client:
        project = command_client.post(
            "/api/projects",
            json={"name": "Long command", "repository_path": str(fixture_repository)},
        ).json()
        task = create_ready_task(command_client, project["id"])
        started = command_client.post(f"/api/tasks/{task['id']}/run").json()
        task_state = command_client.get(f"/api/tasks/{task['id']}").json()
        workspace = Path(task_state["workspace_path"])
        deadline = time.monotonic() + 5
        while not (workspace / "child-started.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (workspace / "child-started.txt").exists()

        cancelled = command_client.post(f"/api/runs/{started['id']}/cancel")
        assert cancelled.status_code == 202
        finished = wait_for_run(command_client, task["id"], started["id"])
        assert finished["status"] == "CANCELLED"
        deadline = time.monotonic() + 3
        while adapter.control and adapter.control._processes and time.monotonic() < deadline:
            time.sleep(0.02)
        assert adapter.control is not None
        assert not adapter.control._processes
        assert not (workspace / "child-finished.txt").exists()


def test_needs_input_is_answerable_and_auditable(
    settings: object,
    fixture_repository: Path,
) -> None:
    app = create_app(settings, adapter_override=NeedsInputAgentAdapter())  # type: ignore[arg-type]
    with TestClient(app) as input_client:
        project = input_client.post(
            "/api/projects",
            json={"name": "Input", "repository_path": str(fixture_repository)},
        ).json()
        task = create_ready_task(input_client, project["id"])
        started = input_client.post(f"/api/tasks/{task['id']}/run").json()
        wait_for_run(input_client, task["id"], started["id"])
        pending = input_client.get(f"/api/tasks/{task['id']}/inputs").json()
        assert pending[0]["status"] == "PENDING"
        answered = input_client.post(
            f"/api/tasks/{task['id']}/input",
            json={"answer": "Keep the public API backward-compatible."},
        )
        assert answered.status_code == 200
        assert answered.json()["status"] == "ANSWERED"
