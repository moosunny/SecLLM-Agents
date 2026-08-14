from collections.abc import Sequence
from typing import NotRequired, Protocol, TypedDict


class ContextEventLike(Protocol):
    line_no: int
    event_type: str
    message_key: str
    message: str
    params: dict[str, str | int]


class UserRequestLike(Protocol):
    locale: str
    programmingLanguage: str
    fileExtension: str | None
    issueLineNumber: int
    entireCode: str
    contexts: Sequence[ContextEventLike]


class ModuleRequestLike(Protocol):
    model: str
    provider: str
    serviceHost: str
    servicePort: str
    userRequest: UserRequestLike


class CodeModification(TypedDict):
    type: str
    startLine: int
    endLine: NotRequired[int]
    content: NotRequired[str]


class FixCandidate(TypedDict):
    modifications: list[CodeModification]
    desc: str
    branch_id: NotRequired[int]
    validation_errors: NotRequired[list[str]]


class GraphTimings(TypedDict):
    explore_elapsed_time: float
    fix_elapsed_time: float
    select_elapsed_time: float
    graph_elapsed_time: float


class GraphError(TypedDict):
    error_code: str
    error_stage: str
    message: str


class CodeChunk(TypedDict):
    index: int
    start_line: int
    end_line: int
    content: str
    # Enclosing class/function headers for this chunk (outermost first). Empty on
    # the fallback line-splitting path, which carries no AST ancestry.
    ancestors: NotRequired[tuple[str, ...]]


class SinkAnchor(TypedDict):
    """Deterministic sink anchoring for issueLineNumber (LLM-free)."""

    line: int
    code: str
    enclosing_symbol: str
    chunk_index: NotRequired[int]
    ancestors: NotRequired[tuple[str, ...]]


class SourceFinding(TypedDict):
    """Engine-provided source coordinates or a deterministic fallback status."""

    line: NotRequired[int]
    code: NotRequired[str]
    enclosing_symbol: NotRequired[str]
    # Only fallback statuses use kind: not_applicable | unresolved. Engine
    # sources deliberately carry line/code without an inferred kind.
    kind: NotRequired[str]
    trace: NotRequired[list[dict[str, object]]]
    notes: NotRequired[str]


class GraphState(TypedDict):
    request: ModuleRequestLike
    branch_id: NotRequired[int]
    language: str
    code_lines: list[str]
    chunks: list[CodeChunk]
    issue_line_number: int
    explore_context: str
    full_code_inline: bool
    sink_anchor: NotRequired[dict]
    source_finding: NotRequired[dict | None]
    # Schema-v2 record of how explore reached its finding (phases, tool calls,
    # grounding snapshots). Emitted to the Langfuse span too; kept here so the
    # offline eval runner can score in-process without a Langfuse round-trip.
    explore_trajectory: NotRequired[dict | None]
    candidates: list[FixCandidate]
    selected: FixCandidate | None
    timings: GraphTimings
    error: GraphError | None
