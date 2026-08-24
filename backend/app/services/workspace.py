from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from backend.app.core.config import Settings


class WorkspaceError(RuntimeError):
    pass


class WorkspaceManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def prepare(
        self,
        *,
        project: dict[str, Any],
        task_id: str,
        base_commit: str | None = None,
    ) -> dict[str, str]:
        repository = Path(project["repository_path"]).resolve()
        workspace = self.expected_path(project_id=str(project["id"]), task_id=task_id)
        project_dir = workspace.parent
        branch = f"agent/task-{task_id}"
        project_dir.mkdir(parents=True, exist_ok=True)

        if workspace.exists():
            self.verify(project=project, workspace=workspace, expected_branch=branch)
            baseline = self._git(workspace, ["rev-parse", "HEAD"])
            return {
                "workspace_path": str(workspace),
                "branch_name": branch,
                "baseline_commit": baseline,
            }

        try:
            baseline = self.resolve_commit(repository, base_commit or "HEAD")
            branch_exists = (
                subprocess.run(
                    ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                    cwd=repository,
                    timeout=10,
                    check=False,
                ).returncode
                == 0
            )
            arguments = ["worktree", "add"]
            if not branch_exists:
                arguments.extend(["-b", branch])
            arguments.extend([str(workspace), branch if branch_exists else baseline])
            self._git(repository, arguments)
        except (subprocess.SubprocessError, OSError) as error:
            raise WorkspaceError(f"Unable to create worktree: {error}") from error
        self.verify(project=project, workspace=workspace, expected_branch=branch)
        return {
            "workspace_path": str(workspace),
            "branch_name": branch,
            "baseline_commit": baseline,
        }

    def expected_path(self, *, project_id: str, task_id: str) -> Path:
        self._validate_identifier(project_id, "project")
        self._validate_identifier(task_id, "task")
        root = self.settings.worktrees_dir.resolve()
        workspace = (root / project_id / f"task-{task_id}").resolve()
        if not workspace.is_relative_to(root) or workspace == root:
            raise WorkspaceError("Task worktree path escapes the configured worktree root")
        return workspace

    def create_archive_snapshot(
        self,
        *,
        project: dict[str, Any],
        task_id: str,
        workspace: Path,
        expected_branch: str,
    ) -> dict[str, str]:
        """Create a recoverable commit/ref without touching the branch or worktree index."""
        repository = Path(project["repository_path"]).resolve()
        resolved_workspace = self._assert_archive_target(
            project=project, task_id=task_id, workspace=workspace
        )
        identity = self.verify(
            project=project,
            workspace=resolved_workspace,
            expected_branch=expected_branch,
        )
        archive_ref = f"refs/agent-cockpit/archives/{task_id}"
        branch_ref = f"refs/heads/{expected_branch}"
        branch_before = self.resolve_commit(repository, branch_ref)
        if branch_before != identity["head"]:
            raise WorkspaceError("Task branch and worktree HEAD do not identify the same commit")

        git_identity = {
            "GIT_AUTHOR_NAME": "Agent Cockpit Archive",
            "GIT_AUTHOR_EMAIL": "archive@agent-cockpit.local",
            "GIT_COMMITTER_NAME": "Agent Cockpit Archive",
            "GIT_COMMITTER_EMAIL": "archive@agent-cockpit.local",
        }
        try:
            with tempfile.TemporaryDirectory(prefix="agent-cockpit-index-") as temp_dir:
                index_path = Path(temp_dir) / "index"
                index_env = {**git_identity, "GIT_INDEX_FILE": str(index_path)}
                self._git(resolved_workspace, ["read-tree", "HEAD"], env=index_env)
                # -A stages tracked changes/deletions and all non-ignored untracked files.
                self._git(resolved_workspace, ["add", "-A", "--", "."], env=index_env)
                tree = self._git(resolved_workspace, ["write-tree"], env=index_env)
                archive_commit = self._git(
                    resolved_workspace,
                    [
                        "commit-tree",
                        tree,
                        "-p",
                        identity["head"],
                        "-m",
                        f"Agent Cockpit archive snapshot for task {task_id}",
                    ],
                    env=index_env,
                )
            self._git(repository, ["update-ref", archive_ref, archive_commit])
            self.verify_archive_ref(
                project=project,
                archive_ref=archive_ref,
                archive_commit=archive_commit,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise WorkspaceError(f"Unable to create archive snapshot: {error}") from error

        branch_after = self.resolve_commit(repository, branch_ref)
        head_after = self.resolve_commit(resolved_workspace, "HEAD")
        if branch_after != branch_before or head_after != identity["head"]:
            raise WorkspaceError("Archive snapshot unexpectedly moved the task branch or HEAD")
        return {
            "archive_ref": archive_ref,
            "archive_commit": archive_commit,
            "tree": tree,
        }

    def verify_archive_candidate(
        self,
        *,
        project: dict[str, Any],
        task_id: str,
        workspace: Path,
        expected_branch: str,
    ) -> dict[str, Any]:
        resolved_workspace = self._assert_archive_target(
            project=project, task_id=task_id, workspace=workspace
        )
        identity = self.verify(
            project=project,
            workspace=resolved_workspace,
            expected_branch=expected_branch,
        )
        if identity["locked"]:
            raise WorkspaceError("Locked worktrees cannot be archived")
        return identity

    def assert_no_forbidden_content(
        self,
        *,
        project: dict[str, Any],
        task_id: str,
        workspace: Path,
        forbidden_values: list[str],
    ) -> None:
        """Reject exact secret values in any file that the archive index would include."""
        resolved_workspace = self._assert_archive_target(
            project=project, task_id=task_id, workspace=workspace
        )
        secrets = [value.encode() for value in forbidden_values if value]
        if not secrets:
            return
        try:
            output = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                cwd=resolved_workspace,
                capture_output=True,
                timeout=30,
                check=True,
            ).stdout
            paths = output.decode(errors="surrogateescape").split("\0")
            for relative in paths:
                if not relative:
                    continue
                candidate = resolved_workspace / relative
                if candidate.is_symlink():
                    content = os.readlink(candidate).encode(errors="surrogateescape")
                    if any(secret in content for secret in secrets):
                        raise WorkspaceError("Archive content contains a configured secret")
                    continue
                if not candidate.is_file():
                    continue
                self._assert_file_has_no_secret(candidate, secrets)
        except WorkspaceError:
            raise
        except (subprocess.SubprocessError, OSError) as error:
            raise WorkspaceError(f"Unable to scan archive content safely: {error}") from error

    def remove_verified(
        self,
        *,
        project: dict[str, Any],
        task_id: str,
        workspace: Path,
        expected_branch: str,
        archive_ref: str,
        archive_commit: str,
    ) -> None:
        """Remove exactly one proven task worktree through Git, never recursive deletion."""
        repository = Path(project["repository_path"]).resolve()
        resolved_workspace = self._assert_archive_target(
            project=project, task_id=task_id, workspace=workspace
        )
        self.verify(
            project=project,
            workspace=resolved_workspace,
            expected_branch=expected_branch,
        )
        self.verify_archive_ref(
            project=project,
            archive_ref=archive_ref,
            archive_commit=archive_commit,
        )
        try:
            self._git(repository, ["worktree", "remove", "--force", str(resolved_workspace)])
        except (subprocess.SubprocessError, OSError) as error:
            raise WorkspaceError(f"Unable to remove archived worktree: {error}") from error
        self.verify_removed(project=project, task_id=task_id, workspace=resolved_workspace)

    def verify_removed(
        self,
        *,
        project: dict[str, Any],
        task_id: str,
        workspace: Path,
    ) -> None:
        repository = Path(project["repository_path"]).resolve()
        resolved_workspace = self._assert_archive_target(
            project=project, task_id=task_id, workspace=workspace
        )
        try:
            listed = self._listed_worktrees(repository)
        except (subprocess.SubprocessError, OSError) as error:
            raise WorkspaceError(f"Unable to verify worktree removal: {error}") from error
        if any(Path(str(item["path"])).resolve() == resolved_workspace for item in listed):
            raise WorkspaceError("Archived worktree remains registered with Git")
        if resolved_workspace.exists():
            raise WorkspaceError("Archived worktree directory still exists")

    def verify_archive_ref(
        self,
        *,
        project: dict[str, Any],
        archive_ref: str,
        archive_commit: str,
    ) -> None:
        repository = Path(project["repository_path"]).resolve()
        expected = self.resolve_commit(repository, archive_commit)
        actual = self.resolve_commit(repository, archive_ref)
        if actual != expected or actual != archive_commit:
            raise WorkspaceError("Archive ref does not resolve to the recorded archive commit")

    def resolve_commit(self, repository: Path, revision: str) -> str:
        try:
            return self._git(repository, ["rev-parse", "--verify", f"{revision}^{{commit}}"])
        except (subprocess.SubprocessError, OSError) as error:
            raise WorkspaceError(f"Git revision is not a readable commit: {revision}") from error

    @staticmethod
    def _git(
        cwd: Path,
        arguments: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            env={**os.environ, **(env or {})},
        )
        return result.stdout.strip()

    def verify(
        self, *, project: dict[str, Any], workspace: Path, expected_branch: str
    ) -> dict[str, Any]:
        """Prove that a path is the expected linked worktree, not merely a Git directory."""
        repository = Path(project["repository_path"]).resolve()
        resolved_workspace = workspace.resolve()
        try:
            project_common = Path(
                self._git(repository, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
            ).resolve()
            workspace_common = Path(
                self._git(
                    resolved_workspace,
                    ["rev-parse", "--path-format=absolute", "--git-common-dir"],
                )
            ).resolve()
            branch = self._git(resolved_workspace, ["branch", "--show-current"])
            head = self._git(resolved_workspace, ["rev-parse", "HEAD"])
            listed = self._listed_worktrees(repository)
        except (subprocess.SubprocessError, OSError) as error:
            raise WorkspaceError(f"Unable to verify worktree identity: {error}") from error

        entry = next(
            (item for item in listed if Path(str(item["path"])).resolve() == resolved_workspace),
            None,
        )
        if entry is None:
            raise WorkspaceError("Prepared path is not registered in git worktree list")
        if project_common != workspace_common:
            raise WorkspaceError("Prepared worktree belongs to a different Git repository")
        if entry.get("prunable"):
            raise WorkspaceError("Prepared worktree is stale or prunable")
        if branch != expected_branch:
            raise WorkspaceError(
                f"Prepared worktree branch mismatch: expected {expected_branch}, found {branch}"
            )
        listed_branch = entry.get("branch")
        if listed_branch != f"refs/heads/{expected_branch}":
            raise WorkspaceError("Registered worktree branch identity does not match the task")
        if entry.get("head") != head:
            raise WorkspaceError("Registered worktree HEAD identity does not match the task")
        return {
            "verified": True,
            "workspace_path": str(resolved_workspace),
            "git_common_dir": str(workspace_common),
            "branch": branch,
            "head": head,
            "locked": bool(entry.get("locked")),
            "prunable": False,
        }

    def _assert_archive_target(
        self,
        *,
        project: dict[str, Any],
        task_id: str,
        workspace: Path,
    ) -> Path:
        expected = self.expected_path(project_id=str(project["id"]), task_id=task_id)
        resolved = workspace.resolve()
        if resolved != expected:
            raise WorkspaceError("Archive target is not the task's managed worktree path")
        return resolved

    @staticmethod
    def _validate_identifier(value: str, kind: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
            raise WorkspaceError(f"Invalid {kind} identifier for managed worktree path")

    @staticmethod
    def _assert_file_has_no_secret(path: Path, secrets: list[bytes]) -> None:
        overlap = max(len(secret) for secret in secrets) - 1
        previous = b""
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                content = previous + chunk
                if any(secret in content for secret in secrets):
                    raise WorkspaceError("Archive content contains a configured secret")
                previous = content[-overlap:] if overlap else b""

    @staticmethod
    def _listed_worktrees(repository: Path) -> list[dict[str, str | bool]]:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain", "-z"],
            cwd=repository,
            capture_output=True,
            timeout=10,
            check=True,
        )
        tokens = result.stdout.decode(errors="replace").split("\0")
        entries: list[dict[str, str | bool]] = []
        current: dict[str, str | bool] = {}
        for token in tokens:
            line = token.strip()
            if not line:
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            if key == "worktree" and current:
                entries.append(current)
                current = {}
            if key == "worktree":
                current["path"] = value
            elif key in {"HEAD", "branch"}:
                current[key.lower()] = value
            elif key in {"locked", "prunable", "bare", "detached"}:
                current[key] = value or True
        if current:
            entries.append(current)
        return entries
