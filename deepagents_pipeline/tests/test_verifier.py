"""G002 VerifierTool 단위 테스트 (LLM-free) — 설계 §6 출력 계약."""

import ast
import re
import warnings

import pytest

from deepagents_pipeline.verifier import (
    _MAX_CONTEXT_LINES,
    _collect_syntax_sites,
    _prioritize_sites,
    _render_syntax_context,
    _SyntaxSite,
    _verify_fix_module,
    verify_candidate,
)

# 80줄 java: multi-site 갭(>=30줄) 테스트가 가능하도록 충분히 길게.
JAVA_LINES = (
    ["public class Demo {"]
    + [f"    int f{i}() {{ return {i}; }}" for i in range(1, 79)]
    + ["}"]
)

DESC = "정수 반환값을 상수로 교체"


def _replace(line: int, content: str) -> list[dict]:
    return [
        {"type": "delete", "startLine": line, "endLine": line},
        {"type": "add", "startLine": line, "content": content},
    ]


PYTHON_LINES = [
    "def total(values):",
    "    result = 0",
    "    for value in values:",
    "        result += value",
    "    return result",
]


def test_syntax_check_uses_the_given_language():
    """언어별 파서로 문법을 본다 — java 고정이었다면 성립하지 않는다 (2026-08-25)."""
    broken = {
        "desc": "합계 계산을 수정",
        "codeModifications": [
            {"type": "delete", "startLine": 2, "endLine": 2},
            {"type": "add", "startLine": 2, "content": "    result = "},
        ],
        "multi_site": False,
    }
    result = verify_candidate(
        PYTHON_LINES, broken, attempt=1, language="python", file_extension=".py"
    )
    assert result["passed"] is False
    assert any(e["code"] == "V401" for e in result["errors"])


def test_valid_python_edit_passes():
    candidate = {
        "desc": "초기값을 명시적으로 지정",
        "codeModifications": [
            {"type": "delete", "startLine": 2, "endLine": 2},
            {"type": "add", "startLine": 2, "content": "    result = int(0)"},
        ],
        "multi_site": False,
    }
    result = verify_candidate(
        PYTHON_LINES, candidate, attempt=1, language="python", file_extension=".py"
    )
    assert result["passed"] is True, result["errors"]


@pytest.mark.parametrize("original,modified", [
    ("x = 1\ny = 2", "x = 1\n    y = 2"),
    ("if True:\n    x = 1\ny = 2", "if True:\nx = 1\ny = 2"),
    ("if True:\n    x = 1\n    y = 2", "if True:\n    x = 1\n  y = 2"),
])
def test_python_ast_rejects_indentation_regression(original, modified):
    # These regressions have no tree-sitter ERROR or missing nodes.
    vf = _verify_fix_module()
    assert vf._syntax_issues(modified, "python", ".py") == []
    with pytest.raises(SyntaxError) as failure:
        ast.parse(modified)
    candidate = {
        "desc": DESC,
        "codeModifications": [
            {"type": "delete", "startLine": 1, "endLine": len(original.split("\n"))},
            {"type": "add", "startLine": 1, "content": modified},
        ],
    }
    result = verify_candidate(
        original.split("\n"), candidate, attempt=2, language="python", file_extension=".py"
    )
    assert result["passed"] is False
    assert result["attempt"] == 2
    assert result["regions"]
    error, = result["errors"]
    assert (error["code"], error["stage"]) == ("V401", "syntax")
    assert f"L{failure.value.lineno}: {failure.value.msg}" in error["message"]
    assert "new coordinates" in error["message"]


@pytest.mark.parametrize("original", [
    'print "hello"\nx = 1',  # Python 2
    "    x = 0\nx = 1",  # Already-invalid Python 3
])
@pytest.mark.parametrize("replacement,passed", [("x = 2", True), ("x =", False)])
def test_unparseable_python_original_preserves_existing_verdict(original, replacement, passed):
    candidate = {"desc": DESC, "codeModifications": _replace(2, replacement)}
    result = verify_candidate(
        original.split("\n"), candidate, attempt=1, language="python", file_extension=".py"
    )
    assert result["passed"] is passed
    assert not any("Python AST" in e["message"] for e in result["errors"])
    if not passed:
        assert any(e["code"] == "V401" for e in result["errors"])


def test_python_ast_suppresses_syntax_warnings():
    original = ['value = "\\q"', 'other = "\\z"']
    candidate = {"desc": DESC, "codeModifications": _replace(2, 'other = "\\y"')}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", SyntaxWarning)
        result = verify_candidate(
            original, candidate, attempt=1, language="python", file_extension=".py"
        )
    assert result["passed"] is True, result["errors"]
    assert not [w for w in caught if issubclass(w.category, SyntaxWarning)]


