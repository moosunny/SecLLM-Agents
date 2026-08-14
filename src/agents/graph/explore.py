"""Deterministic explore preprocessing backed by engine-provided contexts."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence

from langchain_core.callbacks import BaseCallbackHandler

from agents.graph.ast_slice import SinkSlice, build_sink_slice
from agents.graph.chunking import (
    CodeLanguageHint,
    SourceChunk,
    build_source_chunks,
    find_chunk_for_line,
    resolve_language,
)
from agents.graph.engine_trace import render_engine_context
from agents.graph.state import CodeChunk, GraphState
from agents.tracing import observed_span


def prepare_explore_state(
    state: GraphState,
    *,
    base_url_override: str | None = None,
    callbacks: Sequence[BaseCallbackHandler] = (),
    source_necessity_shadow: bool = False,
) -> GraphState:
    """Populate sink anchoring and fix context without invoking an LLM.

    The optional arguments remain accepted for compatibility with existing
    callers. Explore v4 deliberately ignores them because engine ``contexts``
    replace inline/phased source investigation and its shadow classifier.
    """
    del base_url_override, callbacks, source_necessity_shadow
    start = time.perf_counter()
    request = state["request"]
    user_request = request.user_request
    hint = CodeLanguageHint(
        programming_language=user_request.programming_language,
        file_extension=user_request.file_extension,
    )

    language = resolve_language(hint)
    code_lines = user_request.entire_code.splitlines()
    source_chunks = build_source_chunks(user_request.entire_code, hint)
    issue_line = user_request.issue_line_number
    sink_slice = build_sink_slice(
        user_request.entire_code,
        language,
        issue_line,
        file_extension=user_request.file_extension or "",
    )
    sink_anchor = build_sink_anchor(source_chunks, code_lines, issue_line, sink_slice)

    with observed_span("explore") as span:
        result = render_engine_context(
            events=user_request.contexts,
            code=user_request.entire_code,
            code_lines=code_lines,
            language=language,
            file_extension=user_request.file_extension or "",
            sink_anchor=sink_anchor,
            source_chunks=source_chunks,
        )
        span.update(
            output={
                "sink_anchor": sink_anchor,
                "source_finding": result.source_finding,
                "full_code_inline": False,
            },
            metadata={"engine_trace": result.metrics},
        )

    state.update(
        {
            "language": language,
            "code_lines": code_lines,
            "chunks": _code_chunks(source_chunks),
            "issue_line_number": issue_line,
            "sink_anchor": sink_anchor,
            "source_finding": result.source_finding,
            "explore_context": result.context,
            "full_code_inline": False,
            "explore_trajectory": None,
        }
    )
    state["timings"]["explore_elapsed_time"] = time.perf_counter() - start
    return state


def build_sink_anchor(
    source_chunks: Sequence[SourceChunk],
    code_lines: Sequence[str],
    issue_line: int,
    sink_slice: SinkSlice | None = None,
) -> dict[str, object]:
    """Anchor the reported issue line to its code, symbol, and source chunk."""
    sink_chunk = find_chunk_for_line(source_chunks, issue_line)
    sink_code = ""
    if 1 <= issue_line <= len(code_lines):
        sink_code = code_lines[issue_line - 1].strip()
    ancestors = sink_chunk.ancestors if sink_chunk else ()
    enclosing = ""
    if sink_slice is not None and sink_slice.enclosing_chain:
        enclosing = _normalize_enclosing_symbol(sink_slice.enclosing_chain[0].text)
    elif ancestors:
        enclosing = _normalize_enclosing_symbol(ancestors[-1])
    anchor: dict[str, object] = {
        "line": issue_line,
        "code": sink_code,
        "enclosing_symbol": enclosing,
        "ancestors": list(ancestors),
    }
    if sink_chunk is not None:
        anchor["chunk_index"] = list(source_chunks).index(sink_chunk)
    return anchor


def serialize_sink_anchor(anchor: dict[str, object]) -> str:
    """Render the deterministic two-line sink header."""
    lines = [f"Sink (line {anchor.get('line')}): {anchor.get('code', '')}".rstrip()]
    enclosing = anchor.get("enclosing_symbol")
    if enclosing:
        lines.append(f"Enclosing symbol: {enclosing}")
    return "\n".join(lines)


def _normalize_enclosing_symbol(value: str) -> str:
    """Remove a trailing Java body opener/comment from a one-line header."""
    return re.sub(r"\s*\{\s*(?://.*)?$", "", value.strip()).rstrip()


def _code_chunks(source_chunks: Sequence[SourceChunk]) -> list[CodeChunk]:
    return [
        CodeChunk(
            index=index,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            content=chunk.content,
            ancestors=chunk.ancestors,
        )
        for index, chunk in enumerate(source_chunks)
    ]
