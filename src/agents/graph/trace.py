"""Deterministic lexical backtrace engine (no LLM) — parse-failure fallback.

Since the v3 phase redesign the primary sink-dependency evidence comes from the
tree-sitter slice in ``graph/ast_slice.py``. This module is consulted only when
that slice cannot be produced (grammar failure or a degenerate slice): its
seed extraction, closure expansion, and evidence pack then feed the Phase 1
prompt in place of the slice rendering.

Lexical matching is deliberately imprecise (a same-named identifier in an
unrelated scope will match); the Phase 1 LLM judgement is the precision filter.
Every budget is an injected argument so the caps are exercised directly by tests.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from agents.graph.chunking import SourceChunk

NUM_CHARS_PER_TOKEN: Final = 4

# Reserved words and primitive types across the seven supported languages. Class
# and library type names (String, SubLObject, ...) are intentionally *not* here:
# they are tolerated as noise and filtered later by the LLM.
_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        # control flow / declarations shared broadly
        "assert", "if", "else", "elif", "for", "while", "do", "switch", "case",
        "default", "break", "continue", "return", "yield", "throw", "throws",
        "try", "catch", "except", "finally", "raise", "with", "as", "pass",
        "new", "delete", "class", "struct", "interface", "enum", "extends",
        "implements", "import", "from", "package", "namespace", "using",
        "public", "private", "protected", "internal", "static", "final",
        "abstract", "virtual", "override", "synchronized", "transient",
        "volatile", "native", "const", "constexpr", "mutable", "register",
        "inline", "extern", "typedef", "template", "typename", "operator",
        "def", "lambda", "function", "func", "var", "let", "val", "auto",
        "goto", "sizeof", "typeof", "instanceof", "in", "is", "not", "and",
        "or", "xor", "await", "async", "yield", "def", "global", "nonlocal",
        "select", "chan", "defer", "go", "map", "range", "type", "interface",
        # primitive types
        "void", "int", "long", "short", "byte", "char", "boolean", "bool",
        "float", "double", "unsigned", "signed", "string", "str", "object",
        "uint", "int8", "int16", "int32", "int64", "float32", "float64",
        "number", "any", "unknown", "never", "undefined",
        # literals
        "true", "false", "null", "nil", "none", "this", "self", "super",
    }
)

_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_STRING_LITERAL = re.compile(r"""(["'])(?:\\.|(?!\1).)*\1""")
_LINE_COMMENT = re.compile(r"(//|#).*$")

_ROLE_PRIORITY: Final[dict[str, int]] = {
    "parameter": 0,
    "declaration": 1,
    "assignment_lhs": 2,
    "usage": 3,
}

# Statement keywords that can appear as `<keyword> <name>(` at a *call* site
# (e.g. `return foo(x)`, `throw handler(x)`, `new Widget(x)`), which must not be
# mistaken for a method signature. Primitive-type keywords (void/int/...) are
# deliberately excluded so genuine typed signatures still match.
_CALL_PRECEDING_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "return", "throw", "yield", "await", "new", "delete", "else", "do",
        "assert", "case", "in", "and", "or", "not", "is", "with", "as",
        "raise", "goto", "typeof", "sizeof", "instanceof", "of",
    }
)


@dataclass(frozen=True, slots=True)
class Occurrence:
    """A lexical appearance of an identifier, classified by role."""

    name: str
    chunk_index: int
    line: int
    snippet: str
    role: str


def extract_sink_identifiers(code_lines: Sequence[str], issue_line: int) -> list[str]:
    """Lexically extract candidate identifiers from the sink *line*.

    Language keywords, primitive types, and string/comment contents are excluded.
    When the sink statement has unbalanced parentheses (i.e. it spans lines) the
    immediate neighbours are folded in so a wrapped call still yields its args.
    """
    if not 1 <= issue_line <= len(code_lines):
        return []

    window = [code_lines[issue_line - 1]]
    sink_clean = _strip_noise(code_lines[issue_line - 1])
    if sink_clean.count("(") != sink_clean.count(")"):
        if issue_line - 2 >= 0:
            window.insert(0, code_lines[issue_line - 2])
        if issue_line < len(code_lines):
            window.append(code_lines[issue_line])

    identifiers: list[str] = []
    seen: set[str] = set()
    for line in window:
        for token in _IDENTIFIER.findall(_strip_noise(line)):
            # lowercase before matching: uppercase literals (NIL, TRUE, ...)
            # must not slip past the stoplist
            if token.lower() in _KEYWORDS or token in seen:
                continue
            seen.add(token)
            identifiers.append(token)
    return identifiers


def find_identifier_occurrences(
    chunks: Sequence[SourceChunk],
    name: str,
) -> list[Occurrence]:
    """Find every whole-word appearance of ``name`` across all chunks."""
    if not name:
        return []
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    occurrences: list[Occurrence] = []
    for chunk_index, chunk in enumerate(chunks):
        for offset, raw_line in enumerate(chunk.content.splitlines()):
            if pattern.search(_strip_noise(raw_line)):
                occurrences.append(
                    Occurrence(
                        name=name,
                        chunk_index=chunk_index,
                        line=chunk.start_line + offset,
                        snippet=raw_line.strip(),
                        role=_classify_role(raw_line, name),
                    )
                )
    return occurrences


