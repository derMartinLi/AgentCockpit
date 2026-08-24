from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from backend.app.container import build_container
from backend.app.core.config import Settings
from backend.app.domain.models import AgentResult, AgentResultStatus, Event, TaskSpec
from backend.app.services.agent import AgentAdapter, WorkspaceTools
from backend.app.services.cockpit import CockpitProjector
from backend.app.services.tasks import RunCapacityError


def _snapshot_payload(
    captured_at: str, *files: tuple[str, int, int, list[str]]
) -> dict[str, Any]:
    changes = [
        {
            "path": path,
            "status": "Modified",
            "added": added,
            "deleted": deleted,
            "modules": modules,
        }
        for path, added, deleted, modules in files
    ]
    return {
        "captured_at": captured_at,
        "identity": {"verified": True},
        "files": changes,
        "modules": sorted({module for item in changes for module in item["modules"]}),
        "diff": "",
    }


def test_cockpit_sections_capacity_summaries_and_overlap_are_persisted_facts(
    settings: Settings,
    registered_project: dict[str, Any],
    client: Any,
) -> None:
    container = client.app.state.container
    project_id = registered_project["id"]
    task_details = [
        ("Active A", "READY", "C:/retained/a"),
        ("Active B", "RUNNING", "C:/retained/b"),
        ("Needs", "NEEDS_YOU", "C:/retained/needs"),
        ("Review", "REVIEW", "C:/retained/review"),
        ("Done", "DONE", "C:/retained/done"),
        ("Archived", "ARCHIVED", None),
    ]
    tasks = []
    for title, status, workspace_path in task_details:
        task = container.tasks.create(project_id=project_id, title=title, raw_request=title)
        task = container.tasks.update(
            task["id"], status=status, workspace_path=workspace_path
        )
        tasks.append(task)

    timestamp_a = "2026-08-22T01:00:00+00:00"
    timestamp_b = "2026-08-22T02:00:00+00:00"
    snapshot_specs = {
        tasks[0]["id"]: _snapshot_payload(
            timestamp_a, ("backend/shared.py", 4, 1, ["backend"])
        ),
        tasks[1]["id"]: _snapshot_payload(
            timestamp_b,
            ("backend/shared.py", 2, 3, ["backend"]),
            ("backend/other.py", 1, 0, ["backend"]),
        ),
        tasks[2]["id"]: _snapshot_payload(
            timestamp_b, ("backend/needs.py", 3, 0, ["backend"])
        ),
        tasks[3]["id"]: _snapshot_payload(
            timestamp_b, ("docs/review.md", 1, 0, ["docs"])
        ),
        tasks[5]["id"]: _snapshot_payload(
            timestamp_b, ("backend/shared.py", 99, 99, ["backend"])
        ),
    }
    for task_id, payload in snapshot_specs.items():
        container.snapshots.create(
            task_id=task_id,
            run_id=None,
            baseline_commit="base",
            head_commit="head",
            status_hash=f"snapshot-{task_id}",
            payload=payload,
        )
    container.events.record(
        Event(
            timestamp=timestamp_b,
            task_id=tasks[0]["id"],
            run_id=None,
            type="WorkspaceSnapshot",
            source="workspace",
            payload={"files": 1},
        )
    )
    container.runs.create(tasks[1]["id"])

    projector = CockpitProjector(
        settings=settings,
        database=container.database,
        projects=container.projects,
        tasks=container.tasks,
        snapshots=container.snapshots,
        events=container.events,
        runs=container.runs,
        checks=container.checks,
        human_inputs=container.human_inputs,
    )
    cockpit = projector.project(project_id)

    assert cockpit["capacity"] == {"limit": 3, "running": 1, "available": 2}
    assert {item["task"]["title"] for item in cockpit["sections"]["active"]} == {
        "Active A",
        "Active B",
    }
    assert cockpit["sections"]["needs_you"][0]["task"]["title"] == "Needs"
    assert cockpit["sections"]["review"][0]["task"]["title"] == "Review"
    assert cockpit["sections"]["done"][0]["workspace_retained"] is True
    assert cockpit["sections"]["archived"][0]["workspace_retained"] is False
    active_a = next(
        item for item in cockpit["sections"]["active"] if item["task"]["title"] == "Active A"
    )
    assert active_a["workspace"] == {
        "files_count": 1,
        "additions": 4,
        "deletions": 1,
        "modules": ["backend"],
        "recent_files": [
            {"path": "backend/shared.py", "status": "Modified", "added": 4, "deleted": 1}
        ],
        "captured_at": timestamp_a,
    }
    assert active_a["last_activity"]["type"] == "WorkspaceSnapshot"

    risks_by_pair_and_kind = {
        (frozenset(risk["task_titles"]), risk["kind"]): risk
        for risk in cockpit["risks"]
    }
    active_pair = frozenset({"Active A", "Active B"})
    assert risks_by_pair_and_kind[(active_pair, "FILE_OVERLAP")]["items"] == [
        "backend/shared.py"
    ]
    assert risks_by_pair_and_kind[(active_pair, "MODULE_OVERLAP")]["items"] == ["backend"]
    assert risks_by_pair_and_kind[(active_pair, "FILE_OVERLAP")]["snapshot_at"] == timestamp_b
    assert any(
        risk["kind"] == "MODULE_OVERLAP"
        and set(risk["task_titles"]) == {"Active A", "Needs"}
        and risk["items"] == ["backend"]
        for risk in cockpit["risks"]
    )
    assert all("Done" not in risk["task_titles"] for risk in cockpit["risks"])
    assert all("Archived" not in risk["task_titles"] for risk in cockpit["risks"])
    assert all("semantic" not in str(risk).lower() for risk in cockpit["risks"])


class ControlledAgentAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.fail: set[str] = set()

    async def execute(
        self,
        *,
        spec: TaskSpec,
        environment_spec: str,
        tools: WorkspaceTools,
        resume_context: object | None = None,
    ) -> AgentResult:
        del resume_context
        goal = spec.goal
        self.started.setdefault(goal, asyncio.Event()).set()
        await self.release.setdefault(goal, asyncio.Event()).wait()
        if goal in self.fail:
            raise RuntimeError("controlled provider failure")
        return AgentResult(status=AgentResultStatus.COMPLETED, summary=f"Completed {goal}")


@pytest.mark.asyncio
async def test_run_capacity_is_race_safe_and_failure_releases_only_its_slot(
    settings: Settings,
    fixture_repository: Path,
) -> None:
    limited_settings = settings.model_copy(update={"max_concurrent_runs": 3})
    adapter = ControlledAgentAdapter()
    container = build_container(limited_settings, adapter)
    container.database.initialize()
    project = container.project_service.create(
        name="Capacity",
        repository_path=str(fixture_repository),
        environment_spec="",
        module_mapping=[{"name": "backend", "paths": ["backend/**"]}],
    )
    tasks = []
    for index in range(4):
        goal = f"run-{index}"
        task = container.tasks.create(
            project_id=project["id"], title=goal, raw_request=f"Execute {goal}"
        )
        task = container.tasks.update(
            task["id"],
            status="READY",
            spec=TaskSpec(
                goal=goal,
                scope=["backend/**"],
                acceptance_criteria=["Completes deterministically"],
            ),
        )
        tasks.append(task)

    outcomes = await asyncio.gather(
        *(container.task_runtime.start(task["id"]) for task in tasks),
        return_exceptions=True,
    )
    started_runs = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    capacity_errors = [outcome for outcome in outcomes if isinstance(outcome, RunCapacityError)]
    assert len(started_runs) == 3
    assert len(capacity_errors) == 1
    assert capacity_errors[0].code == "RUN_CAPACITY_REACHED"
    started_task_ids = {run["task_id"] for run in started_runs}
    denied_task = next(task for task in tasks if task["id"] not in started_task_ids)
    task_titles = {task["id"]: task["title"] for task in tasks}
    assert container.tasks.get(denied_task["id"])["status"] == "READY"
    assert container.runs.list_for_task(denied_task["id"]) == []
    await asyncio.gather(
        *(adapter.started[task_titles[run["task_id"]]].wait() for run in started_runs)
    )

    failed_run = started_runs[0]
    failed_goal = task_titles[failed_run["task_id"]]
    adapter.fail.add(failed_goal)
    adapter.release[failed_goal].set()
    failed_background = container.task_runtime.active_runs[failed_run["id"]]
    await failed_background
    assert container.runs.get(failed_run["id"])["status"] == "FAILED"

    fourth = await container.task_runtime.start(denied_task["id"])
    assert fourth["status"] == "RUNNING"
    await asyncio.sleep(0)
    denied_goal = denied_task["title"]
    await adapter.started[denied_goal].wait()
    assert all(
        not background.cancelled()
        for background in container.task_runtime.active_runs.values()
    )

    remaining_goals = {
        task_titles[run["task_id"]]
        for run in started_runs
        if run["id"] != failed_run["id"]
    } | {denied_goal}
    for goal in remaining_goals:
        adapter.release[goal].set()
    await asyncio.gather(*list(container.task_runtime.active_runs.values()))
    assert all(
        container.runs.get(run_id)["status"] == "COMPLETED"
        for run_id in [
            *(run["id"] for run in started_runs if run["id"] != failed_run["id"]),
            fourth["id"],
        ]
    )
