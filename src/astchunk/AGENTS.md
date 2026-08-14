<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-06 | Updated: 2026-07-06 -->

# astchunk (vendored)

## Purpose
Vendored, DO-NOT-MODIFY third-party library: a modified fork of [astchunk](https://github.com/yilinjz/astchunk), a tree-sitter based AST code chunker that splits source into structure-aware chunks bounded by non-whitespace character count. It is vendored (rather than pulled from PyPI) specifically because this fork adds **c, cpp, go, and javascript** tree-sitter grammars on top of upstream's python/java/csharp/typescript set (see `ASTChunkBuilder.__init__` and `ANCESTOR_NODE_TYPES` in `astchunk.py`). Wired into the app via `[tool.uv.sources] astchunk = { path = "src/astchunk" }` in the root `pyproject.toml`, and consumed by `src/agents/graph/chunking.py`.

## Key Files
| File | Description |
| --- | --- |
| README.md | Upstream project documentation (usage, benchmarks, API). |
| LICENSE | MIT license from upstream authors (CMU / Augment). |
| pyproject.toml | setuptools package metadata; declares the tree-sitter grammar deps including the added c/cpp/go/javascript. |
| requirements.txt | Runtime dependency pins mirroring pyproject. |
| src/astchunk/ | The importable `astchunk` package (see below); public API re-exported from `__init__.py`. |
| examples/ | Upstream usage examples. |
| tests/ | Upstream test suite. |

The `src/astchunk/` package: `astchunk_builder.py` (`ASTChunkBuilder` — 4-step `chunkify` pipeline: assign-to-windows -> optional overlap -> build chunks -> emit code windows; selects tree-sitter parser by `language`), `astchunk.py` (`ASTChunk` — rebuilds chunk text, ancestor paths, metadata), `astnode.py` (`ASTNode` wrapper), `preprocessing.py` (byte-range / non-whitespace-count helpers).

## For AI Agents
### Working In This Directory
- This is vendored dependency code, NOT project code. **Do not edit anything under `src/astchunk/`.** Any change here silently diverges from upstream and makes future re-syncs painful.
- If you find a bug or need different behavior, do NOT patch it here. Document the issue and prefer wrapping/adapting in `src/agents/graph/chunking.py` (the only in-app consumer).
- Do not create AGENTS.md files inside `src/astchunk/`, `examples/`, or `tests/`. This single file covers the whole vendored tree.
- The only intentional divergence from upstream is the extra grammar support (c/cpp/go/javascript). If upstream language support ever needs extending, that is the rare case where editing here is justified — and it must be mirrored in both `ASTChunkBuilder.__init__` and `ANCESTOR_NODE_TYPES`.

### Testing Requirements
- Do not run this package's own suite (`tests/`, configured for `testpaths = ["test"]`) as part of app work; verify behavior through the app's tests that exercise `src/agents/graph/chunking.py` instead.

### Common Patterns
- Construct with `ASTChunkBuilder(max_chunk_size=..., language=..., metadata_template=...)`, then call `.chunkify(code, chunk_overlap=..., repo_level_metadata=..., chunk_expansion=...)` -> `list[dict]` of `{"content", "metadata"}` code windows.
- `metadata_template="default"` yields `start_line_no` / `end_line_no` (0-based tree-sitter rows).
- Supported `language` values: `python`, `java`, `csharp`, `typescript` (parsed as TSX), `c`, `cpp`, `go`, `javascript`; any other raises `ValueError`.

## Dependencies
### Internal
- Consumed by `src/agents/graph/chunking.py`; not otherwise coupled to app code.

### External
- `tree-sitter` plus grammar packages: `tree-sitter-python`, `-java`, `-c-sharp`, `-typescript`, `-c`, `-cpp`, `-go`, `-javascript`.
- `numpy` (cumulative non-whitespace counts), `pyrsistent` (immutable ancestor vectors).

<!-- MANUAL: notes below this line are preserved on regeneration -->
