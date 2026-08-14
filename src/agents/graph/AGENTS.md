<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-06 | Updated: 2026-07-22 -->

# graph

## Purpose
This subpackage is the LangGraph pipeline core of active-suggestion. It assembles and runs a `StateGraph` that turns a validated code-issue request into an LLM-generated code-fix suggestion through deterministic `explore`, `fix` (one forced `edit_src_code` contiguous replacement), and response finalization. Fix validates and selects the N=1 candidate; there is no select graph node. It is also the PyArmor obfuscation boundary.

## Key Files
| File | Description |
| --- | --- |
| `state.py` | `GraphState` TypedDict and supporting types. `candidates` is a normal list because runtime fix generation is N=1. |
| `registry.py` | `create_chat_model`: `ChatOpenAI` factory used by fix. openai provider only (else `LLMRegistryError` E104), `instruct_general` sampling preset, `FIX_SRC_CODE_TIMEOUT`. Base-url priority: `--base-url` > `serviceHost`+`servicePort` > E104. API key: `APIKEY` else `LITELLM_MASTER_KEY` env else `"EMPTY"`. |
| `middleware.py` | Retained legacy explore middleware; disconnected from the v4 runtime. |
| `chunking.py` | Language resolution (`fileExtension` before `programmingLanguage`) to 7 astchunk languages (python/java/typescript/c/cpp/go/javascript), else `UnsupportedLanguageError` E103. `ASTChunkBuilder` wrapper (`metadata_template="default"`; astchunk `start_line_no`/`end_line_no` are 0-based tree-sitter rows so `+1`). Calls `chunkify(chunk_expansion=True, repo_level_metadata={})` and `_split_expansion_header` strips the injected `'''...'''` ancestor header into `SourceChunk.ancestors` without disturbing line coordinates. Utilities: `find_chunk_for_line`. `fallback_line_chunks` on parse failure (ancestors empty). |
| `ast_slice.py` | Deterministic tree-sitter source analysis. Explore v4 consumes `build_sink_slice().enclosing_chain` for the sink symbol and `resolve_identifier` for grounded param facts. Missing grammar returns `None` and param-fact enrichment fails open. |
| `engine_trace.py` | Explore v4 renderer: validates engine context lines, selects the earliest source event, renders locale messages, deduplicates excerpts against the sink chunk, extracts param identifiers without key whitelists, and emits only source-grounded positive facts. No LLM calls. |
| `trace.py` | Retained legacy lexical backtrace; disconnected from the v4 runtime pending cleanup. |
| `forced_tool.py` | Generic forced-tool middleware retained for the fix loop; pins the tool choice, disables parallel tool calls, and terminates on acceptance/attempt exhaustion. Legacy explore-specific termination support is not wired into v4. |
| `guards.py` | Validates one contiguous replace: range, no-op, net-removal limits, locale desc, prefix sanitation, and conversion to public delete/add modifications. |
| `explore.py` | Thin deterministic node: language/chunk/sink preprocessing followed by `contexts` rendering through `engine_trace.py`. `full_code_inline` is always false and `explore_trajectory` is left `None` for compatibility; explore performs zero LLM calls. |
| `fix.py` | `edit_src_code(startLine,endLine,newString,desc)` validates one edit and converts it to public delete/add. Rejections include the server-derived selected source snippet for the next corrective turn. The prompt always receives the unclipped sink chunk and fixed-slot exploration context. |
| `fix_loop.py` | Forces `edit_src_code`, returns validation errors plus source-range feedback, and retries up to `MAX_FIX_ATTEMPTS=3`. Exact `REVIEW_APPROVED` marks normal approval; at the bounded end, the latest schema- and guard-valid edit remains submittable even when review rejects or violates the review text protocol. Invalid tool protocol and guard failures remain blocking. `MAX_FIX_TURNS` bounds total model calls and is overridable through `run_graph`/CLI. |
| `select.py` | Compatibility-named response assembler; orders the already-selected public modifications and builds `AgentModuleResponse`. It does not validate or select. |
| `builder.py` | `START -> explore -> fix_branch -> END`; runtime BEST_OF_N fan-out and the select node are retired. |
| `__init__.py` | Empty package marker. |

## For AI Agents

