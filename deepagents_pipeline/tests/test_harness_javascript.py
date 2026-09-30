"""JavaScript harness admission and real syntax-verifier regression coverage."""

import pytest
from langchain_core.messages import AIMessage

from deepagents_pipeline.harness import run_fix_with_trace
from test_harness import _edit_msg, _fake, _request, _submit_msg


@pytest.mark.parametrize("extension", [None, "", ".js", ".jsx", " .JS "])
def test_javascript_edit_passes_real_verifier(extension: str | None) -> None:
    # Given a JavaScript expression that is not valid Java.
    request = _request(
        programmingLanguage="JavaScript",
        fileExtension=extension,
        entireCode="const render = (element, input) => {\n  element.innerHTML = input;\n};",
        issueLineNumber=2,
        contexts=[],
    )
    model = _fake([
        _edit_msg(1, "element.innerHTML = input;", "element.textContent = input;"),
        _submit_msg(1),
    ])
    # When the full graph edits and submits against the real verifier.
    outcome = run_fix_with_trace(request, model)
    # Then the edit is accepted after actual model turns and validation.
    assert outcome.terminal_cause == "pass"
    assert outcome.model_turns == 2
    assert outcome.response.is_success
    assert [attempt["passed"] for attempt in outcome.attempts] == [True]
    assert outcome.response.code_modifications[-1].content.strip() == "element.textContent = input;"


def test_javascript_syntax_error_requires_revision() -> None:
    # Given a valid JS file and an invalid first edit followed by a repair.
    request = _request(
        programmingLanguage="javascript", fileExtension=".js",
        entireCode="const value = 1;", issueLineNumber=1, contexts=[],
    )
    model = _fake([
        _edit_msg(1, "const value = 1;", "const value = ;"),
        _submit_msg(1),
        _edit_msg(2, "const value = ;", "const value = 2;"),
        _submit_msg(2),
    ])
    # When both candidates go through real syntax validation.
    outcome = run_fix_with_trace(request, model)
    # Then invalid JS is rejected before the valid revision passes.
    assert outcome.terminal_cause == "pass"
    assert [attempt["passed"] for attempt in outcome.attempts] == [False, True]
    assert outcome.attempts[0]["errors"][0]["code"] == "V401"
    assert outcome.model_turns == 4


@pytest.mark.parametrize("language,extension", [
    ("javascript", ".java"), ("javascript", ".ts"),
    ("javascript", ".unknown"), ("unknown", ".js"), ("", ".js"),
    ("ruby", ".py"), (" ", ".js"),
])
def test_incompatible_language_extension_is_rejected(language: str, extension: str) -> None:
    # Given inconsistent or unsupported declared language metadata.
    request = _request(programmingLanguage=language, fileExtension=extension)
    # When admission runs before any model call.
    outcome = run_fix_with_trace(request, _fake([AIMessage("must not run")]))
    # Then admission fails without consuming generation.
    assert outcome.terminal_cause == "unsupported_input"
    assert outcome.model_turns == 0
