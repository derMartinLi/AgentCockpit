from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig

from backend.app.core.config import Settings

logger = logging.getLogger(__name__)

ObservationType = Literal[
    "span",
    "agent",
    "tool",
    "chain",
    "retriever",
    "evaluator",
    "guardrail",
    "generation",
    "embedding",
]

_TOKEN_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|pk)-lf-[a-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bsk-[a-z0-9_-]{12,}\b"),
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)[^\s,\"']+"),
)


def build_trace_redactor(
    *,
    redact: Callable[[str], str],
    secret_values: list[str],
) -> Callable[[Any], Any]:
    known_secrets = tuple(value for value in secret_values if value)

    def redact_string(value: str) -> str:
        masked = redact(value)
        for secret in known_secrets:
            masked = masked.replace(secret, "[REDACTED]")
        for pattern in _TOKEN_PATTERNS:
            masked = pattern.sub(
                lambda match: (
                    f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]"
                ),
                masked,
            )
        return masked

    def redact_value(value: Any) -> Any:
        if isinstance(value, str):
            return redact_string(value)
        if isinstance(value, dict):
            return {str(key): redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(redact_value(item) for item in value)
        return value

    return redact_value


@dataclass(slots=True)
class TraceScope:
    observation: Any | None = None
    callbacks: tuple[Any, ...] = ()
    trace_id: str | None = None

    def update(
        self,
        *,
        output: Any | None = None,
        metadata: dict[str, Any] | None = None,
        level: Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"] | None = None,
        status_message: str | None = None,
    ) -> None:
        if self.observation is None:
            return
        self.observation.update(
            output=output,
            metadata=metadata,
            level=level,
            status_message=status_message,
        )

    def langchain_config(
        self,
        *,
        run_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> RunnableConfig | None:
        if not self.callbacks:
            return None
        config: RunnableConfig = {"callbacks": list(self.callbacks)}
        if run_name:
            config["run_name"] = run_name
        if metadata:
            config["metadata"] = metadata
        if tags:
            config["tags"] = tags
        return config


class LangfuseTelemetry:
    """Application-owned Langfuse client with safe no-op degradation."""

    def __init__(self, settings: Settings, *, redact: Callable[[str], str]) -> None:
        self.settings = settings
        self.client: Any | None = None
        self._public_key: str | None = None
        self._redact_value: Callable[[Any], Any] = build_trace_redactor(
            redact=redact,
            secret_values=[],
        )

        public_key = (
            settings.langfuse_public_key.get_secret_value()
            if settings.langfuse_public_key
            else None
        )
        secret_key = (
            settings.langfuse_secret_key.get_secret_value()
            if settings.langfuse_secret_key
            else None
        )
        if not settings.langfuse_enabled or not public_key or not secret_key:
            return

        self._public_key = public_key
        self._redact_value = build_trace_redactor(
            redact=redact,
            secret_values=[public_key, secret_key],
        )
        try:
            # Import only after Settings has loaded backend/.env. Constructor arguments are
            # explicit so tracing never depends on process-global environment mutation.
            from langfuse import Langfuse

            self.client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                base_url=settings.langfuse_base_url,
                tracing_enabled=True,
                environment=settings.langfuse_environment,
                release="agent-cockpit@1.0.0",
                mask=self._mask_sdk_value,
                mask_otel_spans=self._mask_otel_spans,
            )
        except Exception as error:  # observability must not prevent application startup
            self.client = None
            logger.warning("Langfuse tracing disabled: %s", type(error).__name__)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @contextmanager
    def trace(
        self,
        *,
        name: str,
        as_type: ObservationType,
        input: Any,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        tags: list[str] | None = None,
        trace_seed: str | None = None,
    ) -> Iterator[TraceScope]:
        if self.client is None:
            yield TraceScope()
            return

        from langfuse import propagate_attributes
        from langfuse.langchain import CallbackHandler

        class CompactCallbackHandler(CallbackHandler):
            @property
            def ignore_chain(self) -> bool:
                # Structured-output parsing emits several internal Runnable chain events.
                # Generations and tools remain visible without this implementation noise.
                return True

        trace_id = self.client.create_trace_id(seed=trace_seed) if trace_seed else None
        trace_context = {"trace_id": trace_id} if trace_id else None
        with self.client.start_as_current_observation(
            name=name,
            as_type=as_type,
            input=self._redact_value(input),
            metadata=self._redact_value(metadata),
            trace_context=trace_context,
        ) as observation:
            with propagate_attributes(
                trace_name=name,
                session_id=session_id,
                tags=tags,
                environment=self.settings.langfuse_environment,
            ):
                handler = CompactCallbackHandler(public_key=self._public_key)
                yield TraceScope(
                    observation=observation,
                    callbacks=(handler,),
                    trace_id=trace_id,
                )

    def flush(self) -> None:
        if self.client is not None:
            self.client.flush()

    def shutdown(self) -> None:
        if self.client is not None:
            self.client.shutdown()

    def redact_value(self, value: Any) -> Any:
        return self._redact_value(value)

    def _mask_sdk_value(self, *, data: Any, **_kwargs: Any) -> Any:
        return self._redact_value(data)

    def _mask_otel_spans(self, *, params: Any) -> Any:
        from langfuse.types import (
            MaskOtelSpansResult,
            OtelSpanIdentifier,
            OtelSpanPatch,
        )

        patches: dict[OtelSpanIdentifier, OtelSpanPatch | None] = {}
        for identifier, span in params.spans.items():
            replacements: dict[str, Any] = {}
            for key, value in span.attributes.items():
                masked = self._redact_value(value)
                if masked != value:
                    replacements[key] = masked
            if replacements:
                patches[identifier] = OtelSpanPatch(set_attributes=replacements)
        return MaskOtelSpansResult(span_patches=patches)
