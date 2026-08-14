from __future__ import annotations

from typing import Final, Literal, TypedDict

type ActiveSuggestionModuleKind = Literal["ActiveSuggestion"]

ACTIVE_SUGGESTION_MODULE_KIND: Final[ActiveSuggestionModuleKind] = "ActiveSuggestion"


class SamplingExtraBody(TypedDict):
    top_p: float
    top_k: int
    min_p: float
    presence_penalty: float
    repetition_penalty: float


class SamplingPreset(TypedDict):
    temperature: float
    extra_body: SamplingExtraBody


SAMPLING_PRESETS: Final[dict[str, SamplingPreset]] = {
    "thinking_general": {
        "temperature": 1.0,
        "extra_body": {
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 1.5,
            "repetition_penalty": 1.0,
        },
    },
    "thinking_coding": {
        "temperature": 0.6,
        "extra_body": {
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "repetition_penalty": 1.0,
        },
    },
    "instruct_general": {
        "temperature": 0.7,
        "extra_body": {
            "top_p": 0.8,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 1.5,
            "repetition_penalty": 1.0,
        },
    },
    "instruct_reasoning": {
        "temperature": 1.0,
        "extra_body": {
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 1.5,
            "repetition_penalty": 1.0,
        },
    },
}
DEFAULT_PRESET_NAME: Final = "instruct_general"

# gpt-oss deployments take OpenAI's recommended harmony sampling: temperature
# and top_p only. None of the instruct extra-body knobs are ever sent to them.
GPT_OSS_MODEL_MARKER: Final = "gpt-oss"
GPT_OSS_TEMPERATURE: Final = 1.0
GPT_OSS_TOP_P: Final = 1.0

# Claude deployments take no sampling knobs at all: the API rejects the vLLM
# extra-body params, and the Claude 5 family deprecates `temperature` outright
# (smoke-tested 2026-07-29: sending it returns 400).
CLAUDE_MODEL_MARKER: Final = "claude"

MAX_TOKENS: Final = 16384
EXPLORE_MAX_TOKENS: Final = 2048
FIX_MAX_TOKENS: Final = 8192
CONTEXT_WINDOW: Final = 32000
SAFETY_MARGIN: Final = 4096
READ_INPUT_BUDGET: Final = CONTEXT_WINDOW - MAX_TOKENS - SAFETY_MARGIN
NUM_CHARS_PER_TOKEN: Final = 4
TOOL_TOKEN_LIMIT: Final = 2500
FULL_CODE_TOKEN_LIMIT: Final = 3500

DEFAULT_READ_OFFSET: Final = 0
DEFAULT_READ_LIMIT: Final = 60
READ_SRC_CODE_MAX_LIMIT: Final = 2
# Cap for the Phase 2 investigation loop only (resolve_identifier/read_code).
# Phase 1 and the verdict are single forced tool calls and are not governed by this.
MAX_EXPLORE_ITERATIONS: Final = 2

# Deterministic sink slice (graph/ast_slice.py) budgets. Consumers in graph/
# carry hardcoded fallbacks so obfuscated builds stay self-contained.
SLICE_TOKEN_LIMIT: Final = 500   # rendered sink-slice token cap (GV3: p95 within cap on real data)
SLICE_MAX_FACTS: Final = 25      # fixpoint identifier ceiling per slice

# Phase 2 adjudication (explore verdict over Phase 1 candidates) budgets.
VERDICT_MAX_CANDIDATES: Final = 3       # exit-C candidates admitted to the verdict call
VERDICT_PACK_TOKEN_LIMIT: Final = 1200  # deterministic candidate context pack budget
CANDIDATE_CONTEXT_LINES: Final = 10     # +/- lines excerpted around each candidate

# Lexical backtrace (graph/trace.py) budgets — parse-failure fallback only:
# consulted when the AST sink slice cannot be produced. Consumers in graph/
# carry hardcoded fallbacks so obfuscated builds stay self-contained.
TRACE_MAX_DEPTH: Final = 2                    # closure recursion depth cap (fallback evidence)
TRACE_MAX_SEED_IDENTIFIERS: Final = 5         # lexical sink-line seed ceiling (excess truncated)
TRACE_MAX_OCCURRENCES_PER_IDENT: Final = 8    # per-identifier occurrence cap
EVIDENCE_PACK_TOKEN_LIMIT: Final = 1500       # fallback evidence pack token budget

# Deterministic engine-context rendering (explore v4).
CONTEXT_EXCERPT_WINDOW: Final = 5
PARAM_MAX_CANDIDATES_PER_EVENT: Final = 5
PARAM_MAX_FACTS: Final = 8
PARAM_MAX_DECLARATIONS: Final = 2
PARAM_MAX_WRITES: Final = 3

BEST_OF_N: Final = 1
FIX_CANDIDATES_PER_BRANCH: Final = 1
FIX_SRC_CODE_TIMEOUT: Final = 90.0
MAX_FIX_ATTEMPTS: Final = 3
# Total model calls inside one fix-agent invocation. Four calls allow the
# third and final mutation submission to receive one non-mutation review turn.
MAX_FIX_TURNS: Final = 4
FIX_REJECTION_SNIPPET_MAX_LINES: Final = 40

MAX_DESC_STRING_LIMIT: Final = 300
MIN_DELETE_NON_WHITESPACE_CHARS: Final = 3
MAX_DELETE_RATIO: Final = 0.40
MAX_STRUCTURAL_DELETE_LINES: Final = 80