def test_non_python_does_not_run_ast_parse(monkeypatch):
    def unexpected_parse(*args, **kwargs):
        raise AssertionError("Python AST parsing must only run for Python")

    monkeypatch.setattr("deepagents_pipeline.verifier.ast.parse", unexpected_parse)
    result = verify_candidate(
        JAVA_LINES, {"desc": DESC, "codeModifications": _replace(5, "    int f4() { return 400; }")},
        attempt=1, language="java", file_extension=".java",
    )
    assert result["passed"] is True, result["errors"]


def test_pass_single_replacement():
    candidate = {
        "desc": DESC,
        "codeModifications": _replace(5, "    int f4() { return 400; }"),
        "multi_site": False,
    }
    result = verify_candidate(JAVA_LINES, candidate, attempt=1, language="java", file_extension=".java")
    assert result["passed"] is True
    assert result["errors"] == []
    assert result["attempt"] == 1
    assert "operation 1" in result["regions"]
    assert "return 400" in result["regions"]


def test_schema_fail_no_regions():
    candidate = {
        "desc": "",
        "codeModifications": [{"type": "replace", "startLine": 5}],
        "multi_site": False,
    }
    result = verify_candidate(JAVA_LINES, candidate, attempt=2, language="java", file_extension=".java")
    assert result["passed"] is False
    assert result["regions"] == ""
    stages = {e["stage"] for e in result["errors"]}
    assert stages == {"schema"}
    assert result["attempt"] == 2


def test_nearby_separated_edits_pass():
    """갭<30 분리 편집은 통과한다 — MERGE_GAP=5 전환으로 정상 출고 형태가 됐다.

    레거시 규칙(V203)은 30줄 미만 간격 연산을 "하나로 합쳐라"라며 거부했고,
    harness는 그걸 피하려 diff 단계에서 미리 병합해 사이의 미변경 줄까지 삭제
    스팬에 흡수했다. verify_candidate는 이제 enforce_multi_site_policy=False로
    호출하므로 이 형태가 계약상 유효하다.
    """
    candidate = {
        "desc": DESC,
        "codeModifications": _replace(5, "    int f4() { return 400; }")
        + _replace(10, "    int f9() { return 900; }"),
        "multi_site": True,
    }
    result = verify_candidate(JAVA_LINES, candidate, attempt=1, language="java", file_extension=".java")
    assert result["passed"] is True, result["errors"]
    assert not any(e["code"] in ("V202", "V203") for e in result["errors"])


def test_multi_site_pass_with_flag():
    candidate = {
        "desc": DESC,
        "codeModifications": _replace(5, "    int f4() { return 400; }")
        + _replace(60, "    int f59() { return 5900; }"),
        "multi_site": True,
    }
    result = verify_candidate(JAVA_LINES, candidate, attempt=1, language="java", file_extension=".java")
    assert result["passed"] is True, result["errors"]
    assert "operation 2" in result["regions"]


def test_multi_site_flag_no_longer_gates():
    """multi_site 플래그 불일치는 더 이상 거부 사유가 아니다 (V202 비활성).

    플래그는 harness가 diff에서 계산하는 내부 필드이고 응답 계약에도 노출되지
    않는다. 정책을 끈 뒤에는 값이 어긋나도 적용·문법 검증 결과만으로 판정한다.
    """
    candidate = {
        "desc": DESC,
        "codeModifications": _replace(5, "    int f4() { return 400; }")
        + _replace(60, "    int f59() { return 5900; }"),
        "multi_site": False,
    }
    result = verify_candidate(JAVA_LINES, candidate, attempt=1, language="java", file_extension=".java")
    assert result["passed"] is True, result["errors"]
    assert not any(e["code"] == "V202" for e in result["errors"])


def test_apply_guard_noop():
    candidate = {
        "desc": DESC,
        "codeModifications": _replace(5, JAVA_LINES[4]),
        "multi_site": False,
    }
    result = verify_candidate(JAVA_LINES, candidate, attempt=3, language="java", file_extension=".java")
    assert result["passed"] is False
    assert any(e["stage"] == "apply-guard" for e in result["errors"])


def test_syntax_regression_with_regions_echo():
    candidate = {
        "desc": DESC,
        "codeModifications": _replace(5, "    int broken(( {"),
        "multi_site": False,
    }
    result = verify_candidate(JAVA_LINES, candidate, attempt=1, language="java", file_extension=".java")
    assert result["passed"] is False
    assert any(e["code"] == "V401" and e["stage"] == "syntax" for e in result["errors"])
    # 적용 단계까지 도달했으므로 새 좌표 영역 에코가 있어야 한다 (§6)
    assert "broken" in result["regions"]


