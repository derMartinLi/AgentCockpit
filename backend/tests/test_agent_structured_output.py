from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage

import backend.app.services.agent as agent_module
from backend.app.core.telemetry import TraceScope
from backend.app.domain.models import AgentResultStatus, TaskSpec
from backend.app.services.agent import (
    AgentResultOutput,
    AgentResumeContext,
    BuiltinLangChainAgentAdapter,
    DeepSeekAgentAdapter,
    WorkspaceTools,
    is_deepseek_endpoint,
)


class FakeStructuredModel:
    def __init__(self, owner: FakeChatModel, method: str) -> None:
        self.owner = owner
        self.method = method

    async def ainvoke(
        self, messages: list[Any], *, config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.owner.final_messages = messages
        self.owner.final_configs.append(config)
        if self.method in self.owner.failing_methods:
            raise RuntimeError(f"{self.method} is unavailable")
        return {
            "raw": AIMessage(content=""),
            "parsed": AgentResultOutput(
                status=AgentResultStatus.COMPLETED,
                summary="Validated completion.",
                changes=["Kept the existing implementation"],
                checks=[],
                known_issues=[],
                risks=[],
                needs_human=False,
            ),
            "parsing_error": None,
        }


class FakeExecutionModel:
    def __init__(self, owner: FakeChatModel) -> None:
        self.owner = owner

    async def ainvoke(
        self, messages: list[Any], *, config: dict[str, Any] | None = None
    ) -> AIMessage:
        self.owner.execution_messages = messages
        self.owner.execution_configs.append(config)
        return AIMessage(content="Everything is complete and tests pass.")


class FakeChatModel:
    instances: list[FakeChatModel] = []
    failing_methods: set[str] = set()

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.methods: list[tuple[str, bool | None]] = []
        self.execution_messages: list[Any] = []
        self.final_messages: list[Any] = []
        self.execution_configs: list[dict[str, Any] | None] = []
        self.final_configs: list[dict[str, Any] | None] = []
        self.failing_methods = set(type(self).failing_methods)
        type(self).instances.append(self)

    def bind_tools(self, _: list[Any]) -> FakeExecutionModel:
        return FakeExecutionModel(self)

    def with_structured_output(
        self,
        schema: type[AgentResultOutput],
        *,
        method: str,
        include_raw: bool,
        strict: bool | None,
    ) -> FakeStructuredModel:
        assert schema is AgentResultOutput
        assert include_raw is True
        self.methods.append((method, strict))
        return FakeStructuredModel(self, method)


def make_tools(
    tmp_path: Any,
    events: list[tuple[str, dict[str, Any]]],
    *,
    trace_scope: TraceScope | None = None,
) -> WorkspaceTools:
    return WorkspaceTools(
        tmp_path,
        timeout_seconds=1,
        emit=lambda event_type, payload: events.append((event_type, payload)),
        trace_scope=trace_scope,
    )


def make_spec() -> TaskSpec:
    return TaskSpec(
        goal="Keep the completed implementation working",
        scope=["Existing workspace"],
        acceptance_criteria=["Tests pass"],
        constraints=["Preserve completed work"],
        decisions=[],
    )


@pytest.mark.asyncio
async def test_markdown_completion_is_finalized_with_strict_json_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    FakeChatModel.instances = []
    FakeChatModel.failing_methods = set()
    monkeypatch.setattr(agent_module, "ChatOpenAI", FakeChatModel)
    events: list[tuple[str, dict[str, Any]]] = []
    adapter = BuiltinLangChainAgentAdapter(model="gpt-test", api_key="secret")

    result = await adapter.execute(
        spec=make_spec(),
        environment_spec="Run project checks.",
        tools=make_tools(tmp_path, events),
        resume_context=AgentResumeContext(
            instruction="Fix only the failed test.",
            previous_run_id="run-1",
            previous_status="FAILED",
            previous_summary="Execution limit reached.",
        ),
    )

    model = FakeChatModel.instances[0]
    assert result.status == AgentResultStatus.COMPLETED
    assert model.methods == [("json_schema", True)]
    assert "This is a resumed run" in model.execution_messages[1].content
    assert "Fix only the failed test." in model.execution_messages[1].content
    assert "Everything is complete" in model.final_messages[-1].content
    assert len(model.final_messages) == 2


@pytest.mark.asyncio
async def test_openai_compatible_provider_uses_function_calling_finalizer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    FakeChatModel.instances = []
    FakeChatModel.failing_methods = set()
    monkeypatch.setattr(agent_module, "ChatOpenAI", FakeChatModel)
    adapter = BuiltinLangChainAgentAdapter(
        model="compatible-model",
        api_key="secret",
        base_url="https://provider.example/v1",
    )

    result = await adapter.execute(
        spec=make_spec(),
        environment_spec="",
        tools=make_tools(tmp_path, []),
    )

    assert result.status == AgentResultStatus.COMPLETED
    assert FakeChatModel.instances[0].methods == [("function_calling", None)]


@pytest.mark.asyncio
async def test_langfuse_callback_config_reaches_planning_and_finalization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    FakeChatModel.instances = []
    FakeChatModel.failing_methods = set()
    monkeypatch.setattr(agent_module, "ChatOpenAI", FakeChatModel)
    callback = object()
    adapter = BuiltinLangChainAgentAdapter(model="gpt-test", api_key="secret")

    result = await adapter.execute(
        spec=make_spec(),
        environment_spec="",
        tools=make_tools(tmp_path, [], trace_scope=TraceScope(callbacks=(callback,))),
    )

    model = FakeChatModel.instances[0]
    assert result.status == AgentResultStatus.COMPLETED
    assert model.execution_configs[0] == {
        "callbacks": [callback],
        "run_name": "agent-planning",
        "metadata": {"agent_step": 1, "phase": "planning"},
        "tags": ["agent-planning"],
    }
    assert model.final_configs[0] == {
        "callbacks": [callback],
        "run_name": "agent-result-finalization",
        "metadata": {"attempt": 1, "method": "json_schema"},
        "tags": ["agent-finalization"],
    }


@pytest.mark.asyncio
async def test_deepseek_provider_uses_non_thinking_json_mode_finalizer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    FakeChatModel.instances = []
    FakeChatModel.failing_methods = set()
    monkeypatch.setattr(agent_module, "ChatOpenAI", FakeChatModel)
    adapter = DeepSeekAgentAdapter(
        model="deepseek-v4-flash",
        api_key="secret",
        base_url="https://api.deepseek.com",
    )

    result = await adapter.execute(
        spec=make_spec(),
        environment_spec="",
        tools=make_tools(tmp_path, []),
    )

    model = FakeChatModel.instances[0]
    assert result.status == AgentResultStatus.COMPLETED
    assert model.init_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert model.methods == [("json_mode", None)]
    assert "JSON Schema" in model.final_messages[0].content
    assert len(model.final_messages) == 2


def test_deepseek_endpoint_detection_is_host_based() -> None:
    assert is_deepseek_endpoint("https://api.deepseek.com") is True
    assert is_deepseek_endpoint("https://api.deepseek.com/beta") is True
    assert is_deepseek_endpoint("https://deepseek.com.evil.example/v1") is False
    assert is_deepseek_endpoint("https://provider.example/v1") is False


@pytest.mark.asyncio
async def test_finalizer_retries_and_records_a_protocol_error_without_raw_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    FakeChatModel.instances = []
    FakeChatModel.failing_methods = {"json_schema", "function_calling"}
    monkeypatch.setattr(agent_module, "ChatOpenAI", FakeChatModel)
    events: list[tuple[str, dict[str, Any]]] = []
    adapter = BuiltinLangChainAgentAdapter(model="gpt-test", api_key="secret")

    result = await adapter.execute(
        spec=make_spec(),
        environment_spec="",
        tools=make_tools(tmp_path, events),
    )

    assert result.status == AgentResultStatus.FAILED
    assert result.protocol_error == "RESULT_FORMAT_INVALID"
    assert result.known_issues == []
    assert FakeChatModel.instances[0].methods == [
        ("json_schema", True),
        ("function_calling", None),
    ]
    assert any(event_type == "AgentResultProtocolError" for event_type, _ in events)
