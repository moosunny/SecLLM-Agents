"""G002 계약 미러 테스트 — 설계 §5 필드 매핑의 wire-format 왕복 검증."""

import pytest
from pydantic import ValidationError

from deepagents_pipeline.contracts import (
    ERROR_MODEL_CONNECT,
    ERROR_REGISTRY,
    ERROR_VERIFY_EXHAUSTED,
    AgentModuleRequest,
    build_error_response,
    build_success_response,
)

REQUEST_JSON = {
    "userId": 7,
    "moduleKind": "ActiveSuggestion",
    "requestIdx": 42,
    "model": "qwen3.5-9b",
    "provider": "openai",
    "serviceHost": "localhost",
    "servicePort": "8000",
    "userRequest": {
        "APIKEY": "sk-test",
        "locale": "ko",
        "programmingLanguage": "java",
        "fileExtension": ".java",
        "checkerId": 123,
        "issueLineNumber": 2,
        "entireCode": "public class A {\n    int f() { return 1; }\n}",
        "contexts": [
            {
                "lineNo": 2,
                "eventType": "sink",
                "messageKey": "k",
                "message": "m",
                "params": {"p": 1},
            }
        ],
    },
}


def test_request_roundtrip_camel_case():
    req = AgentModuleRequest.model_validate(REQUEST_JSON)
    assert req.user_id == 7
    assert req.model_name == "qwen3.5-9b"
    assert req.user_request.issue_line_number == 2
    assert req.user_request.contexts[0].line_no == 2

    dumped = req.model_dump(by_alias=True, exclude_none=True)
    assert dumped["userId"] == 7
    assert dumped["userRequest"]["entireCode"].startswith("public class A")
    assert dumped["userRequest"]["APIKEY"] == "sk-test"


def test_issue_line_out_of_range_rejected():
    bad = {**REQUEST_JSON, "userRequest": {**REQUEST_JSON["userRequest"], "issueLineNumber": 99}}
    with pytest.raises(ValidationError):
        AgentModuleRequest.model_validate(bad)


def test_success_envelope_shape():
    req = AgentModuleRequest.model_validate(REQUEST_JSON)
    resp = build_success_response(
        req,
        desc="널 검사를 추가",
        code_modifications=[
            {"type": "delete", "startLine": 2, "endLine": 2},
            {"type": "add", "startLine": 2, "content": "    int f() { return 2; }"},
        ],
        fix_elapsed_time=1.5,
        graph_elapsed_time=2.0,
    )
    dumped = resp.model_dump(by_alias=True)
    assert dumped["isSuccess"] is True
    assert dumped["userId"] == 7
    assert dumped["requestIdx"] == 42
    assert dumped["moduleKind"] == "ActiveSuggestion"
    assert dumped["codeModifications"][0] == {
        "type": "delete",
        "startLine": 2,
        "endLine": 2,
        "content": None,
    }
    # multi_site는 내부 검증 필드 — 응답 어디에도 나타나면 안 된다 (§5.2)
    assert "multi_site" not in str(dumped)
    assert dumped["timings"]["select_elapsed_time"] == 0.0
    assert dumped["timings"]["fix_elapsed_time"] == 1.5


def test_error_envelope_codes():
    req = AgentModuleRequest.model_validate(REQUEST_JSON)
    for error, (code, stage) in [
        (ERROR_VERIFY_EXHAUSTED, ("E301", "select")),
        (ERROR_MODEL_CONNECT, ("E202", "fix")),
        (ERROR_REGISTRY, ("E104", "input")),
    ]:
        resp = build_error_response(req, error=error, message="boom")
        dumped = resp.model_dump(by_alias=True)
        assert dumped["isSuccess"] is False
        assert dumped["errorCode"] == code
        assert dumped["errorStage"] == stage
        assert dumped["errorNode"] == stage
        assert dumped["errorType"]
        assert dumped["userId"] == 7

    orphan = build_error_response(None, error=ERROR_REGISTRY, message="no request")
    assert orphan.model_dump(by_alias=True)["userId"] is None


def test_error_envelope_preserves_explicit_node_and_type():
    req = AgentModuleRequest.model_validate(REQUEST_JSON)
    resp = build_error_response(
        req,
        error=ERROR_MODEL_CONNECT,
        message="fix LLM request failed: GraphRecursionError",
        error_node="fix_branch",
        error_type="GraphRecursionError",
    )

    dumped = resp.model_dump(by_alias=True)
    assert dumped["errorNode"] == "fix_branch"
    assert dumped["errorType"] == "GraphRecursionError"
