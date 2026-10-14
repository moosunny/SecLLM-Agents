"""프롬프트 회귀 가드 — 스모크에서 관측된 실패 모드의 예방 문구가 유지되는지."""

from deepagents_pipeline.prompts import SYSTEM_PROMPT, InitialTask, build_initial_message


def _task(**overrides) -> InitialTask:
    fields = {
        "issue_line_number": 5,
        "total_lines": 80,
        "meta_json": '{"issueLineNumber": 5}',
        "trace_json": '[{"lineNo": 5, "eventType": "sink"}]',
        "target_chunks": "lines 1-9\n```java\n1  class A {}\n```",
        "checker_description": "Do not store a connection in a static field.",
        "dangerous_example": "private static Connection conn;",
        "safe_example": "private Connection conn;",
    }
    fields.update(overrides)
    return InitialTask(**fields)


def test_language_is_metadata_not_hardcoded():
    """언어는 입력 메타데이터에서 오고 프롬프트에 고정돼 있지 않다 (2026-08-25)."""
    assert "Java SAST" not in SYSTEM_PROMPT
    assert "programmingLanguage" in SYSTEM_PROMPT

    rendered = build_initial_message(_task(language="python"))
    assert "```python\nprivate static Connection conn;\n```" in rendered
    assert "```java" not in rendered.split("## Dangerous example")[1]


def test_example_fence_is_untagged_without_language():
    rendered = build_initial_message(_task())
    assert "## Dangerous example\n```\nprivate static Connection conn;\n```" in rendered


def test_edit_tool_rules_are_explicit():
    # 2026-08-10 A/B: qwen3.5-9b의 주 실패는 old_string 정확 재현 실패(연속 8~9회).
    # verbatim 복사·줄번호 접두어 제거·유일 매칭 규칙이 프롬프트에 명시돼야 한다.
    # 2026-10-14: 접두어 형식(`<line>: <content>`)과 들여쓰기 보존을 예시와 함께 명시.
    assert "edit_code" in SYSTEM_PROMPT
    assert "VERBATIM" in SYSTEM_PROMPT
    assert "`<line>: <content>`" in SYSTEM_PROMPT
    assert "such as `1: `" in SYSTEM_PROMPT
    assert "Never include the prefix in `old_string` or `new_string`" in SYSTEM_PROMPT
    assert "exactly ONE location" in SYSTEM_PROMPT


def test_korean_desc_rule_present():
    # desc는 submit_fix 인자로 전달되며 한국어 1문장 규칙이 유지돼야 한다.
    assert "ONE Korean" in SYSTEM_PROMPT
    assert "300" in SYSTEM_PROMPT


def test_pagination_rules_still_present():
    # read_code 기본 100줄 절단(코퍼스 45%가 초과) 대비 규칙이 유지돼야 한다.
    assert "ALWAYS pass `limit`" in SYSTEM_PROMPT
    assert "0-based" in SYSTEM_PROMPT
    assert "read_code" in SYSTEM_PROMPT
    # Option A(2026-08-19) 이후 경로 기반 참조가 남아 있으면 안 된다.
    assert "/input/" not in SYSTEM_PROMPT
    assert "read_file" not in SYSTEM_PROMPT


def test_system_prompt_describes_read_code_as_working_copy():
    # read_code가 원본이 아니라 작업본을 보여주므로 프롬프트도 그렇게 말해야 한다.
    assert "CURRENT working copy" in SYSTEM_PROMPT
    assert "read-only view of the ORIGINAL" not in SYSTEM_PROMPT


def test_system_prompt_drops_localized_and_source_code_slots():
    # En/Ko 병기와 sourceCode 발췌는 타겟 청크 기반 계약으로 대체됐다.
    for retired in ("issueNameKo/En", "issueDescriptionKo/En", "safeExampleKo/En", "sourceCode"):
        assert retired not in SYSTEM_PROMPT


def test_pass_is_final_and_review_contract_is_retired():
    # 2026-08-22: 리뷰 단계 제거 — verifier PASS가 유일 게이트다. 리뷰 지시가
    # 남아 있으면 모델이 존재하지 않는 도구를 찾는다.
    assert "PASS` is FINAL" in SYSTEM_PROMPT
    assert "review_fix" not in SYSTEM_PROMPT
    assert "REVIEW turn" not in SYSTEM_PROMPT
    assert "REVIEW_APPROVED" not in SYSTEM_PROMPT


def test_initial_message_carries_every_section():
    msg = build_initial_message(_task())

    assert "80 lines total" in msg
    for heading in (
        "## Finding metadata",
        "## Checker description",
        "## SAST trace",
        "## Target chunks",
        "## Dangerous example",
        "## Safe example",
    ):
        assert heading in msg
    assert '{"issueLineNumber": 5}' in msg
    assert '"eventType": "sink"' in msg
    assert "1  class A {}" in msg


def test_initial_message_omits_empty_sections():
    msg = build_initial_message(
        _task(checker_description="", dangerous_example="", safe_example="")
    )

    assert "## Checker description" not in msg
    assert "## Dangerous example" not in msg
    assert "## Safe example" not in msg
    assert "## Target chunks" in msg
    assert "\n\n\n" not in msg