def test_syntax_error_message_includes_location():
    # V401 개수만 보고하면 모델이 같은 오류로 반복 실패한다(2026-08-10 스모크) —
    # 새 좌표 위치와 해당 줄 에코가 메시지에 첨부돼야 한다.
    candidate = {
        "desc": DESC,
        "codeModifications": _replace(5, "    int broken(( {"),
        "multi_site": False,
    }
    result = verify_candidate(JAVA_LINES, candidate, attempt=1, language="java", file_extension=".java")
    msg = next(e["message"] for e in result["errors"] if e["code"] == "V401")
    assert "new coordinates" in msg
    assert re.search(r"L\d+", msg), msg
    assert "broken" in msg  # 파스 에러 지점의 실제 줄 텍스트 에코


# --- V401 위치·문맥 보강 (2026-08-26, 39개 V401 시도 재구성 기반) ---------------------

def _v401_message(result: dict) -> str:
    return next(e["message"] for e in result["errors"] if e["code"] == "V401")


def test_missing_node_reports_expected_token():
    # 재구성에서 가장 흔한 회복 실패: 'missing' 이라고만 나오고 무엇이 빠졌는지 없었다.
    candidate = {
        "desc": DESC,
        "codeModifications": _replace(5, "    int f5() { return 5 }"),
        "multi_site": False,
    }
    result = verify_candidate(JAVA_LINES, candidate, attempt=1, language="java", file_extension=".java")
    msg = _v401_message(result)
    assert "missing ';'" in msg, msg
    assert "'missing'" not in msg  # 옛 형식("(missing)")의 정보 손실이 재발하지 않는다


TRY_LINES = [
    "public class Demo {",
    "    void run() {",
    "        try {",
    "            a();",
    "            b();",
    "            c();",
    "            d();",
    "            e();",
    "        } catch (Exception ex) {",
    "            f();",
    "        }",
    "    }",
    "}",
]


def test_error_node_reports_first_token_text_not_grammar_type():
    # catch를 지워 다줄 try 전체가 ERROR가 되는 idx57 유형. 첫 토큰은 소스 텍스트('try')로
    # 나와야 하고 문법 내부 타입명(block, formal_parameters 등)이 새면 안 된다.
    candidate = {
        "desc": DESC,
        "codeModifications": [
            {"type": "delete", "startLine": 9, "endLine": 11},
            {"type": "add", "startLine": 9, "content": "        }"},
        ],
        "multi_site": False,
    }
    result = verify_candidate(TRY_LINES, candidate, attempt=1, language="java", file_extension=".java")
    msg = _v401_message(result)
    assert "ERROR starting at 'try'" in msg, msg
    assert re.search(r"L3-\d+ ERROR", msg), msg
    assert "formal_parameters" not in msg and "'block'" not in msg
    # 시작 문맥 블록은 적용 영역(op span ±3) 밖이므로 렌더된다
    assert "Parse-error context (new coordinates):" in msg
    assert "--- parse error 1: lines 1-6 ---" in msg, msg


def test_sites_are_listed_in_ascending_line_order():
    # DFS 스택 순서(파일 뒤쪽 우선) 대신 줄번호순: 연쇄 오류의 근원은 앞쪽이다.
    candidate = {
        "desc": DESC,
        "codeModifications": _replace(5, "    int f5() { return 5 }")
        + _replace(40, "    int f40() { return 40 }"),
        "multi_site": True,
    }
    result = verify_candidate(JAVA_LINES, candidate, attempt=1, language="java", file_extension=".java")
    msg = _v401_message(result)
    listed = msg.split("Parse-error context", 1)[0]
    lines = [int(m) for m in re.findall(r"L(\d+)", listed)]
    assert lines == sorted(lines) and lines[0] == 5, msg


def test_location_parse_uses_the_given_extension():
    # 판정은 .tsx 문법으로 하면서 위치만 .java 하드코딩으로 파싱하던 불일치 회귀 방지.
    vf = _verify_fix_module()
    jsx = "const x = <div>hi</div>;"
    assert _collect_syntax_sites(vf, jsx, "typescript", ".tsx") == []
    assert _collect_syntax_sites(vf, jsx, "typescript", ".ts") != []


def test_long_error_span_renders_start_and_end_blocks():
    lines = [f"line {i}" for i in range(1, 41)]
    sites = [_SyntaxSite(3, 20, "error", "try")]
    out = _render_syntax_context(sites, lines, op_spans=[[35, 35]])
    assert "--- parse error 1: lines 1-6 ---" in out
    assert "--- parse error 1 (end): lines 17-23 ---" in out
    assert "   17. line 17" in out


