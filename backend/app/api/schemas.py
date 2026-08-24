from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from backend.app.domain.models import ModuleRule, TaskSpec, VerificationCommand


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    repository_path: str = Field(min_length=1)
    environment_spec: str = ""
    module_mapping: list[ModuleRule] = Field(default_factory=list)
    verification_commands: list[VerificationCommand] | None = None


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    environment_spec: str | None = None
    module_mapping: list[ModuleRule] | None = None
    verification_commands: list[VerificationCommand] | None = None


class ModulePreviewRequest(BaseModel):
    file_path: str = Field(min_length=1)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    raw_request: str = Field(min_length=3)


class RefinementAnswer(BaseModel):
    question_id: str
    answer: str = Field(min_length=1)


class RefinementAnswers(BaseModel):
    answers: list[RefinementAnswer] = Field(min_length=1, max_length=5)


class TaskSpecUpdate(BaseModel):
    spec: TaskSpec


class ProviderUpdate(BaseModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    base_url: str | None = None
    api_key: str | None = None

    @model_validator(mode="after")
    def validate_provider(self) -> ProviderUpdate:
        if self.provider != "demo" and not self.api_key:
            # An existing key may still be present; the service decides whether this is acceptable.
            return self
        return self


class HumanInputAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=10_000)


class ResumeRequest(BaseModel):
    instruction: str | None = Field(default=None, max_length=10_000)


class ReviewDecisionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=10_000)


class ArchiveRequest(BaseModel):
    actor: str = Field(default="local-user", min_length=1, max_length=120)


class ArchivedTaskRestartRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)


def dump_rules(rules: list[ModuleRule] | None) -> list[dict[str, Any]] | None:
    return [rule.model_dump() for rule in rules] if rules is not None else None
