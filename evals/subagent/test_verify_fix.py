"""Tests for the read-only Active Suggestion candidate verifier."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SUBAGENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SUBAGENT_DIR))

from verify_fix import check_candidate  # noqa: E402


def _replacement(line: int, content: str) -> list[dict[str, int | str]]:
    return [
        {"type": "delete", "startLine": line, "endLine": line},
        {"type": "add", "startLine": line, "content": content},
    ]


LANGUAGE_CASES = [
    (
        "python",
        ".py",
        ["def value():", "    return 1"],
        2,
        "    return 2",
        "    return (",
    ),
    (
        "java",
        ".java",
        ["class Demo {", "    int value() { return 1; }", "}"],
        2,
        "    int value() { return 2; }",
        "    int value() { return ( ; }",
    ),
    (
        "typescript",
        ".ts",
        ["function value(): number {", "    return 1;", "}"],
        2,
        "    return 2;",
        "    return ( ;",
    ),
    (
        "typescript",
        ".tsx",
        ["const View = () => <div>ok</div>;"],
        1,
        "const View = () => <span>ok</span>;",
        "const View = () => <div>;",
    ),
    (
        "c",
        ".c",
        ["int value(void) {", "    return 1;", "}"],
        2,
        "    return 2;",
        "    return ( ;",
    ),
    (
        "cpp",
        ".cpp",
        ["int value() {", "    return 1;", "}"],
        2,
        "    return 2;",
        "    return ( ;",
    ),
    (
        "go",
        ".go",
        ["package main", "func value() int {", "    return 1", "}"],
        3,
        "    return 2",
        "    return (",
    ),
    (
        "javascript",
        ".js",
        ["function value() {", "    return 1;", "}"],
        2,
        "    return 2;",
        "    return ( ;",
    ),
    (
        "javascript",
        ".jsx",
        ["const View = () => <div>ok</div>;"],
        1,
        "const View = () => <span>ok</span>;",
        "const View = () => <div>;",
    ),
]


@pytest.mark.parametrize(
    ("language", "extension", "code_lines", "line", "valid", "broken"),
    LANGUAGE_CASES,
)
def test_candidate_passes_tree_sitter_when_modified_source_is_valid(
    language: str,
    extension: str,
    code_lines: list[str],
    line: int,
    valid: str,
    broken: str,
) -> None:
    # Given: a syntactically valid candidate for one supported language.
    candidate = {
        "desc": "Replace the vulnerable expression safely.",
        "codeModifications": _replacement(line, valid),
    }

    # When: the candidate is applied in memory and parsed again.
    result = check_candidate(
        code_lines,
        candidate,
        language=language,
        file_extension=extension,
        enforce_multi_site_policy=False,
    )

    # Then: deterministic syntax validation accepts the candidate.
    assert result["errors"] == []


@pytest.mark.parametrize(
    ("language", "extension", "code_lines", "line", "valid", "broken"),
    LANGUAGE_CASES,
)
def test_candidate_rejects_new_tree_sitter_error_for_supported_language(
    language: str,
    extension: str,
    code_lines: list[str],
    line: int,
    valid: str,
    broken: str,
) -> None:
    # Given: a candidate that introduces a syntax error.
    candidate = {
        "desc": "Replace the vulnerable expression safely.",
        "codeModifications": _replacement(line, broken),
    }

    # When: the modified source is parsed with its language grammar.
    result = check_candidate(
        code_lines,
        candidate,
        language=language,
        file_extension=extension,
        enforce_multi_site_policy=False,
    )

    # Then: the verifier reports an actual syntax regression.
    assert any("introduces syntax errors" in error for error in result["errors"])


def test_candidate_allows_nearby_atomic_edits_for_agent_contract() -> None:
    # Given: two necessary but nearby replacements in original coordinates.
    code_lines = [
        "class Demo {",
        "    int first() { return 1; }",
        "    int second() { return 2; }",
        "}",
    ]
    candidate = {
        "desc": "Update both unsafe operations.",
        "codeModifications": (
            _replacement(2, "    int first() { return 10; }")
            + _replacement(3, "    int second() { return 20; }")
        ),
    }

    # When: the Active Suggestion atomic-edit contract is selected.
    result = check_candidate(
        code_lines,
        candidate,
        language="java",
        file_extension=".java",
        enforce_multi_site_policy=False,
    )

    # Then: the legacy 30-line evaluation policy does not reject valid edits.
    assert result["errors"] == []
    assert result["ops_count"] == 2


def test_cli_reads_selected_jsonl_record_and_candidate_from_stdin(tmp_path: Path) -> None:
    # Given: a JSONL source with a leading blank line and an in-memory candidate.
    source = {
        "userRequest": {
            "programmingLanguage": "Python",
            "fileExtension": ".py",
            "entireCode": "def value():\n    return 1",
        }
    }
    jsonl = tmp_path / "input.jsonl"
    jsonl.write_text("\n" + json.dumps(source) + "\n", encoding="utf-8")
    candidate = {
        "desc": "Replace the vulnerable expression safely.",
        "codeModifications": _replacement(2, "    return 2"),
    }

    # When: the CLI receives candidate JSON through stdin without creating a file.
    completed = subprocess.run(
        [
            sys.executable,
            str(SUBAGENT_DIR / "verify_fix.py"),
            "--input-jsonl",
            str(jsonl),
            "--idx",
            "0",
            "--candidate",
            "-",
            "--atomic-edits",
        ],
        input=json.dumps(candidate),
        text=True,
        capture_output=True,
        check=False,
    )

    # Then: the selected non-empty record is patched and verified successfully.
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.startswith("PASS\n")
    assert "syntax: python" in completed.stdout
