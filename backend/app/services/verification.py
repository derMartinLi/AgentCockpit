from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from backend.app.domain.models import VerificationCommand, now_iso


class RepositoryVerificationInspector:
    """Detect repository-native checks without executing project code."""

    SCRIPT_KINDS = {
        "test": "test",
        "lint": "lint",
        "typecheck": "typecheck",
        "type-check": "typecheck",
        "build": "build",
    }

    def inspect(self, repository: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        root = repository.resolve()
        manifests: list[str] = []
        ecosystems: list[str] = []
        frameworks: set[str] = set()
        commands: list[VerificationCommand] = []

        package_json = root / "package.json"
        if package_json.is_file():
            manifests.append("package.json")
            ecosystems.append("node")
            self._inspect_node(root, package_json, frameworks, commands)

        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            manifests.append("pyproject.toml")
            ecosystems.append("python")
            self._inspect_python(root, pyproject, frameworks, commands)

        return (
            {
                "manifests": manifests,
                "ecosystems": ecosystems,
                "frameworks": sorted(frameworks),
                "detected_at": now_iso(),
            },
            [command.model_dump() for command in self._deduplicate(commands)],
        )

    def _inspect_node(
        self,
        root: Path,
        package_json: Path,
        frameworks: set[str],
        commands: list[VerificationCommand],
    ) -> None:
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        scripts = package.get("scripts") if isinstance(package, dict) else {}
        scripts = scripts if isinstance(scripts, dict) else {}
        dependencies = {
            str(name).lower()
            for group in (package.get("dependencies", {}), package.get("devDependencies", {}))
            if isinstance(group, dict)
            for name in group
        }
        framework_names = {
            "vitest": "Vitest",
            "jest": "Jest",
            "mocha": "Mocha",
            "@playwright/test": "Playwright",
            "cypress": "Cypress",
        }
        frameworks.update(label for name, label in framework_names.items() if name in dependencies)
        if any("node --test" in str(script) for script in scripts.values()):
            frameworks.add("Node test runner")

        if (root / "pnpm-lock.yaml").exists():
            runner = "pnpm"
        elif (root / "yarn.lock").exists():
            runner = "yarn"
        elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
            runner = "bun"
        else:
            runner = "npm"
        runner_executable = f"{runner}.cmd" if os.name == "nt" else runner
        for script_name, kind in self.SCRIPT_KINDS.items():
            if script_name not in scripts:
                continue
            command = (
                [runner_executable, script_name]
                if runner in {"pnpm", "yarn", "bun"}
                else [runner_executable, "run", script_name]
            )
            commands.append(
                VerificationCommand(
                    id=f"node-{script_name.replace('_', '-')}",
                    name=f"Node {script_name}",
                    kind=kind,
                    command=command,
                    source="detected",
                )
            )

    def _inspect_python(
        self,
        root: Path,
        pyproject: Path,
        frameworks: set[str],
        commands: list[VerificationCommand],
    ) -> None:
        try:
            config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return
        tool = config.get("tool", {}) if isinstance(config, dict) else {}
        project = config.get("project", {}) if isinstance(config, dict) else {}
        dependencies = project.get("dependencies", []) if isinstance(project, dict) else []
        dependency_text = " ".join(str(item).lower() for item in dependencies)
        dev_groups = config.get("dependency-groups", {}) if isinstance(config, dict) else {}
        dependency_text += " " + json.dumps(dev_groups).lower()
        runner = ["uv", "run"] if (root / "uv.lock").exists() else ["python", "-m"]

        if (isinstance(tool, dict) and "pytest" in tool) or "pytest" in dependency_text:
            frameworks.add("pytest")
            command = [*runner, "pytest"]
            commands.append(
                VerificationCommand(
                    id="python-pytest",
                    name="Python tests",
                    kind="test",
                    command=command,
                    source="detected",
                )
            )
        if (isinstance(tool, dict) and "ruff" in tool) or re.search(r"\bruff\b", dependency_text):
            frameworks.add("Ruff")
            command = (
                ["uv", "run", "ruff", "check", "."]
                if runner[0] == "uv"
                else ["ruff", "check", "."]
            )
            commands.append(
                VerificationCommand(
                    id="python-ruff",
                    name="Python lint",
                    kind="lint",
                    command=command,
                    source="detected",
                )
            )

    @staticmethod
    def _deduplicate(commands: list[VerificationCommand]) -> list[VerificationCommand]:
        unique: dict[str, VerificationCommand] = {}
        for command in commands:
            unique.setdefault(command.id, command)
        return list(unique.values())


def validate_verification_commands(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    commands = [VerificationCommand.model_validate(value) for value in values]
    ids = [command.id for command in commands]
    if len(ids) != len(set(ids)):
        raise ValueError("Verification command ids must be unique")
    for command in commands:
        if Path(command.cwd).is_absolute() or ".." in Path(command.cwd).parts:
            raise ValueError("Verification command cwd must stay inside the Task Workspace")
    return [command.model_dump() for command in commands]
