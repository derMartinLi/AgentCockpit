from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.app.domain.models import now_iso
from backend.app.repositories.store import WorkspaceSnapshotRepository
from backend.app.services.projects import ModuleMapper
from backend.app.services.workspace import WorkspaceManager


class WorkspaceObserver:
    def __init__(
        self,
        *,
        snapshots: WorkspaceSnapshotRepository,
        workspaces: WorkspaceManager,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.workspaces = workspaces
        self.redact = redact or (lambda value: value)

    def capture(
        self,
        *,
        task: dict[str, Any],
        project: dict[str, Any],
        run_id: str | None,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        workspace = Path(task["workspace_path"])
        identity = self.workspaces.verify(
            project=project,
            workspace=workspace,
            expected_branch=task["branch_name"],
        )
        changes = self._changes(workspace, ModuleMapper(project["module_mapping"]))
        payload = {
            "captured_at": now_iso(),
            "identity": identity,
            "files": changes,
            "modules": sorted({module for item in changes for module in item["modules"]}),
            "diff": self.redact(self._diff(workspace, changes)),
        }
        stable = {key: value for key, value in payload.items() if key != "captured_at"}
        status_hash = hashlib.sha256(
            json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        previous = self.snapshots.latest(task["id"])
        if previous and previous["status_hash"] == status_hash:
            return previous["payload"]
        self.snapshots.create(
            task_id=task["id"],
            run_id=run_id,
            baseline_commit=task["baseline_commit"],
            head_commit=identity["head"],
            status_hash=status_hash,
            payload=payload,
        )
        if emit:
            old_files = (previous or {}).get("payload", {}).get("files", [])
            old = {item["path"]: item for item in old_files}
            current_paths = {item["path"] for item in changes}
            for item in changes:
                if old.get(item["path"]) != item:
                    emit(f"File{item['status']}", item)
            for path in old.keys() - current_paths:
                emit("FileRestored", {"path": path})
            emit("WorkspaceSnapshot", {"files": len(changes), "modules": payload["modules"]})
        return payload

    @staticmethod
    def _git(workspace: Path, arguments: list[str]) -> bytes:
        return subprocess.run(
            ["git", *arguments],
            cwd=workspace,
            capture_output=True,
            timeout=30,
            check=True,
        ).stdout

    def _changes(self, workspace: Path, mapper: ModuleMapper) -> list[dict[str, Any]]:
        raw = self._git(workspace, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        records = raw.decode(errors="replace").split("\0")
        paths: list[tuple[str, str]] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            code, path = record[:2], record[3:]
            previous_path = None
            if "R" in code or "C" in code:
                if index < len(records) and records[index]:
                    previous_path = records[index]
                    index += 1
            if "?" in code or "A" in code:
                status = "Created"
            elif "D" in code:
                status = "Deleted"
            elif "R" in code:
                status = "Renamed"
            else:
                status = "Modified"
            if path.startswith(".task/"):
                continue
            paths.append((path.replace("\\", "/"), status))

        numstat: dict[str, tuple[int, int]] = {}
        raw_numstat = self._git(workspace, ["diff", "--numstat", "HEAD"])
        for line in raw_numstat.decode(errors="replace").splitlines():
            added, deleted, path = line.split("\t", 2)
            numstat[path.replace("\\", "/")] = (
                int(added) if added.isdigit() else 0,
                int(deleted) if deleted.isdigit() else 0,
            )
        result = []
        for path, status in paths:
            added, deleted = numstat.get(path, (0, 0))
            if status == "Created":
                try:
                    added = len((workspace / path).read_text(encoding="utf-8").splitlines())
                except (OSError, UnicodeDecodeError):
                    pass
            item = {
                "path": path,
                "status": status,
                "added": added,
                "deleted": deleted,
                "modules": mapper.match(path),
            }
            if previous_path:
                item["previous_path"] = previous_path.replace("\\", "/")
            result.append(item)
        return sorted(result, key=lambda item: item["path"])

    def _diff(self, workspace: Path, changes: list[dict[str, Any]]) -> str:
        tracked = self._git(workspace, ["diff", "--no-ext-diff", "--binary", "HEAD"]).decode(
            errors="replace"
        )
        additions: list[str] = []
        for item in changes:
            if item["status"] != "Created":
                continue
            path = item["path"]
            try:
                lines = (workspace / path).read_text(encoding="utf-8").splitlines(keepends=True)
            except (OSError, UnicodeDecodeError):
                additions.append(f"Binary or unreadable new file: {path}\n")
                continue
            additions.extend(
                difflib.unified_diff([], lines, fromfile="/dev/null", tofile=f"b/{path}")
            )
        return (tracked + "".join(additions))[-200_000:]
