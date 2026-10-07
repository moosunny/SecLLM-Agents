"""G003 완전형 하네스 테스트 — fake chat model로 LLM-free 루프 검증 (§9, edit_code 개정)."""

import json
from pathlib import Path

from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from deepagents_pipeline.contracts import AgentModuleRequest
from deepagents_pipeline.harness import (
    DENIED_TOOLS,
    MAX_CONSECUTIVE_EDIT_FAILURES,
    MAX_FIX_ATTEMPTS,
    MAX_MODEL_TURNS,
    MAX_NO_TOOL_NUDGES,
    SUPPORTED_LANGUAGES,
    HarnessControlMiddleware,
    SubmissionHolder,
    build_agent,
    build_meta,
    build_target_chunks,
    make_fix_tools,
    mods_from_diff,
    render_target_chunks,
    run_fix,
    run_fix_with_trace,
)

JAVA_LINES = (
    ["public class Demo {"]
    + [f"    int f{i}() {{ return {i}; }}" for i in range(1, 79)]
    + ["}"]
)
ENTIRE_CODE = "\n".join(JAVA_LINES)

GOOD_OLD = "    int f4() { return 4; }"  # 5번 줄, 파일 내 유일
GOOD_NEW = "    int f4() { return 400; }"
KDESC = "취약한 반환식을 안전한 상수 반환으로 교체"
BAD_DESC = "replace the vulnerable return expression"  # 한글 없음 → V110


def _request(**user_request_overrides) -> AgentModuleRequest:
    user_request = {
        "locale": "ko",
        "programmingLanguage": "java",
        "checkerId": 77,
        "issueLineNumber": 5,
        "entireCode": ENTIRE_CODE,
        "contexts": [{"lineNo": 5, "eventType": "sink", "messageKey": "k", "message": "m"}],
    }
    user_request.update(user_request_overrides)
    return AgentModuleRequest.model_validate(
        {
            "userId": 1,
            "moduleKind": "ActiveSuggestion",
            "requestIdx": 5,
            "model": "fake",
            "provider": "openai",
            "serviceHost": "localhost",
            "servicePort": "0",
            "userRequest": user_request,
        }
    )


