from __future__ import annotations

import asyncio
import json
import re
import shlex
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from backend.app.domain.models import AgentResult, AgentResultStatus, TaskSpec
from backend.app.services.verification import RepositoryVerificationInspector

EventCallback = Callable[[str, dict[str, Any]], None]
CheckCallback = Callable[[str, list[str], str, Callable[[], str]], str]


@dataclass(frozen=True, slots=True)
class AgentResumeContext:
    instruction: str
    previous_run_id: str | None
    previous_status: str | None
    previous_summary: str | None
    context_path: str = ".task/context.md"


class AgentCheckOutput(BaseModel):
    """Strict transport schema used only for model-produced final results."""

    name: str
    status: str
    detail: str | None


class AgentResultOutput(BaseModel):
    """All fields are required so provider-native strict JSON Schema can validate them."""

    status: AgentResultStatus
    summary: str
    changes: list[str]
    checks: list[AgentCheckOutput]
    known_issues: list[str]
    risks: list[str]
    needs_human: bool

    def to_domain(self) -> AgentResult:
        return AgentResult.model_validate(self.model_dump())


class ToolBoundaryError(ValueError):
    pass


class RunControl:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[str]] = set()

    def register(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.add(process)

    def unregister(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.discard(process)

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            if process.poll() is None:
                process.terminate()


class WorkspaceTools:
    BLOCKED_GIT_ACTIONS = {"branch", "checkout", "merge", "push", "reset", "switch", "worktree"}

    def __init__(
        self,
        workspace: Path,
        *,
        timeout_seconds: int,
        emit: EventCallback,
        control: RunControl | None = None,
        record_check: CheckCallback | None = None,
        configured_checks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.timeout_seconds = timeout_seconds
        self.emit = emit
        self.control = control or RunControl()
        self.record_check = record_check
        self.configured_checks = configured_checks or []
        self.executed_project_checks: set[str] = set()
        self.verification_inspector = RepositoryVerificationInspector()

    def resolve(self, relative_path: str = ".") -> Path:
        candidate = Path(relative_path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.workspace / candidate).resolve()
        )
        if not resolved.is_relative_to(self.workspace):
            self._reject(
                "Path is outside the assigned task worktree",
                tool="path",
                target=relative_path,
            )
        return resolved

    def _reject(self, reason: str, *, tool: str, target: str | None = None) -> None:
        payload = {"tool": tool, "reason": reason}
        if target is not None:
            payload["target"] = target[:500]
        self.emit("ToolRejected", payload)
        raise ToolBoundaryError(reason)

    def list_files(self, path: str = ".") -> str:
        root = self.resolve(path)
        self.emit("ToolStarted", {"tool": "list_files", "path": path})
        if not root.exists():
            raise FileNotFoundError(path)
        files = [
            item.relative_to(self.workspace).as_posix()
            for item in root.rglob("*")
            if item.is_file() and ".git" not in item.parts
        ][:500]
        self.emit("ToolFinished", {"tool": "list_files", "count": len(files)})
        return "\n".join(files)

    def search_code(self, query: str, path: str = ".") -> str:
        root = self.resolve(path)
        self.emit("ToolStarted", {"tool": "search_code", "query": query, "path": path})
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        matches: list[str] = []
        for file_path in root.rglob("*"):
            if not file_path.is_file() or ".git" in file_path.parts:
                continue
            try:
                for line_number, line in enumerate(
                    file_path.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    if pattern.search(line):
                        matches.append(
                            f"{file_path.relative_to(self.workspace).as_posix()}:{line_number}:{line[:300]}"
                        )
                    if len(matches) >= 200:
                        break
            except (UnicodeDecodeError, OSError):
                continue
            if len(matches) >= 200:
                break
        self.emit("ToolFinished", {"tool": "search_code", "count": len(matches)})
        return "\n".join(matches)

    def read_file(self, path: str) -> str:
        file_path = self.resolve(path)
        self.emit("ToolStarted", {"tool": "read_file", "path": path})
        content = file_path.read_text(encoding="utf-8")
        self.emit("ToolFinished", {"tool": "read_file", "characters": len(content)})
        return content[:100_000]

    def write_file(self, path: str, content: str) -> str:
        file_path = self.resolve(path)
        self.emit("ToolStarted", {"tool": "write_file", "path": path})
        file_path.parent.mkdir(parents=True, exist_ok=True)
        existed = file_path.exists()
        file_path.write_text(content, encoding="utf-8")
        self.emit("FileChanged" if existed else "FileCreated", {"path": path})
        self.emit("ToolFinished", {"tool": "write_file", "characters": len(content)})
        return f"Wrote {len(content)} characters to {path}"

    def run_command(self, command: list[str], cwd: str = ".") -> str:
        if not command or not all(isinstance(item, str) and item for item in command):
            self._reject("Command must be a non-empty string array", tool="run_command")
        if command[0].lower() in {"cmd", "cmd.exe", "powershell", "pwsh", "bash", "sh"}:
            self._reject(
                "Shell interpreters are not allowed",
                tool="run_command",
                target=command[0],
            )
        if (
            command[0].lower() == "git"
            and len(command) > 1
            and command[1].lower() in self.BLOCKED_GIT_ACTIONS
        ):
            self._reject(
                "Git lifecycle commands are controlled by Task Runtime",
                tool="run_command",
                target=f"git {command[1]}",
            )
        working_directory = self.resolve(cwd)
        self.emit("CommandStarted", {"command": command, "cwd": cwd})
        started = time.monotonic()
        process = subprocess.Popen(
                command,
                cwd=working_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
            )
        self.control.register(process)
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if self.control.cancelled.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    self.emit("CommandFinished", {"command": command, "status": "cancelled"})
                    raise ToolBoundaryError("Command cancelled")
                if time.monotonic() - started > self.timeout_seconds:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    self.emit("CommandFinished", {"command": command, "status": "timeout"})
                    raise ToolBoundaryError(
                        f"Command exceeded {self.timeout_seconds} second timeout"
                    )
        finally:
            self.control.unregister(process)
        output = ((stdout or "") + (stderr or ""))[-20_000:]
        self.emit(
            "CommandFinished",
            {"command": command, "exit_code": process.returncode, "output": output[-2000:]},
        )
        return f"exit_code={process.returncode}\n{output}"

    def git_status(self) -> str:
        return self.run_command(["git", "status", "--short"])

    def git_diff(self) -> str:
        return self.run_command(["git", "diff", "--no-ext-diff"])

    def run_check(self, name: str, command: list[str], cwd: str = ".") -> str:
        def runner() -> str:
            return self.run_command(command, cwd)

        if self.record_check:
            return self.record_check(name, command, cwd, runner)
        return runner()

    def list_project_checks(self) -> str:
        """Return configured and repository-detected checks available to this Task."""
        return json.dumps(self.available_project_checks(), ensure_ascii=False)

    def available_project_checks(self) -> list[dict[str, Any]]:
        _profile, detected = self.verification_inspector.inspect(self.workspace)
        combined = {item["id"]: item for item in detected}
        combined.update({item["id"]: item for item in self.configured_checks})
        return list(combined.values())

    def run_project_check(self, check_id: str) -> str:
        selected = next(
            (item for item in self.available_project_checks() if item["id"] == check_id),
            None,
        )
        if not selected:
            self._reject(
                "Unknown project check; call list_project_checks first",
                tool="run_project_check",
                target=check_id,
            )
        self.executed_project_checks.add(check_id)
        return self.run_check(selected["name"], selected["command"], selected.get("cwd", "."))

    def run_automatic_checks(self) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for check in self.available_project_checks():
            if not check.get("auto_run", True) or check["id"] in self.executed_project_checks:
                continue
            try:
                output = self.run_project_check(check["id"])
                first_line = output.splitlines()[0] if output else ""
                passed = first_line == "exit_code=0"
                results.append(
                    {
                        "name": check["name"],
                        "status": "passed" if passed else "failed",
                        "detail": output[-4000:],
                    }
                )
            except Exception as error:
                results.append(
                    {"name": check["name"], "status": "failed", "detail": str(error)}
                )
        return results

    def langchain_tools(self) -> list[BaseTool]:
        workspace_tools = self

        @tool
        def list_files(path: str = ".") -> str:
            """List files under a relative directory in the task worktree."""
            return workspace_tools.list_files(path)

        @tool
        def search_code(query: str, path: str = ".") -> str:
            """Search text in files under a relative worktree directory."""
            return workspace_tools.search_code(query, path)

        @tool
        def read_file(path: str) -> str:
            """Read a UTF-8 text file relative to the task worktree."""
            return workspace_tools.read_file(path)

        @tool
        def write_file(path: str, content: str) -> str:
            """Create or replace a UTF-8 text file relative to the task worktree."""
            return workspace_tools.write_file(path, content)

        @tool
        def run_command(command: list[str], cwd: str = ".") -> str:
            """Run a program without a shell inside the task worktree."""
            return workspace_tools.run_command(command, cwd)

        @tool
        def git_status() -> str:
            """Return concise Git status for the task worktree."""
            return workspace_tools.git_status()

        @tool
        def git_diff() -> str:
            """Return the current uncommitted Git diff for the task worktree."""
            return workspace_tools.git_diff()

        @tool
        def run_check(name: str, command: list[str], cwd: str = ".") -> str:
            """Run and persist a named test, lint, type-check, or build check."""
            return workspace_tools.run_check(name, command, cwd)

        @tool
        def list_project_checks() -> str:
            """List repository-detected and user-configured verification checks."""
            return workspace_tools.list_project_checks()

        @tool
        def run_project_check(check_id: str) -> str:
            """Run a named Project verification check by id and persist its result."""
            return workspace_tools.run_project_check(check_id)

        return [
            list_files,
            search_code,
            read_file,
            write_file,
            run_command,
            run_check,
            list_project_checks,
            run_project_check,
            git_status,
            git_diff,
        ]


class AgentAdapter(ABC):
    @abstractmethod
    async def execute(
        self,
        *,
        spec: TaskSpec,
        environment_spec: str,
        tools: WorkspaceTools,
        resume_context: AgentResumeContext | None = None,
    ) -> AgentResult:
        raise NotImplementedError


class DemoAgentAdapter(AgentAdapter):
    """Deterministic local adapter used for setup and repeatable acceptance tests."""

    async def execute(
        self,
        *,
        spec: TaskSpec,
        environment_spec: str,
        tools: WorkspaceTools,
        resume_context: AgentResumeContext | None = None,
    ) -> AgentResult:
        del resume_context
        tools.emit(
            "AgentProgress",
            {
                "step": 1,
                "phase": "implementing",
                "message": "正在创建确定性的 Workspace 变更。",
            },
        )
        content = (
            "# Agent Cockpit Observable Result\n\n"
            f"Goal: {spec.goal}\n\n"
            "This file was created by the deterministic local adapter inside the task worktree.\n"
        )
        await asyncio.to_thread(tools.write_file, "agent-cockpit-demo.md", content)
        tools.emit(
            "AgentProgress",
            {
                "step": 2,
                "phase": "checking",
                "message": "正在检查最终 Git Workspace 状态。",
            },
        )
        status = await asyncio.to_thread(
            tools.run_check, "git status", ["git", "status", "--short"]
        )
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            summary="Created a deterministic observable demonstration change.",
            changes=["Created agent-cockpit-demo.md inside the isolated worktree"],
            checks=[{"name": "git status", "status": "passed", "detail": status}],
        )


class BuiltinLangChainAgentAdapter(AgentAdapter):
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        max_steps: int = 24,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.max_steps = max_steps

    def _build_llm(self) -> Any:
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=0,
        )

    def _finalization_strategies(self) -> tuple[tuple[str, bool | None], ...]:
        if self.base_url:
            return (("function_calling", None), ("json_schema", True))
        return (("json_schema", True), ("function_calling", None))

    def _finalization_instruction(self, *, retry: bool) -> str:
        retry_note = " The previous formatting attempt was invalid; correct it." if retry else ""
        return (
            "Finalize the run from the observable execution record below. Return the validated "
            "AgentResult fields only. Report checks and changes factually; do not infer success "
            f"when tool evidence shows a failure.{retry_note}"
        )

    async def execute(
        self,
        *,
        spec: TaskSpec,
        environment_spec: str,
        tools: WorkspaceTools,
        resume_context: AgentResumeContext | None = None,
    ) -> AgentResult:
        llm = self._build_llm()
        langchain_tools = tools.langchain_tools()
        tool_map = {item.name: item for item in langchain_tools}
        model_with_tools = llm.bind_tools(langchain_tools)
        messages: list[Any] = [
            SystemMessage(
                content=(
                    "You are the built-in coding agent for a local task worktree. "
                    "Inspect before editing, keep changes within scope, run relevant checks, "
                    "use list_project_checks and run_project_check instead of guessing "
                    "test commands, "
                    "and never "
                    "attempt branch, worktree, merge, push, checkout, switch, or reset operations. "
                    "When the work is finished, stop calling tools and provide a concise, "
                    "factual completion summary. A separate validated finalization step will "
                    "produce the stored result."
                )
            ),
            HumanMessage(
                content=(
                    f"Task Spec:\n{spec.model_dump_json(indent=2)}\n\n"
                    f"Environment Spec:\n{environment_spec or 'Use repository conventions.'}"
                    f"{self._resume_prompt(resume_context)}"
                )
            ),
        ]
        for step in range(1, self.max_steps + 1):
            tools.emit(
                "AgentProgress",
                {
                    "step": step,
                    "phase": "planning",
                    "message": "正在根据当前 Task 状态选择下一步可观察动作。",
                },
            )
            response = await model_with_tools.ainvoke(messages)
            messages.append(response)
            if not response.tool_calls:
                tools.emit(
                    "AgentProgress",
                    {
                        "step": step,
                        "phase": "finalizing",
                        "message": "正在生成结构化执行摘要。",
                    },
                )
                return await self._finalize(llm=llm, messages=messages, tools=tools)
            tool_names = [tool_call["name"] for tool_call in response.tool_calls]
            tools.emit(
                "AgentProgress",
                {
                    "step": step,
                    "phase": "acting",
                    "message": f"准备执行 {len(tool_names)} 个可观察工具动作。",
                    "tools": tool_names,
                },
            )
            for tool_call in response.tool_calls:
                selected = tool_map.get(tool_call["name"])
                if selected is None:
                    messages.append(
                        ToolMessage(
                            content="Unknown tool",
                            tool_call_id=tool_call["id"],
                            name=tool_call["name"],
                        )
                    )
                    continue
                started = time.monotonic()
                tools.emit(
                    "AgentStepStarted",
                    {
                        "step": step,
                        "tool": tool_call["name"],
                        "target": self._public_tool_target(tool_call["args"]),
                    },
                )
                try:
                    output = await selected.ainvoke(tool_call["args"])
                    step_status = "completed"
                except Exception as error:  # tool failures are returned to the model
                    output = f"Tool error: {error}"
                    step_status = "failed"
                tools.emit(
                    "AgentStepFinished",
                    {
                        "step": step,
                        "tool": tool_call["name"],
                        "status": step_status,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                )
                messages.append(
                    ToolMessage(
                        content=str(output),
                        tool_call_id=tool_call["id"],
                        name=tool_call["name"],
                    )
                )
        return AgentResult(
            status=AgentResultStatus.BLOCKED,
            summary=f"Agent exceeded the {self.max_steps}-step execution limit.",
            known_issues=["Execution step limit reached"],
            needs_human=True,
        )

    @staticmethod
    def _public_tool_target(arguments: dict[str, Any]) -> str | None:
        """Describe observable intent without persisting prompts, file content, or reasoning."""
        for key in ("path", "cwd", "name"):
            value = arguments.get(key)
            if isinstance(value, str):
                return value[:300]
        command = arguments.get("command")
        if isinstance(command, list) and command:
            return str(command[0])[:300]
        return None

    @staticmethod
    def _resume_prompt(resume_context: AgentResumeContext | None) -> str:
        if resume_context is None:
            return ""
        return (
            "\n\nResume Context:\n"
            "This is a resumed run in an existing workspace. Preserve completed work, "
            "inspect the current state, and only address unfinished or newly requested work.\n"
            f"Previous run: {resume_context.previous_run_id or 'unknown'} "
            f"({resume_context.previous_status or 'unknown'})\n"
            f"Previous summary: {resume_context.previous_summary or 'No previous summary.'}\n"
            f"User continuation instruction: {resume_context.instruction}\n"
            f"Detailed diff and human-input history: {resume_context.context_path}"
        )

    async def _finalize(
        self,
        *,
        llm: Any,
        messages: list[Any],
        tools: WorkspaceTools,
    ) -> AgentResult:
        strategies = self._finalization_strategies()
        execution_record = self._observable_execution_record(messages)
        errors: list[str] = []
        for index, (method, strict) in enumerate(strategies):
            try:
                final_messages = [
                    SystemMessage(content=self._finalization_instruction(retry=index > 0)),
                    HumanMessage(content=f"Observable execution record:\n{execution_record}"),
                ]
                finalizer = llm.with_structured_output(
                    AgentResultOutput,
                    method=method,
                    include_raw=True,
                    strict=strict,
                )
                envelope = await finalizer.ainvoke(final_messages)
                parsed = envelope.get("parsed") if isinstance(envelope, dict) else envelope
                if parsed is None:
                    parsing_error = (
                        envelope.get("parsing_error") if isinstance(envelope, dict) else None
                    )
                    raise ValueError(
                        f"Structured result was empty: {type(parsing_error).__name__}"
                    )
                output = (
                    parsed
                    if isinstance(parsed, AgentResultOutput)
                    else AgentResultOutput.model_validate(parsed)
                )
                return output.to_domain()
            except Exception as error:  # provider capability and parse failures retry once
                errors.append(type(error).__name__)
                if index == 0:
                    tools.emit(
                        "AgentResultFinalizationRetry",
                        {"method": method, "error": type(error).__name__},
                    )

        tools.emit(
            "AgentResultProtocolError",
            {"code": "RESULT_FORMAT_INVALID", "errors": errors},
        )
        return AgentResult(
            status=AgentResultStatus.FAILED,
            summary="Agent execution finished, but its final result could not be validated.",
            needs_human=True,
            protocol_error="RESULT_FORMAT_INVALID",
        )

    @staticmethod
    def _observable_execution_record(messages: list[Any]) -> str:
        """Flatten provider messages so finalization never replays tool-call protocol state."""
        records: list[str] = []
        for message in messages:
            content = getattr(message, "content", None)
            if not isinstance(content, str) or not content.strip():
                continue
            if isinstance(message, ToolMessage):
                label = f"Tool {message.name or 'unknown'}"
            elif isinstance(message, SystemMessage):
                label = "Instructions"
            elif isinstance(message, HumanMessage):
                label = "Task context"
            else:
                label = "Agent summary"
            records.append(f"[{label}]\n{content[:24000]}")
        return "\n\n".join(records)[-64000:]


class DeepSeekAgentAdapter(BuiltinLangChainAgentAdapter):
    """DeepSeek V4 adapter using its supported non-thinking JSON output protocol."""

    def _build_llm(self) -> Any:
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=0,
            extra_body={"thinking": {"type": "disabled"}},
        )

    def _finalization_strategies(self) -> tuple[tuple[str, bool | None], ...]:
        return (("json_mode", None), ("json_mode", None))

    def _finalization_instruction(self, *, retry: bool) -> str:
        schema = json.dumps(AgentResultOutput.model_json_schema(), ensure_ascii=False)
        retry_note = " The previous JSON object was invalid; correct it." if retry else ""
        return (
            "Return exactly one JSON object matching the supplied JSON Schema. Do not use "
            "Markdown fences or add fields. Base every field on the observable execution record; "
            "do not infer success when tool evidence shows a failure."
            f"{retry_note}\nJSON Schema:\n{schema}"
        )


def is_deepseek_endpoint(base_url: str | None) -> bool:
    if not base_url:
        return False
    hostname = (urlparse(base_url).hostname or "").lower()
    return hostname == "deepseek.com" or hostname.endswith(".deepseek.com")


def parse_command(command: str) -> list[str]:
    """Parse a display command without invoking a shell."""
    return shlex.split(command, posix=False)
