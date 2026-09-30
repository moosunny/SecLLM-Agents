"""Admission and real syntax validation for the remaining GRPO languages."""

import pytest
from langchain_core.messages import AIMessage

from deepagents_pipeline.harness import run_fix_with_trace
from test_harness import _edit_msg, _fake, _request, _submit_msg


@pytest.mark.parametrize("language,extension,source", [
    ("C", ".c", "int value(void) { return 1; }"),
    ("c", ".h", "int value(void) { return 1; }"),
    ("C++", ".cpp", "namespace demo { int value() { return 1; } }"),
    ("cpp", ".hpp", "namespace demo { int value() { return 1; } }"),
    ("cpp", ".h", "namespace demo { int value() { return 1; } }"),
    ("cpp", ".hh", "namespace demo { int value() { return 1; } }"),
    ("cpp", ".hxx", "namespace demo { int value() { return 1; } }"),
    ("cpp", ".cc", "namespace demo { int value() { return 1; } }"),
    ("cpp", ".cxx", "namespace demo { int value() { return 1; } }"),
    ("Go", ".go", "package demo\nfunc value() int { return 1 }"),
    ("Python", ".py", "def value():\n    return 1"),
    ("TypeScript", ".ts", "const value: number = 1;"),
    ("typescript", ".tsx", "const view = <div>{1}</div>;"),
])
def test_language_edit_passes_real_verifier(language: str, extension: str, source: str) -> None:
    # Given valid language-specific syntax and an edit.
    request = _request(
        programmingLanguage=language, fileExtension=extension,
        entireCode=source, issueLineNumber=1, contexts=[],
    )
    model = _fake([_edit_msg(1, source, source.replace("1", "2")), _submit_msg(1)])
    # When the actual graph submits through the deterministic verifier.
    outcome = run_fix_with_trace(request, model)
    # Then the requested edit is accepted by the proper grammar.
    assert outcome.terminal_cause == "pass"
    assert outcome.model_turns == 2
    assert outcome.response.is_success
    assert [attempt["passed"] for attempt in outcome.attempts] == [True]
    assert "2" in outcome.response.code_modifications[-1].content


@pytest.mark.parametrize("language,extension,source,invalid", [
    ("c", ".c", "int value = 1;", "int value = ;"),
    ("cpp", ".hpp", "int value = 1;", "int value = ;"),
    ("go", ".go", "package demo\nvar value = 1", "package demo\nvar value ="),
    ("python", ".py", "value = 1", "value ="),
    ("typescript", ".ts", "const value: number = 1;", "const value: number = ;"),
    ("typescript", ".tsx", "const view = <div>{1}</div>;", "const view = <div>{1 + }</div>;"),
])
def test_invalid_syntax_requires_repair(language: str, extension: str, source: str, invalid: str) -> None:
    # Given invalid generated syntax and a subsequent valid repair.
    request = _request(
        programmingLanguage=language, fileExtension=extension,
        entireCode=source, issueLineNumber=1, contexts=[],
    )
    model = _fake([
        _edit_msg(1, source, invalid), _submit_msg(1),
        _edit_msg(2, invalid, source.replace("1", "2")), _submit_msg(2),
    ])
    # When both submissions undergo real syntax validation.
    outcome = run_fix_with_trace(request, model)
    # Then invalid syntax is rejected before the repair passes.
    assert outcome.terminal_cause == "pass"
    assert [attempt["passed"] for attempt in outcome.attempts] == [False, True]
    assert outcome.attempts[0]["errors"][0]["code"] == "V401"


@pytest.mark.parametrize("language,extension", [
    ("c", ".cpp"), ("cpp", ".py"), ("python", ".h"),
    ("go", ".ts"), ("typescript", ".js"), ("ruby", ".rb"),
])
def test_conflicting_language_extension_is_rejected(language: str, extension: str) -> None:
    # Given incompatible declarations.
    request = _request(programmingLanguage=language, fileExtension=extension)
    # When admission runs.
    outcome = run_fix_with_trace(request, _fake([AIMessage("must not run")]))
    # Then no generation is consumed.
    assert outcome.terminal_cause == "unsupported_input"
    assert outcome.model_turns == 0
