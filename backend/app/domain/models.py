from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class TaskStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    NEEDS_YOU = "NEEDS_YOU"
    REVIEW = "REVIEW"
    DONE = "DONE"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"


class ArchivePhase(StrEnum):
    PREPARING = "PREPARING"
    SNAPSHOTTED = "SNAPSHOTTED"
    REMOVING = "REMOVING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RuntimePhase(StrEnum):
    REFINING = "REFINING"
    PREPARING = "PREPARING"
    IMPLEMENTING = "IMPLEMENTING"
    CHECKING = "CHECKING"
    WAITING = "WAITING"


class AgentResultStatus(StrEnum):
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    NEEDS_INPUT = "NEEDS_INPUT"
    FAILED = "FAILED"


class ModuleRule(BaseModel):
    name: str = Field(min_length=1)
    paths: list[str] = Field(min_length=1)


class VerificationCommand(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="test", pattern=r"^(test|lint|typecheck|build|custom)$")
    command: list[str] = Field(min_length=1, max_length=30)
    cwd: str = Field(default=".", min_length=1, max_length=500)
    auto_run: bool = True
    source: str = Field(default="user", pattern=r"^(detected|user|agent)$")


class TaskSpec(BaseModel):
    goal: str = Field(min_length=1)
    scope: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    interface: str | None = None


class RefinementQuestion(BaseModel):
    id: str
    question: str
    reason: str
    suggested_answer: str = ""
    blocking: bool = False


class AgentCheck(BaseModel):
    name: str
    status: str
    detail: str | None = None


class AgentResult(BaseModel):
    status: AgentResultStatus
    summary: str
    changes: list[str] = Field(default_factory=list)
    checks: list[AgentCheck] = Field(default_factory=list)
    known_issues: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    needs_human: bool = False
    protocol_error: str | None = None


class Event(BaseModel):
    timestamp: str
    task_id: str
    run_id: str | None
    type: str
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
