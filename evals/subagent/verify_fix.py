"""Deterministic verifier for one ActiveSuggestion fix candidate.

Usage with a prepared input directory:
    .venv/bin/python -B evals/subagent/verify_fix.py --dir <input_dir> \
        --candidate <input_dir>/candidate.json

Read-only usage with a JSONL record and candidate JSON on stdin:
    .venv/bin/python -B evals/subagent/verify_fix.py \
        --input-jsonl <input.jsonl> --idx <zero-based-index> \
        --candidate - --atomic-edits

``<input_dir>`` must contain the original source file (``code.java`` /
``code<ext>``).  The candidate JSON has the shape::

    {"desc": "...", "codeModifications": [...], "multi_site": false}

Checks, in order:
  1. public-contract shape via ``agents.graph.guards.validate_candidate``
  2. structural grouping into operations (delete[+adds] / standalone add),
     ascending order, non-overlap, multi-site policy (>=30-line gap between
     operations, ``multi_site`` flag consistency); ``--atomic-edits`` keeps
     structural checks but disables that legacy evaluation-only policy
  3. clean application to the original file (all coordinates original-based)
  4. no-op guard (per operation and whole-file) and net-deletion limits
     (<=40% of the file and <=80 lines)
  5. syntax integrity: the language-specific tree-sitter parse of the modified
     file must not introduce new ERROR/missing nodes compared to the original

Prints ``PASS`` or ``FAIL`` plus the affected regions of the *modified* file
rendered with new line numbers, so the caller can see what the edit actually
produced.  Exit code 0 on PASS, 1 on FAIL, 2 on usage errors.

``merge_parts.py`` imports :func:`check_candidate` to re-run the same checks
at merge time.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))

from agents.graph.guards import validate_candidate  # noqa: E402

MIN_MULTI_SITE_GAP = 30
MAX_NET_DELETE_LINES = 80
MAX_NET_DELETE_RATIO = 0.4
_RENDER_CONTEXT = 3

_EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".java": "java",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".go": "go",
    ".js": "javascript",
    ".jsx": "javascript",
}
_NAME_TO_LANGUAGE = {
    "python": "python",
    "java": "java",
    "typescript": "typescript",
    "ts": "typescript",
    "c": "c",
    "cpp": "cpp",
    "c++": "cpp",
    "go": "go",
    "golang": "go",
    "javascript": "javascript",
    "js": "javascript",
}
_GRAMMAR_MODULES = {
    "python": "tree_sitter_python",
    "java": "tree_sitter_java",
    "typescript": "tree_sitter_typescript",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
    "go": "tree_sitter_go",
    "javascript": "tree_sitter_javascript",
}
_LANGUAGE_CACHE: dict[tuple[str, bool], object] = {}


@dataclass(frozen=True, slots=True)
class SyntaxIssue:
    """One tree-sitter ERROR or parser-inserted missing node."""

    start_line: int
    end_line: int
    node_type: str


def resolve_language(programming_language: str, file_extension: str) -> str:
    """Resolve the verifier grammar using the production extension precedence."""
    extension = file_extension.lower().strip()
    if extension in _EXTENSION_TO_LANGUAGE:
        return _EXTENSION_TO_LANGUAGE[extension]
    language = _NAME_TO_LANGUAGE.get(programming_language.lower().strip())
    if language is not None:
        return language
    raise ValueError(
        "unsupported language hint "
        f"fileExtension={file_extension!r}, programmingLanguage={programming_language!r}"
    )


def _load_language(language: str, file_extension: str):
    from tree_sitter import Language

    tsx = language == "typescript" and file_extension.lower() == ".tsx"
    key = (language, tsx)
    grammar = _LANGUAGE_CACHE.get(key)
    if grammar is not None:
        return grammar

    module_name = _GRAMMAR_MODULES.get(language)
    if module_name is None:
        raise ValueError(f"unsupported syntax-validation language {language!r}")
    module = importlib.import_module(module_name)
    if language == "typescript":
        raw = module.language_tsx() if tsx else module.language_typescript()
    else:
        raw = module.language()
    grammar = Language(raw)
    _LANGUAGE_CACHE[key] = grammar
    return grammar


def _syntax_issues(text: str, language: str, file_extension: str) -> list[SyntaxIssue]:
    import tree_sitter

    grammar = _load_language(language, file_extension)
    try:
        parser = tree_sitter.Parser(grammar)
    except TypeError:  # older tree_sitter API
        parser = tree_sitter.Parser()
        parser.set_language(grammar)
    tree = parser.parse(text.encode("utf-8", errors="replace"))
    issues: list[SyntaxIssue] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            issues.append(
                SyntaxIssue(
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    node_type="missing" if node.is_missing else node.type,
                )
            )
        stack.extend(node.children)
    return issues


def _count_syntax_errors(
    text: str,
    language: str = "java",
    file_extension: str = ".java",
) -> int:
    """Compatibility wrapper retained for existing Java-only callers."""
    return len(_syntax_issues(text, language, file_extension))


def _group_ops(mods: list[dict]) -> tuple[list[dict], list[str]]:
    """Group the flat add/delete list into operations.

    An operation is either ``delete S..E`` optionally followed by add(s) at
    the same startLine (a replacement), or a standalone add (an insertion).
    """
    ops: list[dict] = []
    errors: list[str] = []
    i = 0
    while i < len(mods):
        m = mods[i]
        if m.get("type") == "delete":
            op = {
                "delete": (m["startLine"], m["endLine"]),
                "adds": [],
                "start": m["startLine"],
                "end": m["endLine"],
            }
            j = i + 1
            while (
                j < len(mods)
                and mods[j].get("type") == "add"
                and mods[j].get("startLine") == m["startLine"]
            ):
                op["adds"].append(mods[j].get("content") or "")
                j += 1
            ops.append(op)
            i = j
        else:  # standalone add = insertion before original line startLine
            op = {
                "delete": None,
                "adds": [m.get("content") or ""],
                "start": m["startLine"],
                "end": m["startLine"] - 1,  # occupies no original line
            }
            ops.append(op)
            i += 1

    for a, b in zip(ops, ops[1:]):
        if b["start"] <= a["end"] or b["start"] <= a["start"] and b["delete"] is None:
            errors.append(
                "modifications must be ordered ascending and non-overlapping: "
                f"operation at line {b['start']} follows operation covering "
                f"lines {a['start']}-{a['end']}. Emit `delete` first, then its "
                "`add`(s) at the same startLine."
            )
            break
    return ops, errors


def _apply(code_lines: list[str], ops: list[dict]) -> tuple[list[str], list[list[int]]]:
    """Apply operations; return (new_lines, per-op [start, end] new-file spans)."""
    deleted: set[int] = set()
    adds_at: dict[int, list[int]] = {}
    for op_idx, op in enumerate(ops):
        if op["delete"]:
            s, e = op["delete"]
            deleted.update(range(s, e + 1))
        if op["adds"]:
            adds_at.setdefault(op["start"], []).append(op_idx)

    new_lines: list[str] = []
    spans: list[list[int]] = [[0, 0] for _ in ops]
    for i in range(1, len(code_lines) + 2):
        for op_idx in adds_at.get(i, []):
            for content in ops[op_idx]["adds"]:
                for line in content.split("\n"):
                    new_lines.append(line)
                    if spans[op_idx][0] == 0:
                        spans[op_idx][0] = len(new_lines)
                    spans[op_idx][1] = len(new_lines)
        if i <= len(code_lines):
            if i in deleted:
                for op_idx, op in enumerate(ops):
                    if op["delete"] and op["delete"][0] == i and not op["adds"]:
                        seam = max(1, len(new_lines))
                        spans[op_idx] = [seam, seam + 1]
            else:
                new_lines.append(code_lines[i - 1])
    return new_lines, spans


def _issue_intersects(issue: SyntaxIssue, start_line: int, end_line: int) -> bool:
    return issue.start_line <= end_line and issue.end_line >= start_line


def check_candidate(
    code_lines: list[str],
    candidate: dict,
    *,
    language: str = "java",
    file_extension: str = ".java",
    enforce_multi_site_policy: bool = True,
) -> dict:
    """Apply and verify one candidate without materializing the modified source.

    ``enforce_multi_site_policy`` keeps the legacy evaluation rule as the
    default for existing callers.  The Active Suggestion case agent disables
    it because its public contract permits nearby, separated atomic edits.
    """
    desc = candidate.get("desc") or ""
    mods = candidate.get("codeModifications") or []
    errors = list(validate_candidate(mods, desc, code_lines))

    result = {
        "errors": errors,
        "new_lines": None,
        "spans": [],
        "ops_count": 0,
        "multi_site": False,
        "language": language,
        "syntax_before": None,
        "syntax_after": None,
    }
    if errors:
        return result

    ops, structural = _group_ops(mods)
    errors.extend(structural)
    result["ops_count"] = len(ops)
    result["multi_site"] = len(ops) > 1

    if enforce_multi_site_policy:
        flag = bool(candidate.get("multi_site", False))
        if len(ops) > 1 and not flag:
            errors.append(
                'multiple operations require "multi_site": true in the candidate '
                "(or merge them into one contiguous replacement)."
            )
        if len(ops) <= 1 and flag:
            errors.append('"multi_site": true is only allowed with 2+ operations.')
        if len(ops) > 1:
            for a, b in zip(ops, ops[1:]):
                gap = b["start"] - a["end"] - 1
                if gap < MIN_MULTI_SITE_GAP:
                    errors.append(
                        f"operations at lines {a['start']}-{a['end']} and "
                        f"{b['start']}-{b['end']} are only {gap} lines apart "
                        f"(<{MIN_MULTI_SITE_GAP}): merge them into a single "
                        "contiguous replacement."
                    )
    if errors:
        return result

    new_lines, spans = _apply(code_lines, ops)
    result["new_lines"] = new_lines
    result["spans"] = spans

    for n, op in enumerate(ops, start=1):
        if op["delete"] and op["adds"]:
            s, e = op["delete"]
            original = code_lines[s - 1 : e]
            replacement = "\n".join(op["adds"]).split("\n")
            if original == replacement:
                errors.append(
                    f"operation {n} is a no-op: the replacement is identical "
                    f"to original lines {s}-{e}."
                )
    if new_lines == code_lines:
        errors.append("the modified file is identical to the original (no-op).")

    net_deleted = len(code_lines) - len(new_lines)
    if net_deleted > MAX_NET_DELETE_LINES:
        errors.append(
            f"net deletion of {net_deleted} lines exceeds the "
            f"{MAX_NET_DELETE_LINES}-line limit."
        )
    if net_deleted > MAX_NET_DELETE_RATIO * len(code_lines):
        errors.append(
            f"net deletion of {net_deleted} lines exceeds "
            f"{int(MAX_NET_DELETE_RATIO * 100)}% of the {len(code_lines)}-line file."
        )

    if not errors:
        try:
            before_issues = _syntax_issues(
                "\n".join(code_lines), language, file_extension
            )
            after_issues = _syntax_issues(
                "\n".join(new_lines), language, file_extension
            )
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(
                "syntax validation unavailable for "
                f"{language}: {type(exc).__name__}."
            )
        else:
            result["syntax_before"] = len(before_issues)
            result["syntax_after"] = len(after_issues)
            if len(after_issues) > len(before_issues):
                errors.append(
                    f"the edit introduces syntax errors: {len(before_issues)} parse "
                    f"error(s) in the original vs {len(after_issues)} in the modified file."
                )
            else:
                for operation_index, (op, span) in enumerate(
                    zip(ops, spans), start=1
                ):
                    original_start = op["start"]
                    original_end = max(op["start"], op["end"])
                    modified_start, modified_end = span
                    if modified_start == 0:
                        continue
                    before_near_edit = any(
                        _issue_intersects(issue, original_start, original_end)
                        for issue in before_issues
                    )
                    after_near_edit = any(
                        _issue_intersects(issue, modified_start, modified_end)
                        for issue in after_issues
                    )
                    if after_near_edit and not before_near_edit:
                        errors.append(
                            "the edit introduces syntax errors near operation "
                            f"{operation_index}, even though the total parser-error "
                            "count did not increase."
                        )
                        break
    return result


def render_regions(new_lines: list[str], spans: list[list[int]]) -> str:
    out: list[str] = []
    for n, (s, e) in enumerate(spans, start=1):
        if s == 0:
            continue
        lo = max(1, s - _RENDER_CONTEXT)
        hi = min(len(new_lines), e + _RENDER_CONTEXT)
        out.append(f"--- operation {n}: modified file lines {lo}-{hi} (new coordinates) ---")
        out.extend(f"{i:5d}. {new_lines[i - 1]}" for i in range(lo, hi + 1))
    return "\n".join(out)


def _find_code_file(input_dir: Path) -> Path | None:
    matches = sorted(input_dir.glob("code.*"))
    return matches[0] if matches else None


def _read_candidate(candidate_arg: str) -> dict:
    try:
        raw = sys.stdin.read() if candidate_arg == "-" else Path(candidate_arg).read_text(
            encoding="utf-8"
        )
        candidate = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read candidate JSON: {exc}") from exc
    if not isinstance(candidate, dict):
        raise ValueError("candidate JSON must be an object")
    return candidate


def _read_jsonl_source(path: Path, idx: int) -> tuple[list[str], str, str]:
    if idx < 0:
        raise ValueError("--idx must be zero or greater")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read input JSONL: {exc}") from exc

    selected: str | None = None
    non_empty_idx = 0
    for line in lines:
        if not line.strip():
            continue
        if non_empty_idx == idx:
            selected = line
            break
        non_empty_idx += 1
    if selected is None:
        raise ValueError(f"non-empty JSONL record {idx} does not exist")

    try:
        record = json.loads(selected)
        user = record["userRequest"]
        code = user["entireCode"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"selected JSONL record is invalid: {exc}") from exc
    if not isinstance(code, str):
        raise ValueError("selected userRequest.entireCode must be a string")

    extension = user.get("fileExtension") or ""
    programming_language = user.get("programmingLanguage") or ""
    if not isinstance(extension, str) or not isinstance(programming_language, str):
        raise ValueError("language hints must be strings")
    language = resolve_language(programming_language, extension)
    return code.split("\n"), language, extension


def _read_directory_source(input_dir: Path) -> tuple[list[str], str, str]:
    code_file = _find_code_file(input_dir)
    if code_file is None:
        raise ValueError(f"no code.* file in {input_dir}")
    try:
        code = code_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read source file: {exc}") from exc
    extension = code_file.suffix
    language = resolve_language("", extension)
    return code.split("\n"), language, extension


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dir", type=Path, help="prepared input directory")
    source.add_argument(
        "--input-jsonl",
        type=Path,
        help="source JSONL; requires --idx and reads entireCode directly",
    )
    parser.add_argument(
        "--idx",
        type=int,
        help="zero-based index among non-empty JSONL records",
    )
    parser.add_argument(
        "--candidate",
        required=True,
        help="candidate JSON path, or - to read it from stdin",
    )
    parser.add_argument(
        "--atomic-edits",
        action="store_true",
        help="allow separated atomic edits without the legacy 30-line multi-site rule",
    )
    args = parser.parse_args()

    try:
        if args.input_jsonl is not None:
            if args.idx is None:
                raise ValueError("--idx is required with --input-jsonl")
            code_lines, language, extension = _read_jsonl_source(
                args.input_jsonl, args.idx
            )
        else:
            if args.idx is not None:
                raise ValueError("--idx is only valid with --input-jsonl")
            code_lines, language, extension = _read_directory_source(args.dir)
        candidate = _read_candidate(args.candidate)
    except ValueError as exc:
        print(f"usage error: {exc}")
        return 2

    result = check_candidate(
        code_lines,
        candidate,
        language=language,
        file_extension=extension,
        enforce_multi_site_policy=not args.atomic_edits,
    )

    if result["errors"]:
        print("FAIL")
        for err in result["errors"]:
            print(f"- {err}")
    else:
        print("PASS")
        if result["multi_site"]:
            print(f"(multi_site: {result['ops_count']} operations)")
    if result["syntax_before"] is not None:
        print(
            f"syntax: {result['language']}, original={result['syntax_before']}, "
            f"modified={result['syntax_after']}"
        )
    if result["new_lines"] is not None:
        print(render_regions(result["new_lines"], result["spans"]))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
