from __future__ import annotations

from typing import Any

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.domain.models import now_iso
from backend.app.repositories.store import (
    CheckRepository,
    EventRepository,
    HumanInputRepository,
    ProjectRepository,
    RunRepository,
    TaskRepository,
    WorkspaceSnapshotRepository,
)

ACTIVE_STATUSES = {"DRAFT", "READY", "RUNNING", "FAILED"}
SECTION_BY_STATUS = {
    "NEEDS_YOU": "needs_you",
    "REVIEW": "review",
    "DONE": "done",
    "ARCHIVED": "archived",
}


class CockpitProjector:
    """Build the project cockpit exclusively from persisted facts."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        projects: ProjectRepository,
        tasks: TaskRepository,
        snapshots: WorkspaceSnapshotRepository,
        events: EventRepository,
        runs: RunRepository,
        checks: CheckRepository,
        human_inputs: HumanInputRepository,
    ) -> None:
        self.settings = settings
        self.database = database
        self.projects = projects
        self.tasks = tasks
        self.snapshots = snapshots
        self.events = events
        self.runs = runs
        self.checks = checks
        self.human_inputs = human_inputs

    def project(self, project_id: str) -> dict[str, Any]:
        if not self.projects.get(project_id):
            raise KeyError(project_id)

        generated_at = now_iso()
        tasks = self.tasks.list_for_project(project_id)
        project_events = self.events.list_for_project(project_id)
        latest_events = self._latest_events(project_events)
        current_events = self._current_events(project_events)
        latest_snapshots = {
            task["id"]: self.snapshots.latest(task["id"])
            for task in tasks
        }
        sections: dict[str, list[dict[str, Any]]] = {
            "active": [],
            "needs_you": [],
            "review": [],
            "done": [],
            "archived": [],
        }
        for task in tasks:
            snapshot = latest_snapshots[task["id"]]
            status = task["status"]
            section = SECTION_BY_STATUS.get(status, "active")
            if status not in ACTIVE_STATUSES and status not in SECTION_BY_STATUS:
                section = "active"
            task_runs = self.runs.list_for_task(task["id"])
            latest_run = task_runs[0] if task_runs else None
            task_checks = self.checks.list_for_task(task["id"])
            if latest_run:
                task_checks = [
                    check for check in task_checks if check["run_id"] == latest_run["id"]
                ]
            else:
                task_checks = []
            pending_input = self.human_inputs.pending(task["id"])
            sections[section].append(
                {
                    "task": task,
                    "workspace": self._workspace_summary(snapshot),
                    "last_activity": latest_events.get(task["id"]),
                    "current_activity": current_events.get(task["id"]),
                    "run": self._run_summary(latest_run),
                    "verification": self._verification_summary(task_checks),
                    "attention": self._attention(
                        task=task,
                        latest_run=latest_run,
                        checks=task_checks,
                        pending_input=pending_input,
                        current_activity=current_events.get(task["id"]),
                    ),
                    "workspace_retained": status != "ARCHIVED" and bool(task["workspace_path"]),
                }
            )

        running = self._running_count()
        limit = self.settings.max_concurrent_runs
        return {
            "project_id": project_id,
            "generated_at": generated_at,
            "capacity": {
                "limit": limit,
                "running": running,
                "available": max(limit - running, 0),
            },
            "sections": sections,
            "history": {
                "done": len(sections["done"]),
                "archived": len(sections["archived"]),
                "total": len(sections["done"]) + len(sections["archived"]),
            },
            "risks": self._overlap_risks(tasks, latest_snapshots),
        }

    @staticmethod
    def _latest_events(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for event in events:
            latest[event["task_id"]] = event
        return latest

    @staticmethod
    def _current_events(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        observable = {
            "AgentProgress",
            "AgentStepStarted",
            "ToolStarted",
            "CommandStarted",
            "CommandFinished",
            "FileCreated",
            "FileChanged",
            "FileDeleted",
            "AgentCompleted",
            "AgentFailed",
            "HumanInputRequested",
        }
        latest: dict[str, dict[str, Any]] = {}
        for event in events:
            if event["type"] in observable:
                latest[event["task_id"]] = event
        return latest

    def _running_count(self) -> int:
        row = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM agent_runs WHERE status = 'RUNNING'"
        )
        return int((row or {}).get("count", 0))

    @staticmethod
    def _workspace_summary(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
        if not snapshot:
            return None
        payload = snapshot["payload"]
        files = payload.get("files", [])
        modules = payload.get("modules") or sorted(
            {
                module
                for item in files
                for module in item.get("modules", [])
                if isinstance(module, str)
            }
        )
        return {
            "files_count": len(files),
            "additions": sum(CockpitProjector._line_count(item.get("added")) for item in files),
            "deletions": sum(
                CockpitProjector._line_count(item.get("deleted")) for item in files
            ),
            "modules": sorted({str(module) for module in modules}),
            "recent_files": [
                {
                    "path": str(item.get("path", "")),
                    "status": str(item.get("status", "Modified")),
                    "added": CockpitProjector._line_count(item.get("added")),
                    "deleted": CockpitProjector._line_count(item.get("deleted")),
                }
                for item in files[:5]
                if item.get("path")
            ],
            "captured_at": payload.get("captured_at") or snapshot["captured_at"],
        }

    @staticmethod
    def _run_summary(run: dict[str, Any] | None) -> dict[str, Any] | None:
        if not run:
            return None
        return {
            "id": run["id"],
            "status": run["status"],
            "summary": run.get("summary"),
            "started_at": run["started_at"],
            "finished_at": run.get("finished_at"),
            "known_issues": (run.get("result") or {}).get("known_issues", []),
        }

    @staticmethod
    def _verification_summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
        failed = [check for check in checks if check["status"].lower() == "failed"]
        passed = [check for check in checks if check["status"].lower() == "passed"]
        return {
            "total": len(checks),
            "passed": len(passed),
            "failed": len(failed),
            "status": "failed" if failed else "passed" if checks else "not_run",
            "latest_failure": failed[0] if failed else None,
        }

    @staticmethod
    def _attention(
        *,
        task: dict[str, Any],
        latest_run: dict[str, Any] | None,
        checks: list[dict[str, Any]],
        pending_input: dict[str, Any] | None,
        current_activity: dict[str, Any] | None,
    ) -> dict[str, Any]:
        status = task["status"]
        failed = next(
            (check for check in checks if check["status"].lower() == "failed"), None
        )
        if status == "DRAFT":
            if task["questions"]:
                return {
                    "level": "action",
                    "title": f"AI 提出了 {len(task['questions'])} 个需确认的决策",
                    "detail": "答案已经预填写，可直接修改后生成 Task Spec。",
                    "action": "确认澄清",
                }
            return {
                "level": "action",
                "title": "AI Task Spec 已准备",
                "detail": "可直接编辑后确认，无需额外填写澄清表单。",
                "action": "检查 Spec",
            }
        if status == "READY":
            return {
                "level": "action",
                "title": "Task 已可执行",
                "detail": "启动后将在独立 Worktree 后台运行。",
                "action": "开始运行",
            }
        if status == "RUNNING":
            message = (current_activity or {}).get("payload", {}).get("message")
            return {
                "level": "live",
                "title": str(message or "Agent 正在后台执行"),
                "detail": (current_activity or {}).get("type", "AgentStarted"),
                "action": "查看实时证据",
            }
        if pending_input:
            return {
                "level": "blocked",
                "title": pending_input["question"],
                "detail": "Agent 需要人工输入后才能继续。",
                "action": "回答并继续",
            }
        if status in {"FAILED", "NEEDS_YOU"}:
            return {
                "level": "blocked",
                "title": (latest_run or {}).get("summary") or "Task 需要处理",
                "detail": (
                    failed.get("failure_message")
                    if failed
                    else "Workspace 已保留，可以查看证据后继续。"
                ),
                "action": "处理问题",
            }
        if failed:
            return {
                "level": "warning",
                "title": failed.get("failure_message") or f"检查失败：{failed['name']}",
                "detail": failed.get("suggested_action") or (latest_run or {}).get("summary"),
                "action": "查看验证失败",
            }
        if status == "REVIEW":
            return {
                "level": "review",
                "title": "实现已完成，等待人工 Review",
                "detail": (latest_run or {}).get("summary") or "检查真实 Diff 与验证结果。",
                "action": "开始 Review",
            }
        return {
            "level": "history",
            "title": (latest_run or {}).get("summary") or "历史记录已保留",
            "detail": "查看 Task 的变更、验证和 Review 记录。",
            "action": "查看历史",
        }

    @staticmethod
    def _line_count(value: Any) -> int:
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _overlap_risks(
        tasks: list[dict[str, Any]],
        snapshots: dict[str, dict[str, Any] | None],
    ) -> list[dict[str, Any]]:
        candidates = sorted(
            (
                task
                for task in tasks
                if task["status"] not in {"DONE", "ARCHIVED"} and snapshots.get(task["id"])
            ),
            key=lambda item: item["id"],
        )
        facts = {
            task["id"]: CockpitProjector._snapshot_facts(snapshots[task["id"]])
            for task in candidates
        }
        risks: list[dict[str, Any]] = []
        for index, left in enumerate(candidates):
            for right in candidates[index + 1 :]:
                left_facts = facts[left["id"]]
                right_facts = facts[right["id"]]
                shared_files = sorted(left_facts["files"] & right_facts["files"])
                shared_modules = sorted(left_facts["modules"] & right_facts["modules"])
                evidence_time = max(left_facts["captured_at"], right_facts["captured_at"])
                common = {
                    "task_ids": [left["id"], right["id"]],
                    "task_titles": [left["title"], right["title"]],
                    "snapshot_at": evidence_time,
                }
                if shared_files:
                    risks.append({"kind": "FILE_OVERLAP", **common, "items": shared_files})
                if shared_modules:
                    risks.append({"kind": "MODULE_OVERLAP", **common, "items": shared_modules})
        return sorted(
            risks,
            key=lambda item: (item["task_ids"], item["kind"] != "FILE_OVERLAP", item["items"]),
        )

    @staticmethod
    def _snapshot_facts(snapshot: dict[str, Any] | None) -> dict[str, Any]:
        assert snapshot is not None
        payload = snapshot["payload"]
        files = payload.get("files", [])
        return {
            "files": {
                item["path"]
                for item in files
                if isinstance(item.get("path"), str) and item["path"]
            },
            "modules": {
                str(module)
                for item in files
                for module in item.get("modules", [])
                if module
            }
            | {str(module) for module in payload.get("modules", []) if module},
            "captured_at": str(payload.get("captured_at") or snapshot["captured_at"]),
        }
