from __future__ import annotations

from pathlib import Path

from backend.app.core.config import Settings
from backend.app.core.telemetry import LangfuseTelemetry, TraceScope, build_trace_redactor


def test_settings_accept_standard_langfuse_environment_names(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LANGFUSE_PUBLIC_KEY=pk-lf-public-test\n"
        "LANGFUSE_SECRET_KEY=sk-lf-secret-test\n"
        "LANGFUSE_BASE_URL=https://us.cloud.langfuse.com\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file, langfuse_enabled=False)  # type: ignore[call-arg]

    assert settings.langfuse_public_key is not None
    assert settings.langfuse_secret_key is not None
    assert settings.langfuse_public_key.get_secret_value() == "pk-lf-public-test"
    assert settings.langfuse_secret_key.get_secret_value() == "sk-lf-secret-test"
    assert settings.langfuse_base_url == "https://us.cloud.langfuse.com"


def test_trace_redactor_masks_known_credentials_and_common_tokens() -> None:
    redactor = build_trace_redactor(
        redact=lambda value: value.replace("provider-key", "[REDACTED]"),
        secret_values=["langfuse-secret"],
    )

    masked = redactor(
        {
            "provider": "provider-key",
            "langfuse": "langfuse-secret",
            "headers": "Authorization: Bearer sk-example-token-123456",
            "nested": ["pk-lf-public-token-123456"],
        }
    )

    assert masked == {
        "provider": "[REDACTED]",
        "langfuse": "[REDACTED]",
        "headers": "Authorization: Bearer [REDACTED]",
        "nested": ["[REDACTED]"],
    }


def test_disabled_telemetry_is_a_safe_noop(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=tmp_path,
        langfuse_enabled=False,
    )
    telemetry = LangfuseTelemetry(settings, redact=lambda value: value)

    with telemetry.trace(
        name="agent-run",
        as_type="agent",
        input={"goal": "test"},
        session_id="task-1",
        tags=["agent-cockpit"],
    ) as trace:
        assert trace == TraceScope()
        trace.update(output={"status": "COMPLETED"})

    telemetry.flush()
    telemetry.shutdown()
    assert telemetry.enabled is False
