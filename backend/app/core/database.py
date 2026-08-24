from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        repository_path TEXT NOT NULL UNIQUE,
        default_branch TEXT NOT NULL,
        environment_spec TEXT NOT NULL DEFAULT '',
        module_mapping TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS provider_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        base_url TEXT,
        has_api_key INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        raw_request TEXT NOT NULL,
        spec TEXT,
        status TEXT NOT NULL,
        runtime_phase TEXT,
        refinement_round INTEGER NOT NULL DEFAULT 0,
        inspection TEXT,
        questions TEXT NOT NULL DEFAULT '[]',
        answers TEXT NOT NULL DEFAULT '[]',
        workspace_path TEXT,
        branch_name TEXT,
        baseline_commit TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS agent_runs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        status TEXT NOT NULL,
        summary TEXT,
        result TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        run_id TEXT,
        type TEXT NOT NULL,
        source TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
    CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
    """,
    """
    ALTER TABLE agent_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE agent_runs ADD COLUMN execution_summary TEXT;
    ALTER TABLE agent_runs ADD COLUMN previous_run_id TEXT;

    CREATE TABLE IF NOT EXISTS workspace_snapshots (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
        baseline_commit TEXT NOT NULL,
        head_commit TEXT NOT NULL,
        status_hash TEXT NOT NULL,
        payload TEXT NOT NULL,
        captured_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS file_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
        path TEXT NOT NULL,
        change_type TEXT NOT NULL,
        added INTEGER NOT NULL DEFAULT 0,
        deleted INTEGER NOT NULL DEFAULT 0,
        modules TEXT NOT NULL DEFAULT '[]',
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS checks (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        command TEXT NOT NULL DEFAULT '[]',
        cwd TEXT NOT NULL DEFAULT '.',
        status TEXT NOT NULL,
        exit_code INTEGER,
        output_excerpt TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE TABLE IF NOT EXISTS human_inputs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        question TEXT NOT NULL,
        answer TEXT,
        status TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        answered_at TEXT
    );
    CREATE TABLE IF NOT EXISTS review_decisions (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
        decision TEXT NOT NULL,
        reason TEXT,
        actor TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_workspace_snapshots_task_captured
        ON workspace_snapshots(task_id, captured_at);
    CREATE INDEX IF NOT EXISTS idx_file_changes_task_last_seen
        ON file_changes(task_id, last_seen);
    CREATE INDEX IF NOT EXISTS idx_checks_task_run ON checks(task_id, run_id);
    CREATE INDEX IF NOT EXISTS idx_human_inputs_task_status ON human_inputs(task_id, status);
    CREATE INDEX IF NOT EXISTS idx_review_decisions_task_created
        ON review_decisions(task_id, created_at);
    """,
    """
    ALTER TABLE tasks ADD COLUMN archived_at TEXT;
    ALTER TABLE tasks ADD COLUMN archived_by TEXT;
    ALTER TABLE tasks ADD COLUMN source_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL;

    CREATE TABLE IF NOT EXISTS archive_operations (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
        phase TEXT NOT NULL,
        last_successful_phase TEXT,
        actor TEXT NOT NULL,
        original_workspace_path TEXT NOT NULL,
        final_snapshot_id TEXT REFERENCES workspace_snapshots(id) ON DELETE SET NULL,
        archive_ref TEXT,
        archive_commit TEXT,
        error TEXT,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_source_task ON tasks(source_task_id);
    CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status);
    CREATE INDEX IF NOT EXISTS idx_archive_operations_phase_updated
        ON archive_operations(phase, updated_at);
    """,
    """
    ALTER TABLE projects ADD COLUMN repository_profile TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE projects ADD COLUMN verification_commands TEXT NOT NULL DEFAULT '[]';
    """,
]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            try:
                # Serialize startup upgrades and keep each discovered upgrade set atomic.
                connection.execute("BEGIN IMMEDIATE")
                applied = {
                    row[0]
                    for row in connection.execute("SELECT version FROM schema_migrations")
                }
                for version, migration in enumerate(MIGRATIONS, start=1):
                    if version in applied:
                        continue
                    self._execute_script(connection, migration)
                    connection.execute(
                        "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            connection.execute("PRAGMA optimize")

    @staticmethod
    def _execute_script(connection: sqlite3.Connection, script: str) -> None:
        statement = ""
        for line in script.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                connection.execute(statement)
                statement = ""
        if statement.strip():
            raise sqlite3.OperationalError("Incomplete SQL migration statement")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def fetch_one(self, query: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
            return dict(row) if row else None

    def fetch_all(self, query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> None:
        with self.connect() as connection:
            connection.execute(query, parameters)
            connection.commit()

    def execute_returning(
        self, query: str, parameters: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
            connection.commit()
            return dict(row) if row else None


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)
