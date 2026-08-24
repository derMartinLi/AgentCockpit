from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from backend.app.core.config import Settings
from backend.app.core.credentials import CredentialStore
from backend.app.domain.models import (
    AgentCheck,
    AgentResult,
    AgentResultStatus,
    Event,
    RuntimePhase,
    TaskSpec,
    TaskStatus,
    now_iso,
)
from backend.app.repositories.store import (
    CheckRepository,
    EventRepository,
    HumanInputRepository,
    ProjectRepository,
    ProviderRepository,
    RunRepository,
    TaskRepository,
)
from backend.app.services.agent import (
    AgentAdapter,
    AgentResumeContext,
    BuiltinLangChainAgentAdapter,
    DeepSeekAgentAdapter,
    DemoAgentAdapter,
    RunControl,
    WorkspaceTools,
    is_deepseek_endpoint,
)
from backend.app.services.observer import WorkspaceObserver
from backend.app.services.refinement import RefinementService
from backend.app.services.workspace import WorkspaceManager


class TaskStateError(ValueError):
    pass


class RunCapacityError(TaskStateError):
    code = "RUN_CAPACITY_REACHED"

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"{self.code}: maximum of {limit} concurrent runs reached")


class TaskService:
    def __init__(
        self,
        *,
        tasks: TaskRepository,
        projects: ProjectRepository,
        providers: ProviderRepository,
        credentials: CredentialStore,
        events: EventRepository,
        refinement: RefinementService,
    ) -> None:
        self.tasks = tasks
        self.projects = projects
        self.providers = providers
        self.credentials = credentials
        self.events = events
        self.refinement = refinement

    def create(self, project_id: str, title: str, raw_request: str) -> dict[str, Any]:
        if not self.projects.get(project_id):
            raise KeyError(project_id)
        task = self.tasks.create(project_id=project_id, title=title, raw_request=raw_request)
        return self._assess(task)

    def refine(self, task_id: str) -> dict[str, Any]:
        task = self._require(task_id)
        if task["status"] != TaskStatus.DRAFT:
            raise TaskStateError("Only DRAFT tasks can be refined")
        next_round = task["refinement_round"] + 1
        if next_round > 2:
            raise TaskStateError("Refinement is limited to two rounds")
        return self._assess(task, round_number=next_round)

    def answer(self, task_id: str, answers: list[dict[str, str]]) -> dict[str, Any]:
        task = self._require(task_id)
        if task["status"] != TaskStatus.DRAFT or not task["questions"]:
            raise TaskStateError("Task has no active refinement questions")
        expected = {question["id"] for question in task["questions"]}
        submitted = {answer["question_id"] for answer in answers}
        if expected != submitted:
            raise TaskStateError("Every active refinement question must be answered")
        combined_answers = [*task["answers"], *answers]
        if task["spec"]:
            spec = self.refinement.apply_answers(
                spec=task["spec"], questions=task["questions"], answers=answers
            )
        else:
            spec = self.refinement.build_spec(
                task["raw_request"], combined_answers, task["inspection"] or {}
            )
        return self.tasks.update(
            task_id,
            answers=combined_answers,
            questions=[],
            spec=spec,
            runtime_phase=None,
        )  # type: ignore[return-value]

    def update_spec(self, task_id: str, spec: TaskSpec) -> dict[str, Any]:
        task = self._require(task_id)
        if task["status"] != TaskStatus.DRAFT:
            raise TaskStateError("Only DRAFT task specs can be edited")
        return self.tasks.update(task_id, spec=spec)  # type: ignore[return-value]

    def confirm(self, task_id: str) -> dict[str, Any]:
        task = self._require(task_id)
        if task["status"] != TaskStatus.DRAFT or not task["spec"]:
            raise TaskStateError("A valid Task Spec is required before confirmation")
        TaskSpec.model_validate(task["spec"])
        return self.tasks.update(
            task_id, status=TaskStatus.READY.value, runtime_phase=None, questions=[]
        )  # type: ignore[return-value]

    def _require(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        return task

    def _assess(
        self, task: dict[str, Any], round_number: int | None = None
    ) -> dict[str, Any]:
        project = self.projects.get(task["project_id"])
        assert project is not None
        inspection = task["inspection"] or self.refinement.inspector.inspect(
            project["repository_path"], task["raw_request"]
        )
        inspection = self._redact_inspection(inspection)
        assessment = self.refinement.assess(
            raw_request=task["raw_request"],
            inspection=inspection,
            provider=self.providers.get(),
            api_key=self.credentials.get_api_key(),
        )
        questions = [item.model_dump() for item in assessment.questions]
        next_round = round_number or task["refinement_round"] + 1
        inspection = {
            **inspection,
            "refinement": {
                "mode": assessment.engine,
                "rationale": assessment.rationale,
                "questions_required": len(questions),
            },
        }
        updated = self.tasks.update(
            task["id"],
            inspection=inspection,
            questions=questions,
            spec=assessment.spec,
            refinement_round=next_round,
            runtime_phase=RuntimePhase.REFINING.value if questions else None,
        )
        self.events.record(
            Event(
                timestamp=now_iso(),
                task_id=task["id"],
                run_id=None,
                type="RefinementCompleted",
                source="agent",
                payload={
                    "questions_required": len(questions),
                    "spec_ready": True,
                    "rationale": assessment.rationale,
                },
            )
        )
        return updated  # type: ignore[return-value]

    def _redact_inspection(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.credentials.redact(value)
        if isinstance(value, dict):
            return {str(key): self._redact_inspection(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_inspection(item) for item in value]
        return value


class TaskRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        tasks: TaskRepository,
        projects: ProjectRepository,
        providers: ProviderRepository,
        runs: RunRepository,
        events: EventRepository,
        checks: CheckRepository,
        human_inputs: HumanInputRepository,
        credentials: CredentialStore,
        workspaces: WorkspaceManager,
        observer: WorkspaceObserver,
        adapter_override: AgentAdapter | None = None,
    ) -> None:
        self.settings = settings
        self.tasks = tasks
        self.projects = projects
        self.providers = providers
        self.runs = runs
        self.events = events
        self.checks = checks
        self.human_inputs = human_inputs
        self.credentials = credentials
        self.workspaces = workspaces
        self.observer = observer
        self.adapter_override = adapter_override
        self.active_runs: dict[str, asyncio.Task[None]] = {}
        self.active_controls: dict[str, RunControl] = {}
        self._start_locks: dict[str, asyncio.Lock] = {}
        self._capacity_lock = asyncio.Lock()

    def prepare(self, task_id: str) -> dict[str, Any]:
        task = self._require(task_id)
        if task["status"] != TaskStatus.READY:
            raise TaskStateError("Only READY tasks can prepare a workspace")
        if task["workspace_path"]:
            return task
        self.tasks.update(task_id, runtime_phase=RuntimePhase.PREPARING.value)
        project = self.projects.get(task["project_id"])
        assert project is not None
        try:
            details = self.workspaces.prepare(project=project, task_id=task_id)
            updated = self.tasks.update(task_id, runtime_phase=None, **details)
            self._emit(task_id, None, "WorkspacePrepared", "runtime", details)
            return updated  # type: ignore[return-value]
        except Exception as error:
            self.tasks.update(task_id, status=TaskStatus.FAILED.value, runtime_phase=None)
            self._emit(
                task_id,
                None,
                "WorkspacePreparationFailed",
                "runtime",
                {"error": f"{type(error).__name__}: {error}"},
            )
            raise

    async def start(
        self,
        task_id: str,
        *,
        previous_run_id: str | None = None,
        resume_context: AgentResumeContext | None = None,
    ) -> dict[str, Any]:
        lock = self._start_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            async with self._capacity_lock:
                task = self._require(task_id)
                if task["status"] != TaskStatus.READY:
                    raise TaskStateError("Only READY tasks can run")
                active_count = sum(
                    not background.done() for background in self.active_runs.values()
                )
                if active_count >= self.settings.max_concurrent_runs:
                    raise RunCapacityError(self.settings.max_concurrent_runs)
                if not task["workspace_path"]:
                    task = await asyncio.to_thread(self.prepare, task_id)
                run = self.runs.create(task_id, previous_run_id)
                run_id = run["id"]
                control = RunControl()
                self.active_controls[run_id] = control
                self.tasks.update(
                    task_id,
                    status=TaskStatus.RUNNING.value,
                    runtime_phase=RuntimePhase.IMPLEMENTING.value,
                )
                self._emit(task_id, run_id, "AgentStarted", "runtime", {})

                background = asyncio.create_task(
                    self._execute(
                        task=task,
                        run_id=run_id,
                        control=control,
                        resume_context=resume_context,
                    ),
                    name=f"agent-run-{run_id}",
                )
                self.active_runs[run_id] = background
                background.add_done_callback(lambda _completed: self._forget(run_id))
                return run

    async def run(self, task_id: str) -> dict[str, Any]:
        """Start and await a run; retained for deterministic service-level callers."""
        run = await self.start(task_id)
        background = self.active_runs.get(run["id"])
        if background:
            await background
        return self.runs.get(run["id"])  # type: ignore[return-value]

    async def _execute(
        self,
        *,
        task: dict[str, Any],
        run_id: str,
        control: RunControl,
        resume_context: AgentResumeContext | None = None,
    ) -> None:
        task_id = task["id"]
        project = self.projects.get(task["project_id"])
        assert project is not None
        tools = WorkspaceTools(
            Path(task["workspace_path"]),
            timeout_seconds=self.settings.command_timeout_seconds,
            emit=lambda event_type, payload: self._emit(
                task_id, run_id, event_type, "agent", payload
            ),
            control=control,
            record_check=lambda name, command, cwd, runner: self._record_check(
                task_id, run_id, name, command, cwd, runner
            ),
            configured_checks=project.get("verification_commands", []),
        )
        observation_stop = asyncio.Event()
        observation = asyncio.create_task(
            self._observe(task, project, run_id, observation_stop),
            name=f"workspace-observer-{run_id}",
        )
        try:
            adapter = self._adapter()
            result = await adapter.execute(
                spec=TaskSpec.model_validate(task["spec"]),
                environment_spec=project["environment_spec"],
                tools=tools,
                resume_context=resume_context,
            )
            if result.status == AgentResultStatus.COMPLETED:
                automatic_checks = await asyncio.to_thread(tools.run_automatic_checks)
                if automatic_checks:
                    failed_names = [
                        item["name"] for item in automatic_checks if item["status"] == "failed"
                    ]
                    result = result.model_copy(
                        update={
                            "checks": [
                                *result.checks,
                                *[AgentCheck.model_validate(item) for item in automatic_checks],
                            ],
                            "known_issues": [
                                *result.known_issues,
                                *[
                                    f"Automatic verification failed: {name}"
                                    for name in failed_names
                                ],
                            ],
                            "needs_human": result.needs_human or bool(failed_names),
                        }
                    )
        except asyncio.CancelledError:
            result = AgentResult(
                status=AgentResultStatus.CANCELLED,
                summary="Agent execution was cancelled; workspace changes were preserved.",
                known_issues=["Run cancelled"],
                needs_human=True,
            )
        except Exception as error:
            result = AgentResult(
                status=AgentResultStatus.FAILED,
                summary=f"Agent execution failed: {error}",
                known_issues=[type(error).__name__],
            )
        finally:
            observation_stop.set()
            await observation
        result = AgentResult.model_validate(self._safe_payload(result.model_dump(mode="json")))
        self.checks.record_result_checks(task_id, run_id, result.checks)
        if result.status == AgentResultStatus.NEEDS_INPUT:
            self.human_inputs.create(task_id, run_id, result.summary)
        self.runs.finish(run_id, result)
        status, phase = self._task_state_for_result(result)
        self.tasks.update(task_id, status=status, runtime_phase=phase)
        event_type = {
            AgentResultStatus.COMPLETED: "AgentCompleted",
            AgentResultStatus.CANCELLED: "AgentCancelled",
            AgentResultStatus.NEEDS_INPUT: "HumanInputRequested",
        }.get(result.status, "AgentFailed")
        self._emit(task_id, run_id, event_type, "agent", result.model_dump(mode="json"))

    async def _observe(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        run_id: str,
        stop: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.to_thread(
                    self.observer.capture,
                    task=task,
                    project=project,
                    run_id=run_id,
                    emit=lambda event_type, payload: self._emit(
                        task["id"], run_id, event_type, "workspace", payload
                    ),
                )
            except Exception as error:
                self._emit(
                    task["id"],
                    run_id,
                    "WorkspaceObservationFailed",
                    "workspace",
                    {"error": str(error)},
                )
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.4)
            except TimeoutError:
                pass

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "RUNNING":
            raise TaskStateError("Only RUNNING runs can be cancelled")
        self.runs.request_cancel(run_id)
        control = self.active_controls.get(run_id)
        if control:
            control.cancel()
        background = self.active_runs.get(run_id)
        if background:
            background.cancel()
        self._emit(run["task_id"], run_id, "CancelRequested", "runtime", {})
        return self.runs.get(run_id)  # type: ignore[return-value]

    async def resume(
        self, task_id: str, *, instruction: str | None = None
    ) -> dict[str, Any]:
        task = self._require(task_id)
        if task["status"] not in {
            TaskStatus.NEEDS_YOU,
            TaskStatus.FAILED,
            TaskStatus.REVIEW,
            TaskStatus.DONE,
        }:
            raise TaskStateError("Only NEEDS_YOU, FAILED, REVIEW, or DONE tasks can resume")
        previous = self.runs.list_for_task(task_id)
        previous_run_id = previous[0]["id"] if previous else None
        project = self.projects.get(task["project_id"])
        assert project is not None
        cleaned_instruction = self.credentials.redact((instruction or "").strip()) or (
            "Continue from the existing workspace. Inspect the previous result and current "
            "changes, then complete only unresolved work."
        )
        resume_context = self._project_context(
            task,
            project,
            previous[0] if previous else None,
            instruction=cleaned_instruction,
        )
        self.tasks.update(task_id, status=TaskStatus.READY.value, runtime_phase=None)
        self._emit(
            task_id,
            previous_run_id,
            "TaskResumed",
            "runtime",
            {"has_user_instruction": bool((instruction or "").strip())},
        )
        return await self.start(
            task_id,
            previous_run_id=previous_run_id,
            resume_context=resume_context,
        )

    def answer_input(self, task_id: str, answer: str) -> dict[str, Any]:
        self._require(task_id)
        answered = self.human_inputs.answer(task_id, self.credentials.redact(answer))
        if not answered:
            raise TaskStateError("Task has no pending human input")
        self._emit(task_id, answered["run_id"], "HumanInputAnswered", "runtime", {})
        return answered

    def _project_context(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        previous: dict[str, Any] | None,
        *,
        instruction: str,
    ) -> AgentResumeContext:
        workspace = Path(task["workspace_path"])
        latest = self.observer.capture(task=task, project=project, run_id=None)
        answers = self.human_inputs.list_for_task(task["id"])
        spec_json = TaskSpec.model_validate(task["spec"]).model_dump_json(indent=2)
        context = (
            "# Resume Context\n\n"
            f"## Task Spec\n\n```json\n{spec_json}\n```\n\n"
            f"## Environment\n\n{project['environment_spec'] or 'Use repository conventions.'}\n\n"
            f"## Previous Result\n\n{(previous or {}).get('summary') or 'No previous summary.'}\n\n"
            f"## User Continuation Instruction\n\n{instruction}\n\n"
            f"## Human Input\n\n{answers!r}\n\n"
            f"## Existing Workspace Diff\n\n```diff\n{latest['diff']}\n```\n"
        )
        target = workspace / ".task" / "context.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.credentials.redact(context), encoding="utf-8")
        return AgentResumeContext(
            instruction=instruction,
            previous_run_id=(previous or {}).get("id"),
            previous_status=(previous or {}).get("status"),
            previous_summary=(previous or {}).get("summary"),
        )

    def _forget(self, run_id: str) -> None:
        self.active_runs.pop(run_id, None)
        self.active_controls.pop(run_id, None)

    def _adapter(self) -> AgentAdapter:
        if self.adapter_override:
            return self.adapter_override
        provider = self.providers.get()
        if provider["provider"] == "demo":
            return DemoAgentAdapter()
        api_key = self.credentials.get_api_key()
        if not api_key:
            raise TaskStateError("The selected provider requires an API Key")
        if provider["provider"] == "deepseek" or is_deepseek_endpoint(provider["base_url"]):
            return DeepSeekAgentAdapter(
                model=provider["model"], api_key=api_key, base_url=provider["base_url"]
            )
        return BuiltinLangChainAgentAdapter(
            model=provider["model"], api_key=api_key, base_url=provider["base_url"]
        )

    def _record_check(
        self,
        task_id: str,
        run_id: str,
        name: str,
        command: list[str],
        cwd: str,
        runner: Any,
    ) -> str:
        check_id = self.checks.start(task_id, run_id, name, command, cwd)
        self.tasks.update(task_id, runtime_phase=RuntimePhase.CHECKING.value)
        try:
            output = self.credentials.redact(runner())
            self.checks.finish(check_id, output)
            return output
        except Exception as error:
            self.checks.finish(check_id, self.credentials.redact(str(error)), failed=True)
            raise
        finally:
            self.tasks.update(task_id, runtime_phase=RuntimePhase.IMPLEMENTING.value)

    def _emit(
        self,
        task_id: str,
        run_id: str | None,
        event_type: str,
        source: str,
        payload: dict[str, Any],
    ) -> None:
        self.events.record(
            Event(
                timestamp=now_iso(),
                task_id=task_id,
                run_id=run_id,
                type=event_type,
                source=source,
                payload=self._safe_payload(payload),
            )
        )

    def _safe_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        sensitive_names = {"api_key", "authorization", "password", "secret", "token"}

        def clean(value: Any, key: str = "") -> Any:
            if key.lower() in sensitive_names:
                return "[REDACTED]"
            if isinstance(value, str):
                return self.credentials.redact(value)[:4000]
            if isinstance(value, dict):
                return {
                    str(item_key): clean(item, str(item_key))
                    for item_key, item in value.items()
                }
            if isinstance(value, list):
                return [clean(item) for item in value[:100]]
            return value

        return clean(payload)

    @staticmethod
    def _task_state_for_result(result: AgentResult) -> tuple[str, str | None]:
        if result.status == AgentResultStatus.COMPLETED:
            return TaskStatus.REVIEW.value, None
        if result.status in {AgentResultStatus.BLOCKED, AgentResultStatus.NEEDS_INPUT}:
            return TaskStatus.NEEDS_YOU.value, RuntimePhase.WAITING.value
        if result.status == AgentResultStatus.CANCELLED:
            return TaskStatus.NEEDS_YOU.value, RuntimePhase.WAITING.value
        return TaskStatus.FAILED.value, None

    def _require(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        return task
