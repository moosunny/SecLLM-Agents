"""Deterministically render engine-provided contexts for the fix stage."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from agents.graph.ast_slice import IdentifierFact, SourceRef, resolve_identifier
from agents.graph.chunking import SourceChunk
from agents.schema import ContextEvent

try:
    from agents.config import (
        CONTEXT_EXCERPT_WINDOW,
        PARAM_MAX_CANDIDATES_PER_EVENT,
        PARAM_MAX_DECLARATIONS,
        PARAM_MAX_FACTS,
        PARAM_MAX_WRITES,
    )
except ImportError:  # pragma: no cover - obfuscated builds keep fallbacks in sync
    CONTEXT_EXCERPT_WINDOW: Final = 5
    PARAM_MAX_CANDIDATES_PER_EVENT: Final = 5
    PARAM_MAX_FACTS: Final = 8
    PARAM_MAX_DECLARATIONS: Final = 2
    PARAM_MAX_WRITES: Final = 3


_IDENTIFIER_RE: Final = re.compile(r"(?<![\w$])([A-Za-z_$][A-Za-z0-9_$]*)(?![\w$])")
_JAVA_KEYWORDS: Final = frozenset(
    {
        "abstract",
        "assert",
        "boolean",
        "break",
        "byte",
        "case",
        "catch",
        "char",
        "class",
        "const",
        "continue",
        "default",
        "do",
        "double",
        "else",
        "enum",
        "extends",
        "false",
        "final",
        "finally",
        "float",
        "for",
        "goto",
        "if",
        "implements",
        "import",
        "instanceof",
        "int",
        "interface",
        "long",
        "native",
        "new",
        "null",
        "package",
        "private",
        "protected",
        "public",
        "return",
        "short",
        "static",
        "strictfp",
        "super",
        "switch",
        "synchronized",
        "this",
        "throw",
        "throws",
        "transient",
        "true",
        "try",
        "void",
        "volatile",
        "while",
    }
)


@dataclass(frozen=True, slots=True)
class EngineTraceResult:
    context: str
    source_finding: dict[str, object]
    metrics: dict[str, object]


def render_engine_context(
    *,
    events: Sequence[ContextEvent],
    code: str,
    code_lines: Sequence[str],
    language: str,
    file_extension: str,
    sink_anchor: dict[str, object],
    source_chunks: Sequence[SourceChunk],
) -> EngineTraceResult:
    """Build the fixed-slot explore context without invoking an LLM."""
    valid_events = [event for event in events if _valid_line(event.line_no, code_lines)]
    invalid_count = len(events) - len(valid_events)
    normalized_types = [_normalized_event_type(event.event_type) for event in valid_events]
    source_events = [
        event
        for event, event_type in zip(valid_events, normalized_types, strict=True)
        if event_type == "source"
    ]
    sink_only = bool(valid_events) and all(
        event_type == "sink" for event_type in normalized_types
    )

    source_finding = _source_finding(source_events, code_lines, sink_only=sink_only)
    sink_range = _sink_chunk_range(sink_anchor, source_chunks)
    excerpt, rendered_excerpt_ranges = _render_excerpt(
        valid_events,
        code_lines,
        sink_range=sink_range,
    )
    param_facts, param_candidates = _render_param_facts(
        valid_events,
        code=code,
        language=language,
        file_extension=file_extension,
        sink_range=sink_range,
        excerpt_ranges=rendered_excerpt_ranges,
    )

    lines = [
        f"Sink (line {sink_anchor.get('line')}): "
        f"{sink_anchor.get('code') or '(none)'}",
        f"Enclosing symbol: {sink_anchor.get('enclosing_symbol') or '(none)'}",
        _source_line(source_finding),
    ]
    if valid_events and not sink_only:
        lines.append("Engine trace:")
        lines.extend(
            f"L{event.line_no} [{event_type}] {event.message}"
            for event, event_type in zip(valid_events, normalized_types, strict=True)
        )
    lines.extend(["Source excerpt:", excerpt, "Param facts:"])
    lines.extend(param_facts or ["(none)"])
    lines.append(
        "Notes: "
        + _notes(normalized_types, invalid_count=invalid_count)
    )

    sink_line = sink_anchor.get("line")
    sink_event_lines = [
        event.line_no
        for event, event_type in zip(valid_events, normalized_types, strict=True)
        if event_type == "sink"
    ]
    metrics: dict[str, object] = {
        "total_context_events": len(events),
        "valid_context_events": len(valid_events),
        "invalid_context_events": invalid_count,
        "source_events": normalized_types.count("source"),
        "branch_events": normalized_types.count("branch"),
        "sink_events": normalized_types.count("sink"),
        "sink_line_mismatch": bool(
            sink_event_lines and any(line != sink_line for line in sink_event_lines)
        ),
        "param_candidates": param_candidates,
        "param_facts": len(param_facts),
    }
    return EngineTraceResult(
        context="\n".join(lines),
        source_finding=source_finding,
        metrics=metrics,
    )


def extract_param_identifiers(value: str) -> list[str]:
    """Extract ordered Java identifier candidates without relying on param keys."""
    seen: set[str] = set()
    candidates: list[str] = []
    for match in _IDENTIFIER_RE.finditer(value):
        name = match.group(1)
        if name in _JAVA_KEYWORDS or name in seen:
            continue
        seen.add(name)
        candidates.append(name)
    return candidates


def _source_finding(
    source_events: Sequence[ContextEvent],
    code_lines: Sequence[str],
    *,
    sink_only: bool,
) -> dict[str, object]:
    if source_events:
        source_event = min(source_events, key=lambda event: event.line_no)
        return {
            "line": source_event.line_no,
            "code": code_lines[source_event.line_no - 1],
        }
    if sink_only:
        return {"kind": "not_applicable"}
    return {"kind": "unresolved"}


def _source_line(finding: dict[str, object]) -> str:
    kind = finding.get("kind")
    if kind == "not_applicable":
        return "Source: not applicable — the defect is self-contained at the sink."
    if kind == "unresolved":
        return "Source: unresolved — trust the sink anchor only."
    return f"Source (line {finding['line']}): {finding['code']}"


def _render_excerpt(
    events: Sequence[ContextEvent],
    code_lines: Sequence[str],
    *,
    sink_range: tuple[int, int] | None,
) -> tuple[str, tuple[tuple[int, int], ...]]:
    windows = _merge_ranges(
        (
            max(1, event.line_no - CONTEXT_EXCERPT_WINDOW),
            min(len(code_lines), event.line_no + CONTEXT_EXCERPT_WINDOW),
        )
        for event in events
    )
    if not windows:
        return "(none)", ()

    rendered_ranges: list[tuple[int, int]] = []
    parts: list[str] = []
    event_lines = sorted({event.line_no for event in events})
    for start, end in windows:
        if sink_range is not None and sink_range[0] <= start and end <= sink_range[1]:
            coordinates = ", ".join(
                f"L{line}" for line in event_lines if start <= line <= end
            )
            parts.append(f"(included in code context, {coordinates})")
            continue
        rendered_ranges.append((start, end))
        parts.append(
            "\n".join(
                f"{line}. {code_lines[line - 1]}"
                for line in range(start, end + 1)
            )
        )
    return "\n".join(parts), tuple(rendered_ranges)


def _render_param_facts(
    events: Sequence[ContextEvent],
    *,
    code: str,
    language: str,
    file_extension: str,
    sink_range: tuple[int, int] | None,
    excerpt_ranges: Sequence[tuple[int, int]],
) -> tuple[list[str], int]:
    rendered: list[str] = []
    seen_lines: set[str] = set()
    candidate_count = 0
    for event in events:
        event_candidates: list[str] = []
        event_seen: set[str] = set()
        for value in event.params.values():
            if not isinstance(value, str):
                continue
            for name in extract_param_identifiers(value):
                if name in event_seen or not _identifier_in_code(name, code):
                    continue
                event_seen.add(name)
                event_candidates.append(name)
                if len(event_candidates) >= PARAM_MAX_CANDIDATES_PER_EVENT:
                    break
            if len(event_candidates) >= PARAM_MAX_CANDIDATES_PER_EVENT:
                break

        candidate_count += len(event_candidates)
        for name in event_candidates:
            fact = resolve_identifier(
                code,
                language,
                name,
                near_line=event.line_no,
                file_extension=file_extension,
            )
            if fact is None or not fact.resolved:
                continue
            line = _render_fact(
                fact,
                sink_range=sink_range,
                excerpt_ranges=excerpt_ranges,
            )
            if line in seen_lines:
                continue
            seen_lines.add(line)
            rendered.append(line)
            if len(rendered) >= PARAM_MAX_FACTS:
                return rendered, candidate_count
    return rendered, candidate_count


def _render_fact(
    fact: IdentifierFact,
    *,
    sink_range: tuple[int, int] | None,
    excerpt_ranges: Sequence[tuple[int, int]],
) -> str:
    decl_lines = {ref.line for ref in fact.decls}
    refs: list[tuple[str, SourceRef]] = [
        (f"declared L{ref.line} ({ref.kind})", ref)
        for ref in fact.decls[:PARAM_MAX_DECLARATIONS]
    ]
    write_refs = [ref for ref in fact.writes if ref.line not in decl_lines]
    write_refs.extend(fact.cross_writes)
    refs.extend(
        (
            f"written{' (other scope)' if ref.kind == 'cross_write' else ''} L{ref.line}",
            ref,
        )
        for ref in write_refs[:PARAM_MAX_WRITES]
    )
    segments = [
        _render_ref(label, ref, sink_range=sink_range, excerpt_ranges=excerpt_ranges)
        for label, ref in refs
    ]
    return f"{fact.name}: " + "; ".join(segments)


def _render_ref(
    label: str,
    ref: SourceRef,
    *,
    sink_range: tuple[int, int] | None,
    excerpt_ranges: Sequence[tuple[int, int]],
) -> str:
    if sink_range is not None and sink_range[0] <= ref.line <= sink_range[1]:
        return f"{label} — in code context"
    if any(start <= ref.line <= end for start, end in excerpt_ranges):
        return f"{label} — in source excerpt"
    return f"{label}: {ref.text}"


def _sink_chunk_range(
    sink_anchor: dict[str, object],
    source_chunks: Sequence[SourceChunk],
) -> tuple[int, int] | None:
    index = sink_anchor.get("chunk_index")
    if not isinstance(index, int) or not (0 <= index < len(source_chunks)):
        return None
    chunk = source_chunks[index]
    return chunk.start_line, chunk.end_line


def _normalized_event_type(value: str) -> str:
    normalized = value.strip().lower()
    return normalized if normalized in {"source", "branch", "sink"} else "branch"


def _valid_line(line: int, code_lines: Sequence[str]) -> bool:
    return 1 <= line <= len(code_lines)


def _identifier_in_code(name: str, code: str) -> bool:
    return re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", code) is not None


def _merge_ranges(ranges) -> tuple[tuple[int, int], ...]:
    ordered = sorted(ranges)
    if not ordered:
        return ()
    merged: list[list[int]] = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        current = merged[-1]
        if start <= current[1] + 1:
            current[1] = max(current[1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def _notes(event_types: Sequence[str], *, invalid_count: int) -> str:
    text = (
        f"{event_types.count('source')} source, "
        f"{event_types.count('branch')} branch, "
        f"{event_types.count('sink')} sink events"
    )
    if invalid_count:
        text += f"; {invalid_count} out-of-range events excluded"
    return text
