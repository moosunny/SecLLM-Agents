<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-06 | Updated: 2026-07-06 -->

# agents

## Purpose
`agents` is the main application package for active-suggestion: a standalone CLI that reads a single JSON code-issue request, runs it through a LangGraph pipeline (deterministic explore -> single forced-tool fix -> finalize), and emits an LLM-generated code-fix suggestion as JSON. This top-level package owns the process entry point, the CLI orchestration and its single exception->JSON boundary, the external Pydantic contract, the typed error hierarchy, all tunable constants, optional Langfuse tracing, and prompt loading/rendering. The actual pipeline lives in `graph/` and the prompt templates in `prompts/` (each documented separately).

## Key Files
| File | Description |
| --- | --- |
| `__init__.py` | Public surface: re-exports the schema models (`AgentModuleRequest`, `AgentUserRequest`, `ContextEvent`, `AgentModuleResponse`, `AgentModuleErrorResponse`, `CodeModification`, `Timings`). |
| `__main__.py` | Non-obfuscated build/run entry point; imports and calls `app.main()` via `raise SystemExit(main())`. `build.py` assumes this exists. |
| `app.py` | CLI: `argparse` (`-i/--input`, `-o/--output`, `--base-url`, `--max-fix-turns`), `.env` loading, lazy `run_graph` import + orchestration, top-level `except Exception` -> `error_response_from_exception` boundary, stdout print plus optional `-o` file write, exit code 0/1. |
| `config.py` | Single source of truth for tunables: sampling, token/context budgets, deterministic engine-context rendering caps, N=1 fix configuration, retry count, and mutation/desc guards. |
| `schema.py` | Pydantic external contract (`ContractModel` with `extra="ignore"`, `frozen=True`, alias-based, `populate_by_name`/`serialize_by_alias`), including optional `userRequest.contexts`; models plus `parse_request_json_bytes` with utf-8-sig -> utf-8 -> cp949 decode fallback. |
| `error_handling.py` | Exception hierarchy (`InputError` E1xx, `LLMConnectionError` E2xx, `GenerationError` E3xx, `InternalError` E9xx) carrying `error_code`/`error_stage`, plus `error_response_from_exception` — the ONLY place exceptions become `AgentModuleErrorResponse`. |
| `tracing.py` | Conditional Langfuse import; returns `None`/empty handler tuple when `langfuse` is absent (so `--release` builds are a no-op). Maps `LANGFUSE_BASE_URL` -> `LANGFUSE_HOST` if unset. |
| `prompt_loader.py` | Loads/renders markdown prompts via `str.format_map`; resolves prompt root via `sys._MEIPASS` when frozen else the source dir; `LocalizedText` en->ko fallback; `desc_language_instruction(locale)`. |

## Subdirectories
| Directory | Purpose |
| --- | --- |
| `graph/` | LangGraph pipeline core (state, nodes, builder, tools). PyArmor obfuscation boundary. |
| `prompts/` | Markdown prompt templates, organized `<stage>/<role>.md` (e.g. `explore/system.md`, `fix/user.md`). |

## For AI Agents

### Working In This Directory
- Exceptions become JSON in exactly ONE place: `error_handling.error_response_from_exception`, called only from `app.py`. Graph nodes must raise an `ActiveSuggestionError` subclass (or set `state['error']`); never build an `AgentModuleErrorResponse` yourself and never `print`/serialize errors outside `app.py`. Unknown exceptions collapse to `E999`/`errorStage="output"` with only the exception type name as message.
- Every constant is a `Final` in `config.py`. Add or change tunables there, never inline magic numbers elsewhere.
- `schema.py` is the external contract with front-end/caller. Field aliases (`entireCode`, `issueLineNumber`, `contexts`, `lineNo`, `eventType`, `userRequest`, `codeModifications`, ...) are the wire format; renaming an alias breaks callers. Models are `frozen` (immutable) and `extra="ignore"` (unknown input keys silently dropped). Responses serialize by alias. Note the property shims (e.g. `AgentUserRequest.entireCode`) that expose camelCase read access over snake_case fields.
- New error conditions: pick the right subclass so `error_stage` is correct (`InputError`->input, `LLMConnectionError`->explore/fix by code, `GenerationError`->wire-compatible select, `InternalError`->output). E301 is raised inside fix but retains `errorStage="select"` for caller compatibility. Add any new code to the `ErrorCode` `Literal` union first, or type-checking fails.
- `app.py` imports `run_graph` lazily (inside `_run_graph`) so the heavy `graph/` package (and its obfuscated modules) load only when needed; keep it lazy.
- `.env` loading is environment-dependent: `Path.cwd()/.env` in dev, `Path(sys.executable).parent/.env` when frozen (`sys.frozen`). Don't assume project-root `.env`.

### Testing Requirements
- No test framework config lives in this directory; drive the CLI end-to-end: `python -m agents -i <request.json> [-o out.json] [--base-url URL]`. It must always emit a single JSON line to stdout and exit 0 (success) or 1 (any error), even on failure.
- To exercise the error boundary without a live LLM, feed malformed input: non-decodable bytes -> `E101`, invalid JSON or schema-validation failure -> `E102`.
- Verify round-tripping via alias: build requests with camelCase keys (`entireCode`, `issueLineNumber`) and confirm responses serialize with `isSuccess`/`codeModifications` aliases.

### Common Patterns
- `from __future__ import annotations` at the top of every module; PEP 695 `type` aliases and `assert_never` for exhaustive `match` over `Literal` unions.
- Frozen dataclasses with `slots=True` and `MappingProxyType` for immutable module-level tables (`prompt_loader.py`, `config.py`).
- Prompt rendering uses `string.Formatter` to detect required `{field}` placeholders and raises `PromptRenderError` for any missing field before `format_map`.

## Dependencies

### Internal
- `agents.schema` — imported by `__init__`, `app`, `error_handling`.
- `agents.config` — `ACTIVE_SUGGESTION_MODULE_KIND` / `ActiveSuggestionModuleKind` used by `schema`; constants consumed across `graph/`.
- `agents.error_handling` — used by `app` (boundary) and `schema` (raises `InputError` in `parse_request_json_bytes`).
- `agents.graph.builder.run_graph` — lazily imported by `app`.
- `agents.prompts/*` — loaded by `prompt_loader`.

### External
- `pydantic` (`BaseModel`, `ConfigDict`, `Field`, `ValidationError`) — schema/contract.
- `python-dotenv` (`load_dotenv`) — `.env` loading in `app`.
- `langchain_core.callbacks.BaseCallbackHandler` — tracing handler type.
- `langfuse` (optional; `langfuse.langchain.CallbackHandler`, `langfuse.get_client`) — tracing, absent-safe.

<!-- MANUAL: notes below this line are preserved on regeneration -->
