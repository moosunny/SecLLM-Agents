"""Langfuse observability wiring.

Two layers live here, both fail-open (a broken or absent Langfuse must never
break a graph run):

* the LangChain ``CallbackHandler`` that turns LLM/tool calls into generation
  spans, and
* explicit OTel spans (:func:`observed_span`) plus trace-level attributes
  (:func:`update_trace`), which the graph opens around ``run_graph`` and the
  explore phases so the auto-instrumented generations nest under a named parent.

No scoring happens here — the runtime only records facts. Judgement lives in
``evals/`` and reads the recorded trajectory back out of Langfuse (online) or
``GraphState`` (offline).
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from langchain_core.callbacks import BaseCallbackHandler


def langfuse_handler() -> BaseCallbackHandler | None:
    _ensure_langfuse_host_env()
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:
        return None
    return CallbackHandler()


def langfuse_handlers() -> tuple[BaseCallbackHandler, ...]:
    handler = langfuse_handler()
    if handler is None:
        return ()
    return (handler,)


def shutdown_langfuse() -> None:
    _ensure_langfuse_host_env()
    try:
        from langfuse import get_client
    except ImportError:
        return
    get_client().shutdown()


# --------------------------------------------------------------------------- #
# Spans and trace attributes
# --------------------------------------------------------------------------- #
class SpanHandle:
    """Write side of one span; a no-op when Langfuse is unavailable."""

    __slots__ = ("_span",)

    def __init__(self, span: object | None = None) -> None:
        self._span = span

    def update(self, **attributes: object) -> None:
        """Attach output/metadata to the span, ignoring any SDK failure."""
        if self._span is None:
            return
        try:
            self._span.update(**attributes)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - observability must not raise
            return


@contextmanager
def observed_span(name: str) -> Iterator[SpanHandle]:
    """Open a named span; generations created inside it nest under it.

    Yields a no-op :class:`SpanHandle` when Langfuse is missing or disabled, so
    callers need no conditional wiring.
    """
    client = _client()
    if client is None:
        yield SpanHandle()
        return
    try:
        manager = client.start_as_current_observation(name=name, as_type="span")
    except Exception:  # pragma: no cover - observability must not raise
        yield SpanHandle()
        return
    with manager as span:
        yield SpanHandle(span)


@contextmanager
def trace_attributes(
    *,
    session_id: str | None = None,
    tags: list[str] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> Iterator[None]:
    """Apply trace-level attributes to every span opened inside this context.

    Trace attributes cannot be injected through the LangChain callback config in
    the OTel-based SDK — they belong to the active span and propagate to its
    children, so this must wrap the work, not follow it. Metadata values are
    stringified because the SDK types them as ``Dict[str, str]``.
    """
    propagate = _propagate_attributes()
    if propagate is None:
        yield
        return
    try:
        manager = propagate(
            session_id=session_id,
            tags=tags,
            metadata={key: str(value) for key, value in (metadata or {}).items()},
        )
    except Exception:  # pragma: no cover - observability must not raise
        yield
        return
    with manager:
        yield


def _propagate_attributes():
    if _client() is None:
        return None
    try:
        from langfuse import propagate_attributes
    except ImportError:  # pragma: no cover - older SDKs lack the helper
        return None
    return propagate_attributes


def _client() -> object | None:
    _ensure_langfuse_host_env()
    if os.getenv("LANGFUSE_TRACING_ENABLED", "").strip().lower() == "false":
        return None
    try:
        from langfuse import get_client
    except ImportError:
        return None
    try:
        return get_client()
    except Exception:  # pragma: no cover - misconfiguration degrades to no-op
        return None


def trace_metadata(mapping: Mapping[str, object]) -> dict[str, object]:
    """Drop empty values so Langfuse metadata stays free of null noise."""
    return {key: value for key, value in mapping.items() if value not in (None, "")}


def _ensure_langfuse_host_env() -> None:
    if os.getenv("LANGFUSE_HOST"):
        return
    base_url = os.getenv("LANGFUSE_BASE_URL")
    if base_url:
        os.environ["LANGFUSE_HOST"] = base_url