### Working In This Directory
- This directory is the PyArmor obfuscation boundary. Keep all core logic (nodes, guards, chunking, fix-loop control) here. Do not move node behavior up into `builder.py` — `builder.py` is deliberately assembly-only.
- The fix stage is a bounded mutation/review loop. It pins `tool_choice` to `edit_src_code` until an edit is accepted, then requires exact `REVIEW_APPROVED`; a review revision invalidates the previous unreviewed candidate.
- Runtime fix generation is N=1. Do not reintroduce `Send` fan-out or consensus selection without revising the fine-tuning label contract.
- Explore is deterministic. Do not reintroduce inline/phased LLM routing, source-kind inference, checker/messageKey param routing, or E201 generation in the explore runtime.
- Param facts are grounding-first: inspect every string param, require an identifier to exist in the file, and emit only positive `resolve_identifier` results. Numeric params are never inferred to be line references.
- Guards are metadata-only by design: `validate_candidate` reads line ranges and desc against `code_lines` but never applies the patch, compiles, or re-parses. Do not add compile/re-parse steps here without accounting for the obfuscation and latency budget.
- astchunk line numbers are 0-based tree-sitter rows; `chunking._start_line`/`_end_line` add 1. Preserve that offset if you touch chunk metadata handling.
- Config constants are imported from `agents.config` with hardcoded fallbacks (`registry.py`, `middleware.py`). Keep fallback values in sync with `agents/config` when changing limits.

### Testing Requirements
- `run_graph` accepts an injectable `candidate_provider` (defaults to `LLMCandidateProvider`); pass a stub returning canned `FixCandidate` sequences to exercise the graph without hitting an LLM.
- `finalize_response` only serializes the candidate selected by fix and can be unit-tested directly; likewise `validate_candidate`, `estimate_tokens`, `resolve_language`, `build_source_chunks`, and `fallback_line_chunks`.
- Exercise contexts rendering with Java fixtures 0/15/38 and assert that model construction is never reached. `tests/test_engine_trace.py` covers sink-only, source rendering, range validation, excerpt/fact dedup, arbitrary param keys, positive-only grounding, and numeric-param non-routing.
- `tests/test_ast_slice.py` pins the slice contract per language (parameter/local/field/import/undeclared, shadowing, fixpoint termination, cross-scope writes, token cap, degenerate fallback).
- Cover replace guard edges explicitly: no-op, line ranges, net removal >40%/>80 lines, pure-delete minimum, prefix/line-ending normalization, locale-specific desc rules, and rejected-range source feedback.

### Common Patterns
- Error codes remain stage-scoped: E103 unsupported language, E104 registry/base-url/provider, E201 reserved for compatibility (no v4 explore runtime origin), E202 fix LLM, E301 no valid fix candidate. E301 is raised inside fix but retains wire `errorStage="select"` for caller compatibility.
- LLM models are created lazily inside providers via `from agents.graph.registry import create_chat_model` (local import) so the graph can be built and tested without model construction.
- Prompt context is assembled through `agents.prompt_loader` (`PromptRenderContext`, `render_prompt`, localized en/ko text via `choose_localized_text`).
- Exploration output now reaches the fix stage: `explore.py` serializes the structured sink→source finding into `explore_context`, `prompts/fix/user.md` has a `{explore_context}` placeholder, and `fix.py` injects it (with a budget-reduction order that protects the sink-anchor line last). The former gap is closed.
- The wired N=1 fix node records `fix_elapsed_time`. Mutation submissions and total model turns have separate caps so the third accepted submission can still receive a final review.
- `prepare_explore_state` still accepts legacy explore keyword arguments for caller compatibility, but ignores them. The A2 shadow gate and phase trajectory are retired from runtime; their evaluation redesign is deferred.

## Dependencies

### Internal
- `agents.config` — tunable constants (`BEST_OF_N=1`, `MAX_FIX_ATTEMPTS`, explore/token limits, timeouts, and sampling presets).
- `agents.error_handling` — `InputError`, `LLMConnectionError`, `GenerationError`.
- `agents.schema` — `AgentModuleRequest`, `AgentModuleResponse`, `CodeModification`, `Timings`.
- `agents.prompt_loader` — prompt rendering and localization helpers.
- `agents.tracing` — `langfuse_handlers`, `shutdown_langfuse`.

### External
- `langgraph` — `StateGraph`, `START`/`END`.
- `langchain` / `langchain_core` — `create_agent`, agent middleware (`ToolCallLimitMiddleware`, `ToolRetryMiddleware`, `AgentMiddleware`, `hook_config`), `tool`, message/runnable types.
- `langchain_openai` — `ChatOpenAI`.
- `openai` — `OpenAIError`.
- `pydantic` — `BaseModel` for the `edit_src_code` args schema.
- `astchunk` — AST-aware chunking (lazily imported).
- `tree_sitter` + per-language grammar wheels (`tree_sitter_python`/`_java`/`_typescript`/`_javascript`/`_go`/`_c`/`_cpp`) — sink slice parsing in `ast_slice.py` (lazily imported; already astchunk dependencies, and listed as PyInstaller hidden-imports in `build.py`).

<!-- MANUAL: notes below this line are preserved on regeneration -->
