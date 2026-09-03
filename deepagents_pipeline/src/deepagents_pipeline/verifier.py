"""VerifierTool — `check_candidate` 래핑 적용-후-검증 (설계 §6).

Bash 배제 원칙에 따라 서브프로세스가 아닌 라이브러리 직접 호출이다.
검증 파이프라인 자체는 재구현하지 않고 `evals/subagent/verify_fix.py`의
`check_candidate`를 그대로 사용하며(§12(b) 계측 통일), 이 모듈은
문자열 오류를 {code, stage, message} 구조로 분류하고 적용 영역 에코를
render_regions 스타일로 첨부하는 어댑터다.

출력 계약 (§6):
    {passed: true,  errors: [], regions: "...", attempt: N}
  | {passed: false, errors: [{code, stage, message}], regions: "...", attempt: N}

regions는 패치 적용 단계까지 도달했을 때만 채워지며(새 좌표 ±3줄),
스키마·그룹핑 조기 실패에서는 빈 문자열이다.

V401(문법)만은 message 안에 파스 에러 지점(최대 3곳: 줄 범위, 누락 토큰 또는
ERROR 첫 토큰, 해당 줄 에코)과 지점 주변 ±3줄 문맥을 덧붙인다 — 계약 필드는
그대로이고 message만 길어진다. 자세한 근거는 `_collect_syntax_sites` 위 주석.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import cache
from typing import Any, TypedDict

from deepagents_pipeline.repo_paths import ensure_repo_paths


class VerifierError(TypedDict):
    code: str
    stage: str
    message: str


class VerifierResult(TypedDict):
    passed: bool
    errors: list[VerifierError]
    regions: str
    attempt: int


# check_candidate가 반환하는 문자열 오류의 부분 문자열 → (code, stage).
# 원문: guards.validate_candidate / verify_fix._group_ops / check_candidate.
_STAGE_PATTERNS: list[tuple[str, tuple[str, str]]] = [
    # schema (guards.validate_candidate)
    ("desc is empty", ("V101", "schema")),
    ("char limit", ("V102", "schema")),
    ("modifications is empty", ("V103", "schema")),
    ("unsupported type", ("V104", "schema")),
    ("is missing startLine", ("V105", "schema")),
    ("is missing endLine", ("V107", "schema")),
    # endLine 범위 오류를 startLine 범위 오류와 분리 (§12(b) 사유 히스토그램)
    ("endLine", ("V112", "schema")),
    ("is outside the allowed range", ("V106", "schema")),
    ("is missing content", ("V108", "schema")),
    ("line-number prefix", ("V109", "schema")),
    # structure (_group_ops 순서/중첩 + multi-site 정책)
    ("ordered ascending and non-overlapping", ("V201", "structure")),
    ('"multi_site"', ("V202", "structure")),
    ("lines apart", ("V203", "structure")),
    # apply-guard (no-op / 삭제 상한) — 연산 단위와 파일 전체 no-op을 구분
    ("is a no-op", ("V301", "apply-guard")),
    ("identical to the original", ("V303", "apply-guard")),
    ("net deletion", ("V302", "apply-guard")),
    # syntax (tree-sitter java 차등 검사)
    ("introduces syntax errors", ("V401", "syntax")),
]

_UNKNOWN = ("V999", "unknown")


def classify_error(message: str) -> VerifierError:
    for needle, (code, stage) in _STAGE_PATTERNS:
        if needle in message:
            return VerifierError(code=code, stage=stage, message=message)
    code, stage = _UNKNOWN
    return VerifierError(code=code, stage=stage, message=message)


@cache
def _verify_fix_module() -> Any:
    ensure_repo_paths()
    import verify_fix

    return verify_fix


# --- V401 위치·문맥 보강 --------------------------------------------------------
# check_candidate는 파스 에러 '개수' 차이만 보고하므로 모델이 어디를 고칠지 모른 채
# 같은 V401로 반복 실패하는 회복 불능 루프가 관측됐다(2026-08-10 스모크, qwen3.5-9b
# idx 2). 2026-08-26 재구성(39개 V401 시도) 결과 위치만으로는 부족했다: missing 노드는
# 기대 토큰(';', '}', 'identifier')이 버려졌고, 24~36줄짜리 try 블록 ERROR는 끝부분
# 문맥이 없어 catch 누락이 보이지 않았다. 메인 repo verify_fix는 다른 arm의 채점기라
# 변경하지 않고 이 어댑터에서만 보강한다. 부가 정보이므로 실패해도 판정은 바꾸지
# 않으며, 분류(classify_error)는 보강 전 원문 기준으로 끝나 있다.

_MAX_SITES = 3
_SITE_CONTEXT = 3
_MAX_CONTEXT_LINES = 40
_TOKEN_TEXT_LIMIT = 20
_ECHO_LIMIT = 60
_LOCATION_HEADER = "Parse-error location(s) in the MODIFIED file (new coordinates): "
_CONTEXT_HEADER = "Parse-error context (new coordinates):"


@dataclass(frozen=True)
class _SyntaxSite:
    """파스 에러 한 곳 (1-based, inclusive 줄 범위)."""

    start_line: int
    end_line: int
    kind: str  # "missing" | "error"
    token: str  # missing: 파서가 기대한 토큰 타입 / error: ERROR 노드 첫 토큰의 소스 텍스트


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_line_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip().split("\n", 1)[0].strip()
    return _truncate(text, _TOKEN_TEXT_LIMIT)


def _collect_syntax_sites(
    vf: Any, text: str, language: str, file_extension: str
) -> list[_SyntaxSite]:
    """텍스트를 판정과 같은 문법(TSX 구분 포함)으로 파싱해 에러 지점을 줄번호순으로 모은다.

    verify_fix._syntax_issues(verify_fix.py:139-163)와 같은 노드 집합(ERROR ∪
    is_missing)과 같은 파서 부트스트랩을 쓰되, missing 노드의 ``node.type``(기대
    토큰)과 ERROR 노드 첫 토큰의 소스 텍스트를 보존한다. ERROR는 문법 내부 타입명
    ('formal_parameters' 등)이 아니라 소스 텍스트를 쓴다 — 타입명은 모델에게 의미가
    없다. 같은 줄이면 missing을 ERROR보다 앞에 두고, 정렬 키는 token까지 포함해
    완전 순서로 만든다(집합 순회 순서에 기대면 실행마다 다른 site가 살아남는다).
    """
    import tree_sitter

    grammar = vf._load_language(language, file_extension)
    try:
        parser = tree_sitter.Parser(grammar)
    except TypeError:  # older tree_sitter API — verify_fix.py:143-147 미러
        parser = tree_sitter.Parser()
        parser.set_language(grammar)
    source = text.encode("utf-8", errors="replace")
    tree = parser.parse(source)

    sites: list[_SyntaxSite] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        if node.is_missing:
            sites.append(_SyntaxSite(start, end, "missing", node.type))
        elif node.type == "ERROR":
            head = next(
                (c for c in node.children if c.end_byte > c.start_byte), node
            )
            token = _first_line_text(source[head.start_byte : head.end_byte])
            sites.append(_SyntaxSite(start, end, "error", token))
        stack.extend(node.children)
    # 같은 줄에 같은 기대 토큰이 여러 번 찍히는 경우(중괄호 불균형 뒤 연쇄)가 흔하다 —
    # 동일 site는 한 번만 보여 (+N more) 카운트가 실제 서로 다른 지점 수가 되게 한다.
    unique = dict.fromkeys(sites)
    return sorted(
        unique,
        key=lambda s: (s.start_line, s.end_line, s.kind != "missing", s.token),
    )


def _site_signature(site: _SyntaxSite, lines: list[str]) -> tuple[str, str, str]:
    """좌표 없이 원본/수정본 site를 대응시키는 키 (종류, 토큰, 시작 줄 텍스트)."""
    line_no = min(max(site.start_line, 1), len(lines))
    return (site.kind, site.token, lines[line_no - 1].strip() if lines else "")


def _prioritize_sites(
    sites: list[_SyntaxSite],
    new_lines: list[str],
    op_spans: list[list[int]],
    original_sites: list[_SyntaxSite] | None,
    original_lines: list[str],
) -> list[_SyntaxSite]:
    """편집이 만든 에러가 앞에 오도록 고른다.

    원본에 이미 파스 에러가 있으면 줄번호순 상위 3곳이 전부 편집과 무관한 자리일 수
    있다(리뷰 지적, 2026-08-26). 원본과 수정본은 좌표계가 달라 직접 차집합이 어려우므로
    (종류, 토큰, 줄 텍스트) 서명이 원본에 같은 개수로 존재하는 site를 먼저 제외하고,
    남은 것 중 적용 영역(op span)과 겹치는 site를 앞세운 뒤 줄번호순으로 둔다.
    제외가 전부를 지우면(서명 충돌) 제외 전 목록으로 되돌린다.
    """
    candidates = sites
    if original_sites:
        budget = Counter(_site_signature(s, original_lines) for s in original_sites)
        remaining: list[_SyntaxSite] = []
        for site in sites:
            signature = _site_signature(site, new_lines)
            if budget.get(signature, 0) > 0:
                budget[signature] -= 1
                continue
            remaining.append(site)
        if remaining:
            candidates = remaining

    def touches_edit(site: _SyntaxSite) -> bool:
        return any(
            s != 0 and site.start_line <= e and site.end_line >= s
            for s, e in op_spans
        )

    return sorted(
        candidates,
        key=lambda s: (
            not touches_edit(s),
            s.start_line,
            s.end_line,
            s.kind != "missing",
            s.token,
        ),
    )


def _format_site(site: _SyntaxSite, new_lines: list[str]) -> str:
    span = (
        f"L{site.start_line}"
        if site.end_line == site.start_line
        else f"L{site.start_line}-{site.end_line}"
    )
    echo = ""
    if new_lines:
        line_no = min(max(site.start_line, 1), len(new_lines))
        echo = _truncate(new_lines[line_no - 1].strip(), _ECHO_LIMIT)
    if site.kind == "missing":
        return f"{span} missing {site.token!r}: {echo!r}"
    return f"{span} ERROR starting at {site.token!r}: {echo!r}"


def _render_syntax_context(
    sites: list[_SyntaxSite],
    new_lines: list[str],
    op_spans: list[list[int]],
    region_context: int = 3,
) -> str:
    """각 에러 지점의 ±_SITE_CONTEXT줄을 렌더한다 (새 좌표).

    긴 ERROR 노드(예: catch 없는 24줄 try)는 시작과 끝을 별도 블록으로 낸다 — 고쳐야
    할 자리는 대개 끝(catch/finally가 와야 할 곳)이다. 겹치는 블록은 병합하고, 적용
    영역 에코(op span ±region_context, verify_fix.render_regions가 이미 출력)에
    완전히 들어가는 블록은 생략해 중복을 피한다. 총 줄수는 _MAX_CONTEXT_LINES로
    자르되, 끝 블록이 잘릴 때는 뒷부분을 남긴다.
    """
    total = len(new_lines)
    if total == 0:
        return ""
    covered = [
        (max(1, s - region_context), min(total, e + region_context))
        for s, e in op_spans
        if s != 0
    ]

    labeled: list[tuple[int, int, str]] = []
    for n, site in enumerate(sites, start=1):
        if site.end_line - site.start_line <= _SITE_CONTEXT:
            ranges = [(site.start_line, site.end_line, f"{n}")]
        else:
            ranges = [
                (site.start_line, site.start_line, f"{n}"),
                (site.end_line, site.end_line, f"{n} (end)"),
            ]
        for lo, hi, label in ranges:
            lo = max(1, lo - _SITE_CONTEXT)
            hi = min(total, hi + _SITE_CONTEXT)
            if any(clo <= lo and hi <= chi for clo, chi in covered):
                continue
            labeled.append((lo, hi, label))

    labeled.sort()
    merged: list[tuple[int, int, list[str]]] = []
    for lo, hi, label in labeled:
        if merged and lo <= merged[-1][1] + 1:
            prev_lo, prev_hi, labels = merged[-1]
            merged[-1] = (prev_lo, max(prev_hi, hi), labels + [label])
        else:
            merged.append((lo, hi, [label]))

    out: list[str] = []
    budget = _MAX_CONTEXT_LINES
    truncated = False
    for lo, hi, labels in merged:
        if budget <= 0:
            truncated = True
            break
        if hi - lo + 1 > budget:
            truncated = True
            if any(label.endswith("(end)") for label in labels):
                lo = hi - budget + 1
            else:
                hi = lo + budget - 1
        out.append(f"--- parse error {', '.join(labels)}: lines {lo}-{hi} ---")
        out.extend(f"{i:5d}. {new_lines[i - 1]}" for i in range(lo, hi + 1))
        budget -= hi - lo + 1
    if truncated:
        out.append("(context truncated)")
    return "\n".join(out)


def _attach_syntax_locations(
    vf: Any,
    errors: list[VerifierError],
    result: dict,
    code_lines: list[str],
    file_extension: str,
) -> None:
    """V401 메시지에 수정본의 파스 에러 위치(최대 _MAX_SITES곳)와 문맥을 첨부한다.

    ``file_extension``은 판정(check_candidate)이 쓴 값을 그대로 받아 같은 문법으로
    파싱한다 — 이전에는 '.java'가 하드코딩돼 .tsx 레코드에서 판정과 위치가 어긋났다.
    원본에 파스 에러가 있을 때(syntax_before > 0)만 원본도 파싱해 편집 무관 site를
    걸러낸다. 보강 전체가 try 안이라 어떤 실패도 판정과 원문 메시지를 건드리지 않는다.
    """
    if result["new_lines"] is None or not any(e["code"] == "V401" for e in errors):
        return
    new_lines: list[str] = result["new_lines"]
    language = result["language"]
    try:
        sites = _collect_syntax_sites(vf, "\n".join(new_lines), language, file_extension)
        if not sites:
            return
        original_sites = None
        if result.get("syntax_before"):
            original_sites = _collect_syntax_sites(
                vf, "\n".join(code_lines), language, file_extension
            )
        ordered = _prioritize_sites(
            sites, new_lines, result["spans"], original_sites, code_lines
        )
        shown = ordered[:_MAX_SITES]
        hidden = len(ordered) - len(shown)
        more = f" (+{hidden} more distinct site(s))" if hidden else ""
        suffix = (
            " " + _LOCATION_HEADER
            + "; ".join(_format_site(site, new_lines) for site in shown)
            + more
            + "."
        )
        context = _render_syntax_context(
            shown,
            new_lines,
            result["spans"],
            region_context=getattr(vf, "_RENDER_CONTEXT", 3),
        )
        if context:
            suffix += "\n" + _CONTEXT_HEADER + "\n" + context
    except Exception:  # noqa: BLE001 — 위치 보강 실패가 검증 자체를 죽이면 안 된다
        return
    for e in errors:
        if e["code"] == "V401":
            e["message"] += suffix


def verify_candidate(
    code_lines: list[str],
    candidate: dict,
    *,
    attempt: int,
    language: str,
    file_extension: str,
) -> VerifierResult:
    """후보 1건을 적용-후-검증하고 구조화 결과를 반환한다 (§6(c)).

    `language`는 chunking.resolve_language의 정규화 이름이어야 한다. 기본값을 두지
    않는 이유는 java로 조용히 폴백하면 다른 언어 레코드가 java 파서로 검사돼
    V401이 의미를 잃기 때문이다(2026-08-25). `file_extension`은 typescript에서
    .tsx/.ts 문법을 가르는 데 쓰이므로 원본 요청 값을 그대로 넘긴다.
    """
    vf = _verify_fix_module()
    # multi-site 정책(V202/V203)은 끈다: harness.MERGE_GAP을 5로 낮추면서 5~30줄
    # 간격의 hunk가 분리 출고되는데, 레거시 규칙은 30줄 미만 간격 연산을 "하나로
    # 합쳐라"라며 거부한다(실측 26건). check_candidate 독스트링이 명시하듯
    # Active Suggestion의 공개 계약은 인접한 분리 원자 편집을 허용한다.
    result = vf.check_candidate(
        code_lines,
        candidate,
        language=language,
        file_extension=file_extension,
        enforce_multi_site_policy=False,
    )

    regions = ""
    if result["new_lines"] is not None:
        regions = vf.render_regions(result["new_lines"], result["spans"])

    errors = [classify_error(msg) for msg in result["errors"]]
    _attach_syntax_locations(vf, errors, result, code_lines, file_extension)
    return VerifierResult(
        passed=not errors,
        errors=errors,
        regions=regions,
        attempt=attempt,
    )