def expand_closure(
    seeds: Sequence[str],
    chunks: Sequence[SourceChunk],
    *,
    max_depth: int,
    max_occurrences_per_identifier: int,
    max_total: int,
    sink_line: int | None = None,
) -> list[Occurrence]:
    """Walk the definition closure of ``seeds`` up to ``max_depth`` hops.

    Each identifier's occurrences are located and prioritised (declaration /
    parameter / assignment above bare usage, then nearest the sink) before the
    per-identifier cap is applied — so for a name reused across many scopes the
    occurrence inside the sink's own method wins the cap. When an occurrence is
    an assignment or initialised declaration, the identifiers on its right-hand
    side become the next hop, giving intra-procedural dataflow without an LLM.
    """
    result: list[Occurrence] = []
    seen_occurrences: set[tuple[str, int]] = set()
    visited: set[str] = set()
    frontier = list(dict.fromkeys(seeds))
    depth = 0

    while frontier and depth <= max_depth and len(result) < max_total:
        next_frontier: list[str] = []
        for name in frontier:
            if name in visited:
                continue
            visited.add(name)
            occurrences = _prioritise(
                find_identifier_occurrences(chunks, name),
                sink_line=sink_line,
            )
            for occurrence in occurrences[:max_occurrences_per_identifier]:
                key = (occurrence.name, occurrence.line)
                if key in seen_occurrences:
                    continue
                seen_occurrences.add(key)
                result.append(occurrence)
                if len(result) >= max_total:
                    return result
                if depth < max_depth and occurrence.role in (
                    "assignment_lhs",
                    "declaration",
                ):
                    for rhs_name in _rhs_identifiers(occurrence.snippet, name):
                        if rhs_name not in visited:
                            next_frontier.append(rhs_name)
        frontier = list(dict.fromkeys(next_frontier))
        depth += 1
    return result


def build_evidence_pack(
    occurrences: Sequence[Occurrence],
    code_lines: Sequence[str],
    *,
    token_limit: int,
) -> str:
    """Render prioritised, line-numbered evidence within a token budget.

    Occurrences are considered highest-priority first so that, when the budget
    is exhausted, only the least informative (bare usage) entries are dropped; a
    trailing ``(truncated)`` marker records that a cut happened. The kept entries
    are printed in line order for readability.
    """
    ordered = sorted(
        occurrences,
        key=lambda occurrence: (
            _ROLE_PRIORITY.get(occurrence.role, 9),
            occurrence.line,
        ),
    )
    kept: list[tuple[int, str]] = []
    used = 0.0
    truncated = False
    for occurrence in ordered:
        entry = _format_occurrence(occurrence, code_lines)
        cost = len(entry) / NUM_CHARS_PER_TOKEN + 1
        if kept and used + cost > token_limit:
            truncated = True
            break
        kept.append((occurrence.line, entry))
        used += cost

    kept.sort(key=lambda item: item[0])
    lines = [entry for _, entry in kept]
    if truncated:
        lines.append("(truncated)")
    return "\n".join(lines)


def _prioritise(
    occurrences: Sequence[Occurrence],
    *,
    sink_line: int | None = None,
) -> list[Occurrence]:
    def sort_key(occurrence: Occurrence) -> tuple[int, int, int]:
        role_rank = _ROLE_PRIORITY.get(occurrence.role, 9)
        if sink_line is None:
            return (role_rank, occurrence.line, occurrence.line)
        return (role_rank, abs(occurrence.line - sink_line), occurrence.line)

    return sorted(occurrences, key=sort_key)


def _format_occurrence(occurrence: Occurrence, code_lines: Sequence[str]) -> str:
    if 1 <= occurrence.line <= len(code_lines):
        text = code_lines[occurrence.line - 1].strip()
    else:
        text = occurrence.snippet
    return f"L{occurrence.line} ({occurrence.role}) {text}"


def _classify_role(line: str, name: str) -> str:
    clean = _strip_noise(line)
    if name in _signature_param_names(clean):
        return "parameter"
    if re.search(rf"\b{re.escape(name)}\s*=(?!=)", clean):
        return "assignment_lhs"
    if re.search(
        rf"[A-Za-z_][\w.<>\[\]]*\s+{re.escape(name)}\b\s*[;=]",
        clean,
    ):
        return "declaration"
    return "usage"


def _signature_param_names(line: str) -> set[str]:
    open_index = line.find("(")
    if open_index == -1:
        return set()
    head = line[:open_index].rstrip()
    if not _looks_like_signature(head):
        return set()
    close_index = _matching_paren(line, open_index)
    inner = line[open_index + 1 : close_index] if close_index else line[open_index + 1 :]
    names: set[str] = set()
    for part in inner.split(","):
        tokens = _IDENTIFIER.findall(part)
        if tokens:
            names.add(tokens[-1])
    return names


def _looks_like_signature(head: str) -> bool:
    if re.search(r"\b(?:def|function|func)\s+[A-Za-z_]\w*$", head):
        return True
    # Typed method/function header: `... ReturnType methodName`. Reject when the
    # "return type" token is actually a statement keyword (`return foo(`,
    # `new Widget(`, ...) so call sites are not misread as signatures.
    match = re.search(r"([A-Za-z_][\w.<>\[\]]*)\s+[A-Za-z_]\w*$", head)
    if match is None:
        return False
    return match.group(1) not in _CALL_PRECEDING_KEYWORDS


def _matching_paren(line: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(line)):
        char = line[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _rhs_identifiers(line: str, lhs_name: str) -> list[str]:
    clean = _strip_noise(line)
    match = re.search(r"=(?!=)", clean)
    rhs = clean[match.end() :] if match else ""
    identifiers: list[str] = []
    for token in _IDENTIFIER.findall(rhs):
        if token.lower() in _KEYWORDS or token == lhs_name:
            continue
        identifiers.append(token)
    return identifiers


def _strip_noise(line: str) -> str:
    without_strings = _STRING_LITERAL.sub('""', line)
    return _LINE_COMMENT.sub("", without_strings)
