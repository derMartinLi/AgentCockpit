from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.app.core.database import Database, json_dumps, json_loads
from backend.app.domain.models import AgentResult, AgentResultStatus, Event, TaskSpec, now_iso


class ProjectRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list(self) -> list[dict[str, Any]]:
        rows = self.database.fetch_all("SELECT * FROM projects ORDER BY created_at DESC")
        return [self._decode(row) for row in rows]

    def get(self, project_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        return self._decode(row) if row else None

    def get_by_path(self, repository_path: str) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            "SELECT * FROM projects WHERE repository_path = ?", (repository_path,)
        )
        return self._decode(row) if row else None

    def create(
        self,
        *,
        name: str,
        repository_path: str,
        default_branch: str,
        environment_spec: str,
        module_mapping: list[dict[str, Any]],
        repository_profile: dict[str, Any] | None = None,
        verification_commands: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        project_id = str(uuid4())
        timestamp = now_iso()
        self.database.execute(
            """
            INSERT INTO projects(
                id, name, repository_path, default_branch, environment_spec,
                module_mapping, repository_profile, verification_commands,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                name,
                repository_path,
                default_branch,
                environment_spec,
                json_dumps(module_mapping),
                json_dumps(repository_profile or {}),
                json_dumps(verification_commands or []),
                timestamp,
                timestamp,
            ),
        )
        return self.get(project_id)  # type: ignore[return-value]

    def update(self, project_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "name",
            "environment_spec",
            "module_mapping",
            "repository_profile",
            "verification_commands",
        }
        clean = {key: value for key, value in values.items() if key in allowed}
        if "module_mapping" in clean:
            clean["module_mapping"] = json_dumps(clean["module_mapping"])
        if "repository_profile" in clean:
            clean["repository_profile"] = json_dumps(clean["repository_profile"])
        if "verification_commands" in clean:
            clean["verification_commands"] = json_dumps(clean["verification_commands"])
        if not clean:
            return self.get(project_id)
        clean["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in clean)
        self.database.execute(
            f"UPDATE projects SET {assignments} WHERE id = ?",  # noqa: S608
            (*clean.values(), project_id),
        )
        return self.get(project_id)

    def delete(self, project_id: str) -> bool:
        task_count = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM tasks WHERE project_id = ?", (project_id,)
        )
        if int((task_count or {}).get("count", 0)):
            raise ValueError("Projects with Task history cannot be deleted")
        with self.database.connect() as connection:
            cursor = connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            connection.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        row["module_mapping"] = json_loads(row["module_mapping"], [])
        row["repository_profile"] = json_loads(row.get("repository_profile"), {})
        row["verification_commands"] = json_loads(row.get("verification_commands"), [])
        return row


class ProviderRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self) -> dict[str, Any]:
        row = self.database.fetch_one("SELECT * FROM provider_settings WHERE id = 1")
        if row:
            row["has_api_key"] = bool(row["has_api_key"])
            return row
        return {
            "provider": "demo",
            "model": "deterministic-alpha",
            "base_url": None,
            "has_api_key": False,
            "updated_at": None,
        }

    def save(
        self, *, provider: str, model: str, base_url: str | None, has_api_key: bool
    ) -> dict[str, Any]:
        self.database.execute(
            """
            INSERT INTO provider_settings(id, provider, model, base_url, has_api_key, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET provider = excluded.provider,
                model = excluded.model, base_url = excluded.base_url,
                has_api_key = excluded.has_api_key, updated_at = excluded.updated_at
            """,
            (provider, model, base_url, int(has_api_key), now_iso()),
        )
        return self.get()


class TaskRepository:
    UPDATABLE_FIELDS = {
        "title",
        "spec",
        "status",
        "runtime_phase",
        "refinement_round",
        "inspection",
        "questions",
        "answers",
        "workspace_path",
        "branch_name",
        "baseline_commit",
        "archived_at",
        "archived_by",
        "source_task_id",
    }
    JSON_FIELDS = {"spec", "inspection", "questions", "answers"}

    def __init__(self, database: Database) -> None:
        self.database = database

    def list_for_project(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """SELECT tasks.*, archive_operations.id AS archive_operation_id,
                archive_operations.phase AS archive_phase,
                archive_operations.archive_ref, archive_operations.archive_commit
            FROM tasks LEFT JOIN archive_operations ON archive_operations.task_id = tasks.id
            WHERE tasks.project_id = ? ORDER BY tasks.created_at DESC""",
            (project_id,),
        )
        return [self._decode(row) for row in rows]

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            """SELECT tasks.*, archive_operations.id AS archive_operation_id,
                archive_operations.phase AS archive_phase,
                archive_operations.archive_ref, archive_operations.archive_commit
            FROM tasks LEFT JOIN archive_operations ON archive_operations.task_id = tasks.id
            WHERE tasks.id = ?""",
            (task_id,),
        )
        return self._decode(row) if row else None

    def create(
        self,
        *,
        project_id: str,
        title: str,
        raw_request: str,
        source_task_id: str | None = None,
        spec: dict[str, Any] | None = None,
        status: str = "DRAFT",
    ) -> dict[str, Any]:
        task_id = str(uuid4())
        timestamp = now_iso()
        runtime_phase = "REFINING" if status == "DRAFT" else None
        self.database.execute(
            """
            INSERT INTO tasks(
                id, project_id, title, raw_request, spec, status, runtime_phase,
                source_task_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                project_id,
                title,
                raw_request,
                json_dumps(spec) if spec is not None else None,
                status,
                runtime_phase,
                source_task_id,
                timestamp,
                timestamp,
            ),
        )
        return self.get(task_id)  # type: ignore[return-value]

    def update(self, task_id: str, **values: Any) -> dict[str, Any] | None:
        clean = {key: value for key, value in values.items() if key in self.UPDATABLE_FIELDS}
        for field in self.JSON_FIELDS & clean.keys():
            value = clean[field]
            if isinstance(value, TaskSpec):
                value = value.model_dump()
            clean[field] = json_dumps(value) if value is not None else None
        if not clean:
            return self.get(task_id)
        clean["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in clean)
        self.database.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",  # noqa: S608
            (*clean.values(), task_id),
        )
        return self.get(task_id)

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        row["spec"] = json_loads(row.get("spec"), None)
        row["inspection"] = json_loads(row.get("inspection"), None)
        row["questions"] = json_loads(row.get("questions"), [])
        row["answers"] = json_loads(row.get("answers"), [])
        return row


class RunRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, task_id: str, previous_run_id: str | None = None) -> dict[str, Any]:
        run_id = str(uuid4())
        self.database.execute(
            """INSERT INTO agent_runs(id, task_id, status, started_at, previous_run_id)
            VALUES (?, ?, ?, ?, ?)""",
            (run_id, task_id, "RUNNING", now_iso(), previous_run_id),
        )
        return self.get(run_id)  # type: ignore[return-value]

    def get(self, run_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
        return self._decode(row) if row else None

    def list_for_task(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT * FROM agent_runs WHERE task_id = ? ORDER BY started_at DESC", (task_id,)
        )
        return [self._decode(row) for row in rows]

    def finish(self, run_id: str, result: AgentResult) -> dict[str, Any] | None:
        self.database.execute(
            """
            UPDATE agent_runs SET status = ?, summary = ?, result = ?, execution_summary = ?,
                finished_at = ? WHERE id = ?
            """,
            (
                result.status.value,
                result.summary,
                json_dumps(result.model_dump(mode="json")),
                json_dumps(
                    {
                        "summary": result.summary,
                        "changes": result.changes,
                        "checks": [check.model_dump() for check in result.checks],
                        "known_issues": result.known_issues,
                        "risks": result.risks,
                    }
                ),
                now_iso(),
                run_id,
            ),
        )
        return self.get(run_id)

    def request_cancel(self, run_id: str) -> dict[str, Any] | None:
        self.database.execute(
            "UPDATE agent_runs SET cancel_requested = 1 WHERE id = ? AND status = 'RUNNING'",
            (run_id,),
        )
        return self.get(run_id)

    def recover_interrupted(self) -> int:
        rows = self.database.fetch_all(
            "SELECT id, task_id FROM agent_runs WHERE status = 'RUNNING'"
        )
        for row in rows:
            result = AgentResult(
                status=AgentResultStatus.FAILED,
                summary="Run was interrupted by an application restart and can be resumed.",
                known_issues=["Runtime process restarted"],
                needs_human=True,
            )
            self.finish(row["id"], result)
        return len(rows)

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        row["result"] = json_loads(row.get("result"), None)
        row["execution_summary"] = json_loads(row.get("execution_summary"), None)
        row["cancel_requested"] = bool(row.get("cancel_requested", 0))
        return row


class WorkspaceSnapshotRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        task_id: str,
        run_id: str | None,
        baseline_commit: str,
        head_commit: str,
        status_hash: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot_id = str(uuid4())
        self.database.execute(
            """INSERT INTO workspace_snapshots(
                id, task_id, run_id, baseline_commit, head_commit, status_hash, payload, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                task_id,
                run_id,
                baseline_commit,
                head_commit,
                status_hash,
                json_dumps(payload),
                now_iso(),
            ),
        )
        return self.latest(task_id)  # type: ignore[return-value]

    def latest(self, task_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            """SELECT * FROM workspace_snapshots WHERE task_id = ?
            ORDER BY captured_at DESC, rowid DESC LIMIT 1""",
            (task_id,),
        )
        if row:
            row["payload"] = json_loads(row["payload"], {})
        return row

    def get(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            "SELECT * FROM workspace_snapshots WHERE id = ?", (snapshot_id,)
        )
        if row:
            row["payload"] = json_loads(row["payload"], {})
        return row


class ArchiveRepository:
    UPDATABLE_FIELDS = {
        "phase",
        "last_successful_phase",
        "final_snapshot_id",
        "archive_ref",
        "archive_commit",
        "error",
        "updated_at",
        "completed_at",
    }

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, operation_id: str) -> dict[str, Any] | None:
        return self.database.fetch_one(
            "SELECT * FROM archive_operations WHERE id = ?", (operation_id,)
        )

    def get_for_task(self, task_id: str) -> dict[str, Any] | None:
        return self.database.fetch_one(
            "SELECT * FROM archive_operations WHERE task_id = ?", (task_id,)
        )

    def create(
        self, *, task_id: str, actor: str, original_workspace_path: str
    ) -> dict[str, Any]:
        operation_id = str(uuid4())
        timestamp = now_iso()
        self.database.execute(
            """INSERT INTO archive_operations(
                id, task_id, phase, actor, original_workspace_path, started_at, updated_at
            ) VALUES (?, ?, 'PREPARING', ?, ?, ?, ?)
            ON CONFLICT(task_id) DO NOTHING""",
            (operation_id, task_id, actor, original_workspace_path, timestamp, timestamp),
        )
        return self.get_for_task(task_id)  # type: ignore[return-value]

    def update(self, operation_id: str, **values: Any) -> dict[str, Any] | None:
        clean = {key: value for key, value in values.items() if key in self.UPDATABLE_FIELDS}
        clean["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in clean)
        self.database.execute(
            f"UPDATE archive_operations SET {assignments} WHERE id = ?",  # noqa: S608
            (*clean.values(), operation_id),
        )
        return self.get(operation_id)

    def list_incomplete(self) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """SELECT * FROM archive_operations WHERE phase != 'COMPLETED'
            ORDER BY started_at"""
        )

    def complete(
        self,
        operation_id: str,
        *,
        task_id: str,
        actor: str,
        archive_ref: str,
        archive_commit: str,
        completed_at: str,
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE tasks SET status = 'ARCHIVED', runtime_phase = NULL,
                    workspace_path = NULL, archived_at = ?, archived_by = ?, updated_at = ?
                WHERE id = ? AND status IN ('DONE', 'ARCHIVED')""",
                (completed_at, actor, completed_at, task_id),
            )
            connection.execute(
                """UPDATE archive_operations SET phase = 'COMPLETED',
                    last_successful_phase = 'REMOVING', archive_ref = ?, archive_commit = ?,
                    error = NULL, updated_at = ?, completed_at = ? WHERE id = ?""",
                (archive_ref, archive_commit, completed_at, completed_at, operation_id),
            )
            connection.execute(
                """INSERT INTO events(timestamp, task_id, run_id, type, source, payload)
                VALUES (?, ?, NULL, 'TaskArchived', 'archive', ?)""",
                (
                    completed_at,
                    task_id,
                    json_dumps(
                        {
                            "actor": actor,
                            "archive_ref": archive_ref,
                            "archive_commit": archive_commit,
                            "workspace_removed": True,
                        }
                    ),
                ),
            )
            connection.commit()
        return self.get(operation_id)  # type: ignore[return-value]


class CheckRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record_result_checks(self, task_id: str, run_id: str, checks: list[Any]) -> None:
        for item in checks:
            data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
            existing = self.database.fetch_one(
                "SELECT id FROM checks WHERE run_id = ? AND name = ? LIMIT 1",
                (run_id, data["name"]),
            )
            if existing:
                continue
            timestamp = now_iso()
            self.database.execute(
                """INSERT INTO checks(
                    id, task_id, run_id, name, status, output_excerpt, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid4()),
                    task_id,
                    run_id,
                    data["name"],
                    data["status"],
                    (data.get("detail") or "")[-4000:],
                    timestamp,
                    timestamp,
                ),
            )

    def start(
        self, task_id: str, run_id: str, name: str, command: list[str], cwd: str
    ) -> str:
        check_id = str(uuid4())
        self.database.execute(
            """INSERT INTO checks(
                id, task_id, run_id, name, command, cwd, status, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?)""",
            (check_id, task_id, run_id, name, json_dumps(command), cwd, now_iso()),
        )
        return check_id

    def finish(self, check_id: str, output: str, *, failed: bool = False) -> None:
        first_line = output.splitlines()[0] if output else ""
        exit_code = None
        if first_line.startswith("exit_code="):
            try:
                exit_code = int(first_line.removeprefix("exit_code="))
            except ValueError:
                pass
        status = "failed" if failed or exit_code not in {0, None} else "passed"
        self.database.execute(
            """UPDATE checks SET status = ?, exit_code = ?, output_excerpt = ?, finished_at = ?
            WHERE id = ?""",
            (status, exit_code, output[-4000:], now_iso(), check_id),
        )

    def list_for_task(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT * FROM checks WHERE task_id = ? ORDER BY started_at DESC", (task_id,)
        )
        for row in rows:
            row["command"] = json_loads(row["command"], [])
            row.update(self._diagnose(row))
        return rows

    @staticmethod
    def _diagnose(row: dict[str, Any]) -> dict[str, str | None]:
        if row.get("status", "").lower() != "failed":
            return {
                "failure_kind": None,
                "failure_message": None,
                "suggested_action": None,
            }
        output = str(row.get("output_excerpt") or "")
        lowered = output.lower()
        command = " ".join(str(item) for item in row.get("command", []))
        if "cannot find module" in lowered or "no such file or directory" in lowered:
            return {
                "failure_kind": "COMMAND_INVALID",
                "failure_message": f"验证命令引用了不存在的目标：{command}",
                "suggested_action": "在 Project Settings 修正命令或工作目录后重新运行 Task。",
            }
        if "not recognized" in lowered or "no such file or directory" in lowered:
            return {
                "failure_kind": "COMMAND_NOT_FOUND",
                "failure_message": f"当前环境找不到命令：{command}",
                "suggested_action": "安装项目工具链，或在 Project Settings 选择正确的命令。",
            }
        if row.get("exit_code") is None:
            return {
                "failure_kind": "RUNNER_ERROR",
                "failure_message": "验证执行器未能取得命令退出码。",
                "suggested_action": "先重试；若持续出现，请检查 Runtime 错误而不是修改业务测试。",
            }
        return {
            "failure_kind": "CHECK_FAILED",
            "failure_message": f"命令已执行，但以退出码 {row['exit_code']} 结束。",
            "suggested_action": "查看输出定位失败断言；若命令目标不正确，请更新 Project 验证配置。",
        }


class HumanInputRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, task_id: str, run_id: str, question: str) -> dict[str, Any]:
        input_id = str(uuid4())
        self.database.execute(
            """INSERT INTO human_inputs(
                id, task_id, run_id, question, status, requested_at
            ) VALUES (?, ?, ?, ?, 'PENDING', ?)""",
            (input_id, task_id, run_id, question, now_iso()),
        )
        return self.database.fetch_one("SELECT * FROM human_inputs WHERE id = ?", (input_id,))  # type: ignore[return-value]

    def pending(self, task_id: str) -> dict[str, Any] | None:
        return self.database.fetch_one(
            """SELECT * FROM human_inputs WHERE task_id = ? AND status = 'PENDING'
            ORDER BY requested_at DESC LIMIT 1""",
            (task_id,),
        )

    def answer(self, task_id: str, answer: str) -> dict[str, Any] | None:
        pending = self.pending(task_id)
        if not pending:
            return None
        self.database.execute(
            """UPDATE human_inputs SET answer = ?, status = 'ANSWERED', answered_at = ?
            WHERE id = ?""",
            (answer, now_iso(), pending["id"]),
        )
        return self.database.fetch_one("SELECT * FROM human_inputs WHERE id = ?", (pending["id"],))

    def list_for_task(self, task_id: str) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            "SELECT * FROM human_inputs WHERE task_id = ? ORDER BY requested_at", (task_id,)
        )


class ReviewRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        task_id: str,
        run_id: str | None,
        decision: str,
        reason: str | None,
        actor: str = "local-user",
    ) -> dict[str, Any]:
        decision_id = str(uuid4())
        self.database.execute(
            """INSERT INTO review_decisions(
                id, task_id, run_id, decision, reason, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (decision_id, task_id, run_id, decision, reason, actor, now_iso()),
        )
        return self.database.fetch_one(
            "SELECT * FROM review_decisions WHERE id = ?", (decision_id,)
        )  # type: ignore[return-value]

    def list_for_task(self, task_id: str) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            "SELECT * FROM review_decisions WHERE task_id = ? ORDER BY created_at", (task_id,)
        )


class EventRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, event: Event) -> None:
        self.database.execute(
            """
            INSERT INTO events(timestamp, task_id, run_id, type, source, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.timestamp,
                event.task_id,
                event.run_id,
                event.type,
                event.source,
                json_dumps(event.payload),
            ),
        )

    def list_for_task(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT * FROM events WHERE task_id = ? AND id > ? ORDER BY id", (task_id, after)
        )
        for row in rows:
            row["payload"] = json_loads(row["payload"], {})
        return rows

    def list_for_project(self, project_id: str, after: int = 0) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """SELECT events.* FROM events
            JOIN tasks ON tasks.id = events.task_id
            WHERE tasks.project_id = ? AND events.id > ? ORDER BY events.id""",
            (project_id, after),
        )
        for row in rows:
            row["payload"] = json_loads(row["payload"], {})
        return rows