def _tc(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _edit_msg(seq: int, old: str = GOOD_OLD, new: str = GOOD_NEW) -> AIMessage:
    return AIMessage(
        "", tool_calls=[_tc("edit_code", {"old_string": old, "new_string": new}, f"e{seq}")]
    )


def _submit_msg(seq: int, desc: str = KDESC) -> AIMessage:
    return AIMessage("", tool_calls=[_tc("submit_fix", {"desc": desc}, f"s{seq}")])



class _ToolCallingFakeModel(GenericFakeChatModel):
    """GenericFakeChatModel은 bind_tools 미구현 — 도구 바인딩을 무시하고 자신을 반환."""

    def bind_tools(self, tools, **kwargs):
        return self


def _fake(messages: list[AIMessage]) -> GenericFakeChatModel:
    return _ToolCallingFakeModel(messages=iter(messages))


class _CountingFakeModel(_ToolCallingFakeModel):
    calls: int = 0

    def _generate(self, *args, **kwargs):
        object.__setattr__(self, "calls", self.calls + 1)
        return super()._generate(*args, **kwargs)


def _apply_wire_operations(original: list[str], modifications: list[dict]) -> list[str]:
    """Apply the public add/delete wire contract without verifier internals.

    This deliberately models the consumer contract rather than importing
    ``verify_fix._group_ops`` or ``verify_fix._apply``.  Adds occur before the
    original line at ``startLine``; deletes use inclusive original coordinates.
    """
    deletes: dict[int, int] = {}
    additions: dict[int, list[str]] = {}
    for modification in modifications:
        match modification["type"]:
            case "delete":
                deletes[modification["startLine"]] = modification["endLine"]
            case "add":
                additions.setdefault(modification["startLine"], []).extend(
                    modification["content"].split("\n")
                )
            case unexpected:
                raise AssertionError(f"unexpected wire operation: {unexpected!r}")

    result: list[str] = []
    line = 1
    while line <= len(original) + 1:
        result.extend(additions.get(line, []))
        end = deletes.get(line)
        if end is not None:
            line = end + 1
        else:
            if line <= len(original):
                result.append(original[line - 1])
            line += 1
    return result


def test_build_meta_omits_checker_id_while_request_accepts_it():
    request = _request()

    assert request.user_request.checker_id == 77
    assert "checkerId" not in build_meta(request.user_request)


def test_build_meta_is_english_first_and_drops_source_code():
    request = _request(
        issueNameEn="Race Condition",
        issueNameKo="경쟁 조건",
        sourceCode="5. int f4() { return 4; }",
    )

    meta = build_meta(request.user_request)

    assert meta["issueName"] == "Race Condition"
    assert "issueNameKo" not in meta
    assert "sourceCode" not in meta


def test_build_meta_falls_back_to_korean_when_english_missing():
    request = _request(issueNameEn="", issueNameKo="경쟁 조건")

    assert build_meta(request.user_request)["issueName"] == "경쟁 조건"


def _wide_request(sink_line: int, event_lines: list[int]):
    body = [f"    int f{i}() {{ return {i}; }}  // {'padding ' * 6}" for i in range(1, 401)]
    code = "\n".join(["public class Wide {", *body, "}"])
    return _request(
        entireCode=code,
        issueLineNumber=sink_line,
        contexts=[
            {"lineNo": line, "eventType": kind, "messageKey": "k", "message": "m"}
            for line, kind in [(sink_line, "sink"), *[(l, "source") for l in event_lines]]
        ],
    )


def test_target_chunks_include_trace_event_chunk_outside_the_sink_chunk():
    request = _wide_request(sink_line=3, event_lines=[395])

    pack = build_target_chunks(request.user_request)

    assert len(pack) >= 2
    assert pack[0].start_line <= 3 <= pack[0].end_line
    assert any(chunk.start_line <= 395 <= chunk.end_line for chunk in pack[1:])


def test_target_chunks_deduplicate_events_sharing_the_sink_chunk():
    request = _wide_request(sink_line=3, event_lines=[4, 5])

    pack = build_target_chunks(request.user_request)

    assert len(pack) == 1
    assert pack[0].start_line <= 5 <= pack[0].end_line


def test_rendered_chunks_use_the_read_code_gutter():
    request = _wide_request(sink_line=3, event_lines=[395])
    pack = build_target_chunks(request.user_request)

    rendered = render_target_chunks(pack)

    assert rendered.count("```java") == len(pack)
    assert f"lines {pack[0].start_line}-{pack[0].end_line}" in rendered
    first_body_line = rendered.split("```java\n")[1].splitlines()[0]
    assert first_body_line.strip().startswith(str(pack[0].start_line))


def test_rendered_chunks_tag_the_fence_with_the_resolved_language():
    request = _wide_request(sink_line=3, event_lines=[395])
    pack = build_target_chunks(request.user_request)

    rendered = render_target_chunks(pack, language="javascript")

    assert rendered.count("```javascript") == len(pack)
    assert "```java\n" not in rendered


def test_pass_first_try_success_envelope():
    msgs = [_edit_msg(1), _submit_msg(1)]
    resp = run_fix(_request(), _fake(msgs))
    dumped = resp.model_dump(by_alias=True)
    assert dumped["isSuccess"] is True
    assert dumped["desc"] == KDESC
    # diff 후처리: 5번 줄 치환 → delete(5..5) + add(5)
    assert len(dumped["codeModifications"]) == 2
    assert dumped["codeModifications"][0]["type"] == "delete"
    assert dumped["codeModifications"][0]["startLine"] == 5
    assert dumped["codeModifications"][1]["content"] == GOOD_NEW
    assert "multi_site" not in str(dumped)
    assert dumped["timings"]["select_elapsed_time"] == 0.0
    assert dumped["timings"]["graph_elapsed_time"] >= dumped["timings"]["fix_elapsed_time"] >= 0


def test_two_distant_edits_pass_multi_site_review_end_to_end():
    far_old = "    int f69() { return 69; }"
    far_new = "    int f69() { return 6900; }"
    msgs = [
        _edit_msg(1),
        _edit_msg(2, old=far_old, new=far_new),
        _submit_msg(1),
    ]

    outcome = run_fix_with_trace(_request(), _fake(msgs))
    dumped = outcome.response.model_dump(by_alias=True)

    assert dumped["isSuccess"] is True
    assert outcome.terminal_cause == "pass"
    assert outcome.model_turns == 3
    assert [attempt["passed"] for attempt in outcome.attempts] == [True]
    assert "operation 2" in outcome.attempts[0]["regions"]
    assert outcome.candidates[0]["multi_site"] is True
    assert [mod["type"] for mod in dumped["codeModifications"]] == [
        "delete",
        "add",
        "delete",
        "add",
    ]
    assert [mod["startLine"] for mod in dumped["codeModifications"]] == [5, 5, 70, 70]
    assert dumped["codeModifications"][1]["content"] == GOOD_NEW
    assert dumped["codeModifications"][3]["content"] == far_new
    assert "multi_site" not in str(dumped)


def test_fail_then_revise_then_pass_with_trace():
    # 편집은 유효하지만 desc가 영어 → V110 FAIL → 한국어 desc로 재제출 → PASS
    msgs = [
        _edit_msg(1),
        _submit_msg(1, BAD_DESC),
        _submit_msg(2, KDESC),
    ]
    outcome = run_fix_with_trace(_request(), _fake(msgs))
    assert outcome.response.model_dump(by_alias=True)["isSuccess"] is True
    assert outcome.terminal_cause == "pass"
    assert [a["passed"] for a in outcome.attempts] == [False, True]
    assert len(outcome.candidates) == 2  # §12(b): 제출 candidate 전량 보존
    assert outcome.edit_calls == 1 and outcome.edit_errors == 0


def test_pass_terminates_structurally():
    # 검증 PASS 후 정확한 review 승인이 있어야 성공하고 이후 낭비 턴은 실행되지 않는다.
    wasteful = _edit_msg(9, old=GOOD_NEW, new="waste")
    msgs = [_edit_msg(1), _submit_msg(1), wasteful]
    outcome = run_fix_with_trace(_request(), _fake(msgs))
    assert outcome.response.model_dump(by_alias=True)["isSuccess"] is True
    assert outcome.terminal_cause == "pass"
    assert outcome.model_turns == 2  # edit, submit








def test_exhaustion_emits_e301_with_cause():
    msgs = [_edit_msg(1)] + [_submit_msg(i, BAD_DESC) for i in (1, 2, 3)] + [AIMessage("실패")]
    outcome = run_fix_with_trace(_request(), _fake(msgs))
    dumped = outcome.response.model_dump(by_alias=True)
    assert dumped["isSuccess"] is False
    assert dumped["errorCode"] == "E301"
    assert dumped["errorStage"] == "select"
    assert "3 attempt" in dumped["errorMessage"]
    assert outcome.terminal_cause == "budget_exhausted"
    assert "budget_exhausted" in dumped["errorMessage"]


def test_edit_failure_spiral_terminates_edit_exhausted():
    # 2026-08-10 A/B 갭: 매칭 실패는 예산 미소모라 상한이 없으면 recursion_limit까지
    # 나선을 돈다 — 연속 실패 캡이 구조적으로 끊어야 한다.
    msgs = [_edit_msg(i, old="NOPE", new="x") for i in range(1, MAX_CONSECUTIVE_EDIT_FAILURES + 1)]
    msgs += [_edit_msg(99, old="NOPE", new="x"), AIMessage("잔여")]  # 소비되면 안 됨
    outcome = run_fix_with_trace(_request(), _fake(msgs))
    dumped = outcome.response.model_dump(by_alias=True)
    assert dumped["isSuccess"] is False
    assert dumped["errorCode"] == "E301"
    assert outcome.terminal_cause == "edit_exhausted"
    assert outcome.model_turns == MAX_CONSECUTIVE_EDIT_FAILURES
    assert "failed edit_code" in dumped["errorMessage"]
    assert outcome.edit_errors == MAX_CONSECUTIVE_EDIT_FAILURES
    assert outcome.edit_failure_kinds == ["not_found"] * MAX_CONSECUTIVE_EDIT_FAILURES


class _BoomModel(GenericFakeChatModel):
    exc: Exception = RuntimeError("boom")

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        raise self.exc


def test_llm_error_maps_to_e202():
    # 프로덕션 미러 (fix.py:92): RuntimeError 포함 튜플 → E202/fix.
    outcome = run_fix_with_trace(_request(), _BoomModel(messages=iter([])))
    dumped = outcome.response.model_dump(by_alias=True)
    assert dumped["isSuccess"] is False
    assert dumped["errorCode"] == "E202"
    assert dumped["errorStage"] == "fix"
    assert dumped["errorNode"] == "fix_branch"
    assert dumped["errorType"] == "RuntimeError"
    assert dumped["errorMessage"] == "fix LLM request failed: RuntimeError"
    assert outcome.terminal_cause == "connection_error"


def test_unexpected_error_maps_to_e999():
    class _Weird(Exception):
        pass

    outcome = run_fix_with_trace(_request(), _BoomModel(messages=iter([]), exc=_Weird("?")))
    dumped = outcome.response.model_dump(by_alias=True)
    assert dumped["isSuccess"] is False
    assert dumped["errorCode"] == "E999"
    assert dumped["errorStage"] == "output"
    assert dumped["errorNode"] == "fix_branch"
    assert dumped["errorType"] == "_Weird"
    assert dumped["errorMessage"] == "?"
    assert outcome.terminal_cause == "internal_error"


def test_no_submission_emits_e301():
    # 2026-08-27: 도구 호출 없는 턴은 MAX_NO_TOOL_NUDGES회 재촉되므로 그만큼 응답을 더 준다.
    msgs = [AIMessage(f"아무것도 안 함 {i}") for i in range(MAX_NO_TOOL_NUDGES + 1)]
    resp = run_fix(_request(), _fake(msgs))
    dumped = resp.model_dump(by_alias=True)
    assert dumped["isSuccess"] is False
    assert dumped["errorCode"] == "E301"


class _BoomAfterModel(GenericFakeChatModel):
    """정상 응답 n회 뒤 연결 오류 — 실패 레코드의 토큰 계측 보존 검증용."""

    ok_turns: int = 2
    seen: int = 0

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        object.__setattr__(self, "seen", self.seen + 1)
        if self.seen > self.ok_turns:
            raise RuntimeError("upstream 503")
        return super()._generate(*args, **kwargs)


def test_usage_preserved_when_model_raises_midrun():
    # §12(c) 비용/지연 게이트: 예외로 끝난 레코드의 지출을 0으로 세면 안 된다.
    msgs = [_edit_msg(1), _submit_msg(1, BAD_DESC)] + [AIMessage("x")] * 4
    outcome = run_fix_with_trace(_request(), _BoomAfterModel(messages=iter(msgs)))
    dumped = outcome.response.model_dump(by_alias=True)
    assert dumped["errorCode"] == "E202"
    assert outcome.terminal_cause == "connection_error"
    assert outcome.attempts, "예외 전 제출 이력은 남아야 한다"
    assert outcome.model_turns == 2, f"실제 모델 턴이 유실됐다: {outcome.model_turns}"
    assert outcome.trace_dict()["model_turns"] == 2


def test_unknown_language_rejected_e103():
    assert "ruby" not in SUPPORTED_LANGUAGES
    req_data = _request().model_dump(by_alias=True)
    req_data["userRequest"]["programmingLanguage"] = "ruby"
    req = AgentModuleRequest.model_validate(req_data)
    outcome = run_fix_with_trace(req, _fake([AIMessage("호출되면 안 됨")]))
    dumped = outcome.response.model_dump(by_alias=True)
    assert dumped["isSuccess"] is False
    assert dumped["errorCode"] == "E103"
    assert dumped["errorStage"] == "input"
    assert outcome.terminal_cause == "unsupported_input"
    assert outcome.model_turns == 0  # 모델 호출 자체가 없어야 한다


def test_java_boundary_accepts_java_with_empty_or_java_extension():
    for extension in (None, "", " .JAVA "):
        model = _CountingFakeModel(messages=iter([_edit_msg(1), _submit_msg(1)]))
        outcome = run_fix_with_trace(_request(fileExtension=extension), model)

        assert outcome.response.is_success is True
        assert model.calls == 2


JS_LINES = ["const db = require('./db')", "", "function load(id) {"] + [
    f"  const q{i} = {i}" for i in range(1, 40)
] + ["  return db.query(`SELECT * FROM t WHERE id='${id}'`)", "}", "module.exports = { load }"]
JS_OLD = "  return db.query(`SELECT * FROM t WHERE id='${id}'`)"
JS_NEW = "  return db.query('SELECT * FROM t WHERE id = ?', [id])"
TS_LINES = ["interface User { id: string }", "", "function load(id: string): User {"] + [
    f"  const q{i}: number = {i}" for i in range(1, 40)
] + ["  return db.query(`SELECT * FROM t WHERE id='${id}'`)", "}"]
TS_OLD = "  return db.query(`SELECT * FROM t WHERE id='${id}'`)"
TS_NEW = "  return db.query('SELECT * FROM t WHERE id = ?', [id])"
C_LINES = ["#include <stdio.h>", "", "int clamp(int x) {"] + [
    f"  int v{i} = {i};" for i in range(1, 40)
] + ["  return x;", "}"]
C_OLD = "  return x;"
C_NEW = "  return x < 0 ? 0 : x;"
CPP_LINES = ["#include <iostream>", "", "int clamp(int x) {"] + [
    f"  int v{i} = {i};" for i in range(1, 40)
] + ["  return x;", "}"]
CPP_OLD = "  return x;"
CPP_NEW = "  return x < 0 ? 0 : x;"
GO_LINES = ["package main", "", "import \"crypto/md5\"", "", "func hash(b []byte) [16]byte {"] + [
    f"\tvar v{i} = {i}" for i in range(1, 40)
] + ["\treturn md5.Sum(b)", "}"]
GO_OLD = "\treturn md5.Sum(b)"
GO_NEW = "\treturn sha256.Sum256(b)"
PYTHON_LINES = ["def clamp(x):"] + [
    f"    v{i} = {i}" for i in range(1, 40)
] + ["    return x"]
PYTHON_OLD = "    return x"
PYTHON_NEW = "    return max(x, 0)"


def test_supported_languages_accepted_and_verified_with_their_own_grammar():
    cases = (
        ("JavaScript", ".js", JS_LINES, JS_OLD, JS_NEW),
        ("TypeScript", ".ts", TS_LINES, TS_OLD, TS_NEW),
        ("C", ".c", C_LINES, C_OLD, C_NEW),
        ("C++", ".cpp", CPP_LINES, CPP_OLD, CPP_NEW),
        ("Go", ".go", GO_LINES, GO_OLD, GO_NEW),
        ("Python", ".py", PYTHON_LINES, PYTHON_OLD, PYTHON_NEW),
    )
    for language, extension, lines, old, new in cases:
        code = "\n".join(lines)
        sink = lines.index(old) + 1
        model = _CountingFakeModel(messages=iter([_edit_msg(1, old, new), _submit_msg(1)]))
        outcome = run_fix_with_trace(
            _request(
                programmingLanguage=language,
                fileExtension=extension,
                entireCode=code,
                issueLineNumber=sink,
                contexts=[{"lineNo": sink, "eventType": "sink", "messageKey": "k", "message": "m"}],
            ),
            model,
        )

        assert outcome.response.is_success is True, (language, outcome.response)
        assert outcome.terminal_cause == "pass"
        assert model.calls == 2
        mods = outcome.response.model_dump(by_alias=True)["codeModifications"]
        assert _apply_wire_operations(lines, mods) == [new if l == old else l for l in lines]


def test_supported_language_edit_that_breaks_syntax_fails_v401_under_that_grammar():
    # 자바 파서였다면 JS 원본 자체가 에러투성이라 차등 검사가 무의미해진다 — JS 문법으로
    # 판정돼야 괄호 하나 빠진 편집이 V401로 잡힌다.
    code = "\n".join(JS_LINES)
    sink = JS_LINES.index(JS_OLD) + 1
    broken = "  return db.query('SELECT * FROM t WHERE id = ?', [id]"
    model = _fake([_edit_msg(1, JS_OLD, broken), _submit_msg(1)])
    outcome = run_fix_with_trace(
        _request(
            programmingLanguage="JavaScript",
            fileExtension=".js",
            entireCode=code,
            issueLineNumber=sink,
            contexts=[{"lineNo": sink, "eventType": "sink", "messageKey": "k", "message": "m"}],
        ),
        model,
    )

    assert outcome.attempts and outcome.attempts[0]["passed"] is False
    assert {e["code"] for e in outcome.attempts[0]["errors"]} == {"V401"}


def test_java_boundary_rejects_conflicting_or_unknown_extension_before_model_call():
    for language, extension in (
        ("java", ".py"),
        ("python", ".java"),
        ("java", ".unknown"),
        ("javascript", ".go"),
        ("go", ".js"),
    ):
        model = _CountingFakeModel(messages=iter([AIMessage("must not run")]))
        outcome = run_fix_with_trace(
            _request(programmingLanguage=language, fileExtension=extension), model
        )
        dumped = outcome.response.model_dump(by_alias=True)

        assert dumped["errorCode"] == "E103"
        assert dumped["errorStage"] == "input"
        assert outcome.terminal_cause == "unsupported_input"
        assert outcome.model_turns == 0
        assert model.calls == 0


# --- edit_code/submit_fix 단위 테스트 (그래프 밖 직접 호출) ---


def _tools(holder: SubmissionHolder):
    _, edit_code, submit_fix = make_fix_tools(ENTIRE_CODE.split("\n"), holder)
    return edit_code, submit_fix


def test_pass_immediately_accepts_the_candidate():
    # 리뷰 단계 제거(2026-08-22): verifier PASS가 유일 게이트 — 즉시 승격된다.
    holder = SubmissionHolder()
    edit, submit = _tools(holder)
    edit.invoke({"old_string": GOOD_OLD, "new_string": GOOD_NEW})

    out = submit.invoke({"desc": KDESC})

    assert "PASS" in out and "Stop now" in out
    assert holder.passed
    assert holder.accepted["desc"] == KDESC


def test_submit_after_pass_is_blocked():
    holder = SubmissionHolder()
    edit, submit = _tools(holder)
    edit.invoke({"old_string": GOOD_OLD, "new_string": GOOD_NEW})
    submit.invoke({"desc": KDESC})

    assert submit.invoke({"desc": KDESC}).startswith("Already PASSED")
    assert holder.attempt_count == 1



def test_read_code_reflects_working_copy_after_edit():
    holder = SubmissionHolder()
    read_code, edit_code, _submit = make_fix_tools(ENTIRE_CODE.split("\n"), holder)

    edit_code.invoke({"old_string": GOOD_OLD, "new_string": GOOD_NEW})
    out = read_code.invoke({"offset": 4, "limit": 1})

    assert out.startswith(f"5  {GOOD_NEW}\n")
    assert GOOD_OLD not in out


def test_edit_not_found_does_not_consume_budget():
    holder = SubmissionHolder()
    edit, _ = _tools(holder)
    out = edit.invoke({"old_string": "NOPE", "new_string": "x"})
    assert "not found" in out
    assert holder.attempt_count == 0
    assert holder.edit_errors == 1
    assert holder.edit_failure_kinds == ["not_found"]
    assert holder.consecutive_edit_failures == 1


def test_noop_edit_identical_strings_rejected():
    holder = SubmissionHolder()
    edit, _ = _tools(holder)

    out = edit.invoke({"old_string": GOOD_OLD, "new_string": GOOD_OLD})

    assert out.startswith("ERROR:")
    assert holder.edit_failure_kinds == ["noop"]
    assert holder.consecutive_edit_failures == 1
    assert holder.attempt_count == 0


def test_fuzzy_matched_noop_rejected():
    # old_string은 파일과 다르지만(후행 공백) fuzzy가 잡은 원문 스팬이 new_string과
    # 동일한 경우 — 파일은 이미 원하는 상태이므로 OK가 아니라 거부돼야 한다.
    holder = SubmissionHolder()
    edit, _ = _tools(holder)

    out = edit.invoke({"old_string": GOOD_OLD + "  ", "new_string": GOOD_OLD})

    assert out.startswith("ERROR:")
    assert holder.edit_failure_kinds == ["noop"]


def test_noop_spiral_terminates_at_edit_exhaustion():
    msgs = [
        _edit_msg(i, old=GOOD_OLD, new=GOOD_OLD)
        for i in range(1, MAX_CONSECUTIVE_EDIT_FAILURES + 1)
    ] + [AIMessage("잔여")]

    outcome = run_fix_with_trace(_request(), _fake(msgs))

    assert outcome.response.model_dump(by_alias=True)["isSuccess"] is False
    assert outcome.terminal_cause == "edit_exhausted"
    assert outcome.edit_failure_kinds == ["noop"] * MAX_CONSECUTIVE_EDIT_FAILURES


def test_edit_ambiguous_still_rejected():
    holder = SubmissionHolder()
    edit, _ = _tools(holder)
    out = edit.invoke({"old_string": "    int f", "new_string": "x"})
    assert "more than one location" in out
    assert holder.edit_failure_kinds == ["ambiguous"]


# --- replacer 폴백 체인 (OpenCode replace() 포팅, 2026-08-20) ---


def test_edit_fuzzy_recovers_gutter_indent_error():
    # 관측된 지배적 실패: 거터 2칸 흡수로 들여쓰기 +2칸 → 기존엔 not_found.
    holder = SubmissionHolder()
    edit, submit = _tools(holder)
    wrong_indent = "      " + GOOD_OLD.lstrip()  # 실제 4칸 → 6칸으로 오기
    out = edit.invoke({"old_string": wrong_indent, "new_string": GOOD_NEW})
    assert out.startswith("OK.")
    assert holder.edit_errors == 0
    assert holder.fuzzy_matches == 1
    assert holder.fuzzy_kinds == ["line_trimmed"]
    assert "did not match exactly" in out  # 모델에게 폴백 매칭 사실을 고지
    # 적용된 스팬은 원문이므로 diff→제출까지 정상 동작해야 한다
    assert "PASS" in submit.invoke({"desc": KDESC})


def test_edit_fuzzy_multiline_block_with_wrong_indent():
    holder = SubmissionHolder()
    edit, _ = _tools(holder)
    block = "\n".join("  " + JAVA_LINES[i] for i in (3, 4, 5))  # 3줄 전체 +2칸
    out = edit.invoke({"old_string": block, "new_string": "    // replaced"})
    assert out.startswith("OK.")
    assert holder.fuzzy_matches == 1
    # 치환 결과가 작업본에 실재해야 한다 — 이어지는 exact 편집이 성공하면 증명된다
    out2 = edit.invoke({"old_string": "    // replaced", "new_string": "    // again"})
    assert out2.startswith("OK.")
    assert holder.fuzzy_matches == 1  # 두 번째는 exact 매칭


def test_fuzzy_edit_reindents_new_string_to_the_file():
    # rerun2 잔여 실패 5/6: 모델이 old/new 모두 같은 오기 들여쓰기로 쓰면 오염된
    # 블록이 삽입돼 복구 나선에 빠졌다 — 파일 실재 들여쓰기(span)로 보정돼야 한다.
    holder = SubmissionHolder()
    edit, _ = _tools(holder)
    wrong_old = "        " + GOOD_OLD.lstrip()  # 실제 4칸 → 8칸 오기
    wrong_new = "        " + GOOD_NEW.lstrip()  # new도 같은 8칸

    out = edit.invoke({"old_string": wrong_old, "new_string": wrong_new})

    assert out.startswith("OK.")
    assert holder.reindented_edits == 1
    assert "re-indented" in out
    # 4칸으로 보정 삽입됐음을 exact 재매칭으로 증명
    out2 = edit.invoke({"old_string": GOOD_NEW, "new_string": GOOD_OLD})
    assert out2.startswith("OK.")


def test_fuzzy_reindent_preserves_relative_structure():
    holder = SubmissionHolder()
    edit, _ = _tools(holder)
    wrong_old = "        " + GOOD_OLD.lstrip()
    wrong_new = "        if (true) {\n            return 400;\n        }"

    out = edit.invoke({"old_string": wrong_old, "new_string": wrong_new})

    assert out.startswith("OK.")
    assert holder.reindented_edits == 1
    expected = "    if (true) {\n        return 400;\n    }"
    out2 = edit.invoke({"old_string": expected, "new_string": GOOD_OLD})
    assert out2.startswith("OK.")


def test_exact_match_never_reindents_new_string():
    holder = SubmissionHolder()
    edit, _ = _tools(holder)
    over_indented_new = "        " + GOOD_NEW.lstrip()

    out = edit.invoke({"old_string": GOOD_OLD, "new_string": over_indented_new})

    assert out.startswith("OK.")
    assert holder.reindented_edits == 0
    # 8칸 그대로 삽입됐음을 exact 재매칭으로 증명 (모델 의도 존중)
    out2 = edit.invoke({"old_string": over_indented_new, "new_string": GOOD_OLD})
    assert out2.startswith("OK.")


def test_edit_exact_match_has_no_fuzzy_note():
    holder = SubmissionHolder()
    edit, _ = _tools(holder)
    out = edit.invoke({"old_string": GOOD_OLD, "new_string": GOOD_NEW})
    assert out.startswith("OK.")
    assert holder.fuzzy_matches == 0
    assert "did not match exactly" not in out


def test_edit_truly_absent_still_not_found():
    holder = SubmissionHolder()
    edit, _ = _tools(holder)
    out = edit.invoke({"old_string": "THIS TEXT EXISTS NOWHERE", "new_string": "x"})
    assert "not found" in out
    assert holder.edit_failure_kinds == ["not_found"]
    assert holder.fuzzy_matches == 0


def test_find_unique_match_disproportionate_guard():
    from deepagents_pipeline.replacers import find_unique_match

    # 앵커 2줄만 주면 block_anchor가 훨씬 큰 블록을 잡을 수 있다 — 가드가 거부해야 한다
    content = "\n".join(["void a() {"] + [f"    x{i};" for i in range(10)] + ["}"])
    match, err = find_unique_match(content, "void a() {\n}")
    assert match is None and err in ("not_found", "disproportionate")


def test_edit_success_resets_consecutive_counter():
    holder = SubmissionHolder()
    edit, _ = _tools(holder)
    edit.invoke({"old_string": "NOPE", "new_string": "x"})
    edit.invoke({"old_string": "NOPE", "new_string": "x"})
    assert holder.consecutive_edit_failures == 2
    out = edit.invoke({"old_string": GOOD_OLD, "new_string": GOOD_NEW})
    assert out.startswith("OK.")
    assert holder.consecutive_edit_failures == 0
    assert holder.edit_errors == 2  # 누적 총계는 유지


def test_submit_without_edits_does_not_consume_budget():
    holder = SubmissionHolder()
    _, submit = _tools(holder)
    out = submit.invoke({"desc": KDESC})
    assert "no edits applied" in out
    assert holder.attempt_count == 0


def test_noop_edit_then_submit_is_not_a_submission():
    # old == new인 편집은 성공 메시지를 반환하지만 작업본이 원본과 동일하므로
    # 제출 단계에서 예산 미소모로 걸러진다.
    holder = SubmissionHolder()
    edit, submit = _tools(holder)
    edit.invoke({"old_string": GOOD_OLD, "new_string": GOOD_OLD})
    out = submit.invoke({"desc": KDESC})
    assert "no edits applied" in out
    assert holder.attempt_count == 0


def test_submit_non_korean_desc_rejected():
    holder = SubmissionHolder()
    edit, submit = _tools(holder)
    edit.invoke({"old_string": GOOD_OLD, "new_string": GOOD_NEW})
    out = submit.invoke({"desc": BAD_DESC})
    assert "FAIL" in out
    assert any(e["code"] == "V110" for e in holder.attempts[0]["errors"])


def test_submit_non_korean_desc_skips_deterministic_verifier(monkeypatch):
    def unexpected_check(*args, **kwargs):
        raise AssertionError("later verification stages must not run for an invalid desc")

    monkeypatch.setattr("deepagents_pipeline.harness._roundtrip_ok", unexpected_check)
    monkeypatch.setattr("deepagents_pipeline.harness.verify_candidate", unexpected_check)
    holder = SubmissionHolder()
    edit, submit = _tools(holder)
    edit.invoke({"old_string": GOOD_OLD, "new_string": GOOD_NEW})

    out = submit.invoke({"desc": BAD_DESC})

    assert "FAIL" in out
    assert holder.attempt_count == 1
    assert holder.attempts[0]["errors"][0]["code"] == "V110"


def test_submit_token_korean_desc_rejected_by_ratio():
    # 식별자 제거 후 남는 산문이 한글 1자뿐인 영어 문장은 거부된다
    holder = SubmissionHolder()
    edit, submit = _tools(holder)
    edit.invoke({"old_string": GOOD_OLD, "new_string": GOOD_NEW})
    submit.invoke({"desc": "replace the vulnerable return expression 값"})
    assert any(e["code"] == "V110" for e in holder.attempts[0]["errors"])


def test_identifier_heavy_korean_descs_accepted():
    # Java 식별자 비중이 높은 실전형 한국어 desc들은 전부 통과해야 한다
    descs = [
        "허용된 예외 타입만 던지도록 CodecException 체크를 DecodingException 체크로 변경합니다",
        "NullPointerException 방지",
        "throw new RuntimeException을 IllegalArgumentException으로 교체",
        "HttpServletRequest.getParameter() 반환값을 PreparedStatement 바인딩으로 교체",
    ]
    for desc in descs:
        holder = SubmissionHolder()
        edit, submit = _tools(holder)
        edit.invoke({"old_string": GOOD_OLD, "new_string": GOOD_NEW})
        out = submit.invoke({"desc": desc})
        assert "PASS" in out, f"false V110 reject: {desc!r} -> {out[:200]}"


def test_submit_budget_exhausted_message():
    holder = SubmissionHolder()
    edit, submit = _tools(holder)
    edit.invoke({"old_string": GOOD_OLD, "new_string": GOOD_NEW})
    for _ in range(MAX_FIX_ATTEMPTS):
        submit.invoke({"desc": BAD_DESC})
    assert holder.attempt_count == MAX_FIX_ATTEMPTS
    out = submit.invoke({"desc": BAD_DESC})
    assert "BUDGET EXHAUSTED" in out
    assert holder.attempt_count == MAX_FIX_ATTEMPTS  # 초과 호출은 예산 미소모



def test_mods_from_diff_shapes_and_roundtrip():
    from deepagents_pipeline.harness import _roundtrip_ok

    orig = JAVA_LINES
    cases = {
        "치환": orig[:4] + [GOOD_NEW] + orig[5:],
        "삽입": orig[:4] + ["    // guard"] + orig[4:],
        "삭제": orig[:4] + orig[6:],
        "EOF append": orig + ["// tail"],
        "근접 병합": orig[:4] + [GOOD_NEW] + orig[5:7] + ["    int f7() { return 700; }"] + orig[8:],
        "임계 분리": orig[:4] + [GOOD_NEW] + orig[5:10] + ["    int f10() { return 1000; }"] + orig[11:],
        "원거리 multi": orig[:4] + [GOOD_NEW] + orig[5:70] + ["    int f70() { return 7000; }"] + orig[71:],
    }
    for name, mod in cases.items():
        mods, multi = mods_from_diff(orig, mod)
        assert _roundtrip_ok(orig, mods, mod), name
        types = {m["type"] for m in mods}
        assert types <= {"add", "delete"}, name
        if name == "근접 병합":
            # 갭 2줄(<MERGE_GAP=5) → 단일 연속 치환으로 병합, multi_site 아님
            assert multi is False
            assert [m["type"] for m in mods] == ["delete", "add"]
        if name == "임계 분리":
            # 갭 5줄(>=MERGE_GAP) → 병합하지 않는다. 30이던 시절에는 이 케이스가
            # 사이의 미변경 5줄까지 삭제 스팬에 흡수했다.
            assert multi is True
            assert [m["type"] for m in mods] == ["delete", "add", "delete", "add"]
            assert all(m["endLine"] - m["startLine"] == 0 for m in mods if m["type"] == "delete")
        if name == "원거리 multi":
            assert multi is True


def test_mods_from_diff_roundtrips_blank_insert_across_following_delete():
    original = [" ", "A", "B"]
    modified = ["", " ", "B"]

    modifications, multi_site = mods_from_diff(original, modified)

    assert multi_site is False
    assert _apply_wire_operations(original, modifications) == modified
    assert all(
        modification["type"] == "delete" or modification["content"].strip()
        for modification in modifications
    )


def test_mods_from_diff_keeps_pure_delete_delete_only():
    original = ["package example;", "", "import old.Api;", "class Example {}"]
    modified = ["package example;", "", "class Example {}"]

    modifications, _ = mods_from_diff(original, modified)

    assert modifications == [{"type": "delete", "startLine": 3, "endLine": 3}]
    assert _apply_wire_operations(original, modifications) == modified


def test_mods_from_diff_roundtrips_compact_fixture_whitespace_shapes():
    cases = {
        "6682_license_package_boundary": (
            [" */", "", "package org.luna;", "import old.Api;"],
            [" */", "", "package org.luna;", "import new.Api;"],
        ),
        "6712_tab_blank_between_members": (
            ["class UpdateDividend {", "\t", "\tvoid main() {}", "}"],
            ["class UpdateDividend {", "", "\tvoid updateDividend() {}", "}"],
        ),
        "6742_tabbed_blank_before_constructor": (
            ["class Reranker {", "\t", "\tReranker() {}", "}"],
            ["class Reranker {", "", "\tReranker(int limit) {}", "}"],
        ),
    }
    for name, (original, modified) in cases.items():
        modifications, _ = mods_from_diff(original, modified)

        assert _apply_wire_operations(original, modifications) == modified, name


def test_unrepresentable_blank_diff_terminates_internal_without_verifier(monkeypatch):
    def verifier_must_not_run(*args, **kwargs):
        raise AssertionError("unrepresentable diff must not consume a verifier attempt")

    monkeypatch.setattr("deepagents_pipeline.harness.verify_candidate", verifier_must_not_run)
    request = _request(entireCode="A", issueLineNumber=1, contexts=[])
    outcome = run_fix_with_trace(
        request,
        _fake([_edit_msg(1, old="A", new=""), _submit_msg(1)]),
    )
    dumped = outcome.response.model_dump(by_alias=True)

    assert dumped["errorCode"] == "E999"
    assert dumped["errorStage"] == "output"
    assert outcome.terminal_cause == "internal_error"
    assert outcome.roundtrip_failures == 1
    assert outcome.attempts == []


def test_blank_hunk_widens_by_one_line_instead_of_absorbing_the_gap():
    """공백뿐인 조각은 인접 equal 줄 하나만 붙여 표현하고, 먼 hunk와 합치지 않는다.

    이전 구현은 표현 불가능한 공백 hunk를 갭과 무관하게 이전 hunk에 병합해서,
    멀리 떨어진 두 변경 사이의 무관한 줄을 전부 삭제 스팬에 흡수했다(MERGE_GAP
    무력화).
    """
    from deepagents_pipeline.harness import _roundtrip_ok

    orig = JAVA_LINES
    mod = orig[:4] + [GOOD_NEW] + orig[5:60] + ["      "] + orig[61:]
    mods, multi = mods_from_diff(orig, mod)

    assert _roundtrip_ok(orig, mods, mod)
    assert all(m["content"].strip() for m in mods if m["type"] == "add")
    assert multi is True
    assert len([m for m in mods if m["type"] == "add"]) == 2
    deleted = sum(m["endLine"] - m["startLine"] + 1 for m in mods if m["type"] == "delete")
    assert deleted == 3  # 실수정 1줄 + 공백 hunk 1줄 + 앞줄 1줄 확장


def test_blank_hunk_at_file_start_widens_forward():
    from deepagents_pipeline.harness import _roundtrip_ok

    orig = JAVA_LINES
    mod = ["   "] + orig[1:]
    mods, _ = mods_from_diff(orig, mod)

    assert _roundtrip_ok(orig, mods, mod)
    assert all(m["content"].strip() for m in mods if m["type"] == "add")


# --- 관측 config 관통 (langfuse 등, 2026-08-21) ---


def test_invoke_config_callbacks_are_wired():
    from langchain_core.callbacks import BaseCallbackHandler

    class _Counter(BaseCallbackHandler):
        def __init__(self):
            self.model_starts = 0

        def on_chat_model_start(self, *a, **k):
            self.model_starts += 1

    counter = _Counter()
    msgs = [_edit_msg(1), _submit_msg(1)]
    outcome = run_fix_with_trace(
        _request(), _fake(msgs), invoke_config={"callbacks": [counter]}
    )
    assert outcome.response.model_dump(by_alias=True)["isSuccess"] is True
    assert counter.model_starts >= 2  # edit·submit 턴이 콜백에 관측돼야 한다


def test_langfuse_disabled_by_default(monkeypatch):
    from deepagents_pipeline.runner import init_langfuse_handler

    monkeypatch.delenv("LANGFUSE_TRACING", raising=False)
    assert init_langfuse_handler() is None


# --- read_code: entireCode 문자열 페이징 도구 (Option A, 2026-08-19) ---


def _read_tool(code: str = ENTIRE_CODE):
    from deepagents_pipeline.harness import make_read_code_tool

    return make_read_code_tool(code)


def test_read_code_whole_with_explicit_limit():
    out = _read_tool().invoke({"offset": 0, "limit": 80})
    lines = out.split("\n")
    assert len(lines) == 80
    # 우측정렬 거터 + 2칸 구분 (deepagents format_content_with_line_numbers 계약 승계)
    assert lines[0] == " 1  public class Demo {"
    assert lines[79] == "80  }"
    assert "remaining" not in out


def test_read_code_default_limit_and_pagination_notice():
    big = "\n".join(f"line {i}" for i in range(1, 251))
    tool = _read_tool(big)
    out = tool.invoke({})
    assert out.count("\n\n") == 1  # 본문 + 안내 블록
    assert "[Read 100 lines (lines 1-100 of 250 total). 150 lines remaining from offset 100.]" in out
    # 안내된 offset에서 재개하면 정확히 다음 줄부터 읽힌다
    out2 = tool.invoke({"offset": 100, "limit": 200})
    assert out2.startswith("101  line 101")
    assert "remaining" not in out2


def test_read_code_negative_offset_clamped_with_notice():
    out = _read_tool().invoke({"offset": -5, "limit": 80})
    assert "public class Demo {" in out
    assert "[Requested offset -5 is before the start of the file; read from line 1 instead.]" in out


def test_read_code_offset_past_eof_errors():
    out = _read_tool().invoke({"offset": 999})
    assert out.startswith("Error:") and "999" in out


def test_read_code_trailing_newline_not_counted():
    out = _read_tool("a\nb\n").invoke({})
    assert out == "1  a\n2  b"


def test_read_code_caps_each_call_at_two_thousand_lines():
    code = "\n".join(f"line {i}" for i in range(1, 2502))

    out = _read_tool(code).invoke({"offset": 0, "limit": 2501})

    assert "2000  line 2000" in out
    assert "2001  line 2001" not in out
    assert "remaining from offset 2000" in out


def test_read_code_caps_each_call_at_fifty_kibibytes():
    code = "\n".join("x" * 1000 for _ in range(100))

    out = _read_tool(code).invoke({"offset": 0, "limit": 100})

    assert len(out.encode("utf-8")) <= 50 * 1024
    assert "remaining from offset" in out


def test_read_code_rejects_one_line_larger_than_fifty_kibibytes():
    out = _read_tool("x" * (50 * 1024 + 1)).invoke({"offset": 0, "limit": 1})

    assert out.startswith("Error:")
    assert "single line" in out
    assert "50KB" in out


def test_read_code_enforces_cumulative_output_budget():
    tool = _read_tool("\n".join("x" * 1000 for _ in range(100)))
    outputs: list[str] = []

    for _ in range(10):
        out = tool.invoke({"offset": 0, "limit": 100})
        if out.startswith("Error: cumulative read budget exhausted"):
            break
        outputs.append(out)
    else:
        raise AssertionError("read_code never exhausted its cumulative output budget")

    assert sum(len(out.encode("utf-8")) for out in outputs) <= 200 * 1024


def test_read_code_rejects_nonpositive_limit():
    tool = _read_tool("a\nb")

    assert tool.invoke({"limit": 0}) == "Error: limit must be a positive number of lines"
    assert tool.invoke({"limit": -1}) == "Error: limit must be a positive number of lines"


# --- HarnessControlMiddleware: 도구 배제 + 구조적 종료 ---


class _StubTool:
    def __init__(self, name: str):
        self.name = name


class _StubRequest:
    def __init__(self, tools, model_settings=None, tool_choice=None):
        self.tools = tools
        self.model_settings = model_settings
        self.tool_choice = tool_choice

    def override(self, *, tools, model_settings=None, tool_choice=None):
        return _StubRequest(tools, model_settings, tool_choice)





def _names_seen(mw, req):
    captured = {}

    def handler(r):
        captured["tools"] = [t.name for t in r.tools]
        return "resp"

    out = mw.wrap_model_call(req, handler)
    return out, captured.get("tools")


def test_build_agent_registers_only_control_middleware(monkeypatch):
    captured = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return "agent"

    monkeypatch.setattr("deepagents_pipeline.harness.create_agent", fake_create_agent)

    agent = build_agent(_fake([]), [])

    assert agent == "agent"
    assert len(captured["middleware"]) == 1
    assert isinstance(captured["middleware"][0], HarnessControlMiddleware)


def test_control_middleware_strips_denied_tools():
    mw = HarnessControlMiddleware()
    names = [
        "read_file", "ls", "edit_code", "submit_fix", "write_todos",
        "write_file", "execute", "task", "grep", "edit_file",
    ]
    out, seen = _names_seen(mw, _StubRequest([_StubTool(n) for n in names]))
    assert out == "resp"
    assert set(seen) == {"read_file", "ls", "edit_code", "submit_fix", "write_todos"}
    assert not set(seen) & DENIED_TOOLS


def test_control_middleware_disables_parallel_tool_calls():
    mw = HarnessControlMiddleware()
    request = _StubRequest([_StubTool("edit_code")], {"temperature": 0.2})
    captured = {}

    def handler(overridden):
        captured.update(overridden.model_settings)
        return "resp"

    out = mw.wrap_model_call(request, handler)

    assert out == "resp"
    assert captured == {"temperature": 0.2, "parallel_tool_calls": False}


def test_control_middleware_terminates_on_pass():
    holder = SubmissionHolder()
    holder.accepted = {"desc": KDESC}
    mw = HarnessControlMiddleware(holder)
    out, seen = _names_seen(mw, _StubRequest([_StubTool("read_file")]))
    assert isinstance(out, AIMessage) and not out.tool_calls
    assert seen is None  # 모델 호출 자체가 일어나지 않았다
    assert holder.terminal_cause == "pass"


def test_control_middleware_terminates_on_edit_exhaustion():
    holder = SubmissionHolder()
    holder.consecutive_edit_failures = MAX_CONSECUTIVE_EDIT_FAILURES
    mw = HarnessControlMiddleware(holder)
    out, seen = _names_seen(mw, _StubRequest([_StubTool("read_file")]))
    assert isinstance(out, AIMessage)
    assert seen is None
    assert holder.terminal_cause == "edit_exhausted"


def test_control_middleware_terminates_on_deadline():
    holder = SubmissionHolder()
    mw = HarnessControlMiddleware(holder, deadline=0.0)  # 이미 지난 데드라인
    out, seen = _names_seen(mw, _StubRequest([_StubTool("read_file")]))
    assert isinstance(out, AIMessage)
    assert seen is None
    assert holder.terminal_cause == "wall_clock"


def test_multiple_tool_calls_terminate_before_tool_dispatch():
    response = AIMessage(
        "",
        tool_calls=[
            _tc("edit_code", {"old_string": GOOD_OLD, "new_string": GOOD_NEW}, "edit"),
            _tc("submit_fix", {"desc": KDESC}, "submit"),
        ],
    )
    outcome = run_fix_with_trace(_request(), _fake([response]))
    dumped = outcome.response.model_dump(by_alias=True)

    assert dumped["errorCode"] == "E301"
    assert dumped["errorStage"] == "select"
    assert outcome.terminal_cause == "protocol_error"
    assert outcome.model_turns == 1
    assert outcome.edit_calls == 0
    assert outcome.attempts == []
    assert outcome.nudges == 0


def test_control_middleware_rejects_two_calls_split_across_response_messages():
    holder = SubmissionHolder()
    middleware = HarnessControlMiddleware(holder)
    response = ModelResponse(result=[_edit_msg(1), _submit_msg(1)])

    def handler(_request):
        return response

    output = middleware.wrap_model_call(
        _StubRequest([_StubTool("edit_code"), _StubTool("submit_fix")]), handler
    )

    assert isinstance(output, AIMessage)
    assert output.additional_kwargs["harness_terminated"] == "protocol_error"
    assert holder.terminal_cause == "protocol_error"


def test_control_middleware_rejects_two_invalid_tool_call_attempts():
    holder = SubmissionHolder()
    middleware = HarnessControlMiddleware(holder)
    invalid = {
        "type": "invalid_tool_call",
        "id": "broken",
        "name": "edit_code",
        "args": "{",
        "error": "malformed JSON",
    }
    response = ModelResponse(result=[AIMessage("", invalid_tool_calls=[invalid, invalid])])

    output = middleware.wrap_model_call(
        _StubRequest([_StubTool("edit_code")]), lambda _request: response
    )

    assert isinstance(output, AIMessage)
    assert holder.terminal_cause == "protocol_error"


def test_valid_and_invalid_tool_call_terminate_before_valid_edit_dispatch():
    invalid = {
        "type": "invalid_tool_call",
        "id": "broken",
        "name": "submit_fix",
        "args": "{",
        "error": "malformed JSON",
    }
    response = AIMessage(
        "",
        tool_calls=[_tc("edit_code", {"old_string": GOOD_OLD, "new_string": GOOD_NEW}, "edit")],
        invalid_tool_calls=[invalid],
    )

    outcome = run_fix_with_trace(_request(), _fake([response, _submit_msg(1)]))

    assert outcome.terminal_cause == "protocol_error"
    assert outcome.edit_calls == 0
    assert outcome.attempts == []


def _persistent_debug_main_deletion_cases() -> list[tuple[int, list[str], str, list[str]]]:
    source_path = (
        Path(__file__).parents[1]
        / "artifacts/260831_persistent_failure_analysis/input_normalized.jsonl"
    )
    ranges = {6682: (42, 147), 6712: (9, 46), 6742: (230, 329)}
    records = {
        record["requestIdx"]: record
        for record in (json.loads(line) for line in source_path.read_text(encoding="utf-8").splitlines())
        if record["requestIdx"] in ranges
    }
    cases = []
    for request_idx, (start_line, end_line) in ranges.items():
        source = records[request_idx]["userRequest"]["entireCode"].split("\n")
        old_string = "\n".join(source[start_line - 1 : end_line])
        expected = source[: start_line - 1] + source[end_line:]
        cases.append((request_idx, source, old_string, expected))
    return cases


def test_real_persistent_main_deletions_emit_no_blank_add_and_apply_exactly():
    for request_idx, source, old_string, expected in _persistent_debug_main_deletion_cases():
        holder = SubmissionHolder()
        _, edit_code, submit_fix = make_fix_tools(source, holder)

        assert edit_code.invoke({"old_string": old_string, "new_string": ""}).startswith("OK")
        submit_fix.invoke({"desc": BAD_DESC})

        modifications = holder.candidates[0]["codeModifications"]
        assert all(
            modification["type"] != "add" or modification["content"].strip()
            for modification in modifications
        ), request_idx
        assert _apply_wire_operations(source, modifications) == expected, request_idx


def test_line_aligned_whitespace_replacement_deletes_complete_lines_at_boundaries():
    cases = [
        (["main", "after"], "main", ["after"]),
        (["before", "main", "after"], "main", ["before", "after"]),
        (["before", "main"], "main", ["before"]),
    ]
    for source, old_string, expected in cases:
        holder = SubmissionHolder()
        _, edit_code, submit_fix = make_fix_tools(source, holder)

        assert edit_code.invoke({"old_string": old_string, "new_string": " \t"}).startswith("OK")
        submit_fix.invoke({"desc": BAD_DESC})

        modifications = holder.candidates[0]["codeModifications"]
        assert _apply_wire_operations(source, modifications) == expected


def test_non_line_aligned_whitespace_replacement_remains_literal():
    source = ["before main after"]
    holder = SubmissionHolder()
    _, edit_code, submit_fix = make_fix_tools(source, holder)

    assert edit_code.invoke({"old_string": "main", "new_string": " \t"}).startswith("OK")
    submit_fix.invoke({"desc": BAD_DESC})

    modifications = holder.candidates[0]["codeModifications"]
    assert _apply_wire_operations(source, modifications) == ["before  \t after"]


# --- 도구 호출 없는 턴 재촉 (2026-08-27, 27b agent_stop 8건) --------------------------


def test_no_tool_call_turn_is_nudged_then_submit_passes():
    # 편집을 올려놓고 빈 응답으로 끝내던 4505/5814/5815 유형: 재촉 1회 뒤 submit_fix로 PASS.
    msgs = [_edit_msg(1), AIMessage(""), _submit_msg(1)]
    outcome = run_fix_with_trace(_request(), _fake(msgs))
    assert outcome.response.model_dump(by_alias=True)["isSuccess"] is True
    assert outcome.terminal_cause == "pass"
    assert outcome.nudges == 1
    assert outcome.model_turns == 3
    assert outcome.trace_dict()["nudges"] == 1


def test_leaked_template_tokens_are_nudged_like_empty_text():
    # 4505: 텍스트로 새어 나온 "<tool_call>\n<|im_start|><think>" — tool_calls는 비어 있다.
    msgs = [_edit_msg(1), AIMessage("\n<tool_call>\n<|im_start|><think>"), _submit_msg(1)]
    outcome = run_fix_with_trace(_request(), _fake(msgs))
    assert outcome.terminal_cause == "pass"
    assert outcome.nudges == 1


def test_nudges_are_capped_then_agent_stop():
    # 재촉 상한 뒤에도 도구 호출이 없으면 기존처럼 agent_stop — 무한 루프 없음.
    # 같은 AIMessage 객체를 반복하면 add_messages가 id로 병합하므로 별개 객체로 만든다.
    msgs = [_edit_msg(1)] + [AIMessage("") for _ in range(MAX_NO_TOOL_NUDGES + 1)] + [_submit_msg(99)]
    outcome = run_fix_with_trace(_request(), _fake(msgs))
    dumped = outcome.response.model_dump(by_alias=True)
    assert dumped["isSuccess"] is False
    assert outcome.terminal_cause == "agent_stop"
    assert outcome.nudges == MAX_NO_TOOL_NUDGES
    # 편집 1 + 빈 응답 (상한+1) — 그 뒤 submit은 소비되지 않는다
    assert outcome.model_turns == 1 + MAX_NO_TOOL_NUDGES + 1


def test_no_nudge_without_edits_still_asks_to_edit_first():
    # 편집 없이 빈 응답 → 재촉 문구는 edit_code부터 하라고 안내하고, 이어지는 편집+제출로 PASS.
    msgs = [AIMessage(""), _edit_msg(1), _submit_msg(1)]
    outcome = run_fix_with_trace(_request(), _fake(msgs))
    assert outcome.terminal_cause == "pass"
    assert outcome.nudges == 1


def test_synthetic_termination_is_not_nudged():
    # 예산 소진 시 하네스가 만드는 합성 종료 AIMessage(tool_calls 없음)는 재촉 대상이 아니다.
    msgs = [_edit_msg(1)] + [_submit_msg(i, desc="english only desc") for i in range(1, 4)]
    msgs += [AIMessage("잔여")]
    outcome = run_fix_with_trace(_request(), _fake(msgs))
    assert outcome.terminal_cause == "budget_exhausted"
    assert outcome.nudges == 0


def test_recursion_limit_allows_exactly_max_model_turns():
    # 2026-08-27 회귀: after_model 훅이 턴당 그래프 스텝을 2→3으로 늘려 recursion_limit=50이
    # 17턴에서 끊겼다(27b·9b 전수 실행 recursion_limit 전부 model_turns=17). 턴 상한은
    # 그래프 구조와 무관하게 MAX_MODEL_TURNS여야 한다.
    msgs = [
        AIMessage("", tool_calls=[_tc("read_code", {"offset": 0, "limit": 3}, f"r{i}")])
        for i in range(MAX_MODEL_TURNS + 10)
    ]
    outcome = run_fix_with_trace(_request(), _fake(msgs))
    assert outcome.terminal_cause == "recursion_limit"
    assert outcome.model_turns == MAX_MODEL_TURNS
    assert outcome.nudges == 0
    dumped = outcome.response.model_dump(by_alias=True)
    assert dumped["errorNode"] == "fix_branch"
    assert dumped["errorType"] == "GraphRecursionError"