def test_context_block_inside_applied_region_is_skipped():
    # 적용 영역 에코(op span ±3)가 이미 보여주는 줄은 중복 렌더하지 않는다.
    lines = [f"line {i}" for i in range(1, 41)]
    sites = [_SyntaxSite(10, 10, "missing", "}")]
    assert _render_syntax_context(sites, lines, op_spans=[[9, 11]]) == ""
    assert _render_syntax_context(sites, lines, op_spans=[[30, 31]]) != ""


def test_context_total_is_capped():
    lines = [f"line {i}" for i in range(1, 201)]
    sites = [
        _SyntaxSite(10, 40, "error", "try"),
        _SyntaxSite(80, 110, "error", "try"),
        _SyntaxSite(150, 180, "error", "try"),
    ]
    out = _render_syntax_context(sites, lines, op_spans=[[195, 195]])
    rendered = [l for l in out.split("\n") if re.match(r"\s+\d+\. ", l)]
    assert len(rendered) <= _MAX_CONTEXT_LINES
    assert "(context truncated)" in out


def test_location_enrichment_failure_keeps_the_verdict(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr("deepagents_pipeline.verifier._collect_syntax_sites", boom)
    candidate = {
        "desc": DESC,
        "codeModifications": _replace(5, "    int broken(( {"),
        "multi_site": False,
    }
    result = verify_candidate(JAVA_LINES, candidate, attempt=1, language="java", file_extension=".java")
    assert result["passed"] is False
    msg = _v401_message(result)
    assert msg.startswith("the edit introduces syntax errors")
    assert "Parse-error location" not in msg


def test_same_line_sites_have_deterministic_order():
    # 정렬 키가 token까지 포함해야 집합 순회 순서(해시 시드)에 따라 살아남는 site가
    # 달라지지 않는다.
    lines = ["x"]
    sites = [_SyntaxSite(1, 1, "missing", t) for t in ("}", ";", ")")]
    ordered = _prioritize_sites(sites, lines, [[5, 5]], None, lines)
    assert [s.token for s in ordered] == [")", ";", "}"]


PRE_BROKEN_LINES = [
    "public class Demo {",
    "    void a() { @@@ }",
    "    void b() { ### }",
    "    void c() { $$$ }",
    "    int f() { return 1; }",
    "    int g() { return 2; }",
    "}",
]


def test_preexisting_errors_do_not_hide_the_introduced_one():
    # 원본에 파스 에러가 3곳 이상 있으면 줄번호순 상위 3곳이 전부 편집과 무관해진다
    # (리뷰 지적). 편집이 만든 site가 첫 번째로, 원본 site는 제외돼야 한다.
    candidate = {
        "desc": DESC,
        "codeModifications": _replace(6, "    int g() { return 2 }"),
        "multi_site": False,
    }
    result = verify_candidate(
        PRE_BROKEN_LINES, candidate, attempt=1, language="java", file_extension=".java"
    )
    msg = _v401_message(result)
    listed = msg.split("Parse-error context", 1)[0]
    assert "L6 missing ';'" in listed, msg
    assert "L2 " not in listed and "L3 " not in listed and "L4 " not in listed, msg


def test_enrichment_failure_after_parse_keeps_the_verdict(monkeypatch):
    # fail-open은 수집뿐 아니라 포맷/렌더 실패도 덮어야 한다.
    def boom(*_args, **_kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr("deepagents_pipeline.verifier._render_syntax_context", boom)
    candidate = {
        "desc": DESC,
        "codeModifications": _replace(5, "    int broken(( {"),
        "multi_site": False,
    }
    result = verify_candidate(JAVA_LINES, candidate, attempt=1, language="java", file_extension=".java")
    assert result["passed"] is False
    msg = _v401_message(result)
    assert msg.startswith("the edit introduces syntax errors")
    assert "Parse-error location" not in msg


def test_truncated_end_block_keeps_its_tail():
    lines = [f"line {i}" for i in range(1, 201)]
    # 6블록 × 7줄 = 42 > 40: 마지막 (end) 블록이 5줄로 잘린다
    sites = [
        _SyntaxSite(5, 60, "error", "try"),
        _SyntaxSite(80, 130, "error", "try"),
        _SyntaxSite(150, 190, "error", "try"),
    ]
    out = _render_syntax_context(sites, lines, op_spans=[[195, 195]])
    assert "(context truncated)" in out
    assert "  193. line 193" in out  # 마지막 (end) 블록의 뒷부분이 살아남는다
