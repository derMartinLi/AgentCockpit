from __future__ import annotations

import fnmatch
import ntpath
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from backend.app.domain.models import ModuleRule
from backend.app.repositories.store import ProjectRepository
from backend.app.services.verification import (
    RepositoryVerificationInspector,
    validate_verification_commands,
)


class ProjectValidationError(ValueError):
    pass


def normalize_local_path(raw_path: str, *, base_directory: Path | None = None) -> Path:
    """Normalize a user-entered local path without guessing ambiguous Windows semantics."""
    value = raw_path.strip()
    if not value:
        raise ProjectValidationError("Repository path cannot be empty")
    if "\x00" in value:
        raise ProjectValidationError("Repository path contains an invalid null character")
    if value[0] in {'"', "'"} or value[-1] in {'"', "'"}:
        if len(value) < 2 or value[0] != value[-1]:
            raise ProjectValidationError("Repository path contains unmatched quotes")
        value = value[1:-1].strip()
        if not value:
            raise ProjectValidationError("Repository path cannot be empty")
    if value.lower().startswith("file:"):
        raise ProjectValidationError("Use a local filesystem path instead of a file:// URL")

    expanded = os.path.expandvars(os.path.expanduser(value))
    drive, drive_tail = ntpath.splitdrive(expanded)
    if re.fullmatch(r"[A-Za-z]:", drive) and not drive_tail.startswith(("\\", "/")):
        raise ProjectValidationError(
            "Drive-relative paths such as G:project are ambiguous. "
            "Use G:\\project or G:/project."
        )

    normalized = Path(os.path.normpath(expanded))
    if not normalized.is_absolute():
        normalized = (base_directory or Path.cwd()) / normalized
    try:
        return normalized.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectValidationError(
            "Repository path does not exist or cannot be resolved. "
            "Examples: G:\\Projects\\my-app, G:/Projects/my-app, or ../my-app."
        ) from error


class ModuleMapper:
    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.rules = [ModuleRule.model_validate(rule) for rule in rules]

    def match(self, file_path: str) -> list[str]:
        normalized = PurePosixPath(file_path.replace("\\", "/")).as_posix().lstrip("./")
        matches: list[str] = []
        for rule in self.rules:
            if any(fnmatch.fnmatchcase(normalized, pattern.lstrip("./")) for pattern in rule.paths):
                matches.append(rule.name)
        return matches


class ProjectService:
    def __init__(
        self,
        repository: ProjectRepository,
        verification_inspector: RepositoryVerificationInspector | None = None,
    ) -> None:
        self.repository = repository
        self.verification_inspector = verification_inspector or RepositoryVerificationInspector()

    def create(
        self,
        *,
        name: str,
        repository_path: str,
        environment_spec: str,
        module_mapping: list[dict[str, Any]],
        verification_commands: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        root, branch = self._validate_repository(repository_path)
        if self.repository.get_by_path(str(root)):
            raise ProjectValidationError("This repository is already registered")
        repository_profile, detected_commands = self.verification_inspector.inspect(root)
        commands = validate_verification_commands(
            verification_commands if verification_commands is not None else detected_commands
        )
        return self.repository.create(
            name=name.strip(),
            repository_path=str(root),
            default_branch=branch,
            environment_spec=environment_spec,
            module_mapping=module_mapping,
            repository_profile=repository_profile,
            verification_commands=commands,
        )

    def update(self, project_id: str, values: dict[str, Any]) -> dict[str, Any]:
        if not self.repository.get(project_id):
            raise KeyError(project_id)
        if "module_mapping" in values:
            ModuleMapper(values["module_mapping"])
        if "verification_commands" in values:
            values["verification_commands"] = validate_verification_commands(
                values["verification_commands"]
            )
        return self.repository.update(project_id, values)  # type: ignore[return-value]

    def discover_verification(self, project_id: str) -> dict[str, Any]:
        project = self.repository.get(project_id)
        if not project:
            raise KeyError(project_id)
        profile, commands = self.verification_inspector.inspect(
            Path(project["repository_path"])
        )
        return self.repository.update(
            project_id,
            {"repository_profile": profile, "verification_commands": commands},
        )  # type: ignore[return-value]

    def delete(self, project_id: str) -> None:
        if not self.repository.get(project_id):
            raise KeyError(project_id)
        try:
            self.repository.delete(project_id)
        except ValueError as error:
            raise ProjectValidationError(str(error)) from error

    @staticmethod
    def _validate_repository(repository_path: str) -> tuple[Path, str]:
        candidate = normalize_local_path(repository_path)
        if not candidate.is_dir():
            raise ProjectValidationError("Repository path does not exist or is not a directory")
        try:
            root_result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=candidate,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            root = Path(root_result.stdout.strip()).resolve()
            branch_result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise ProjectValidationError("Path is not a usable Git repository") from error
        branch = branch_result.stdout.strip() or "main"
        return root, branch
