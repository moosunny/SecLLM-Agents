"""완전형 하네스 — edit_code 인터페이스 (설계 §9의 개정, 2026-08-10).

2026-08-19: deepagents(create_deep_agent) → langchain.agents.create_agent 전환.
같은 날 Option A 개정(사용자 결정): 경로 기반 read_file(/input/*)을
`read_code(offset, limit)`로 교체하고 meta는 초기 태스크 메시지에 JSON 인라인.
출력 형식(0-based offset, 기본 limit 100, 우측정렬 거터+2칸 구분, 남은 줄
안내)은 deepagents 0.7.4 read_file 시맨틱을 그대로 잇는다. 경로 인자·not-found
실패 표면은 소멸. 이 개정 이전의 경로 기반 계측과는 궤적 비교 불가 (새 arm).

- `read_code`가 요청 스키마의 entireCode 문자열을 페이징 노출 (읽기 전용)
- 도구셋: `read_code` + `edit_code`/`submit_fix` + planning todo
- 편집 인터페이스: 설계 §9의 write_file(candidate.json) 대신
  `edit_code(old_string, new_string)`가 작업본에 편집을 누적하고,
  `submit_fix(desc)`가 원본과의 diff에서 codeModifications를 후처리로 생성한다
  (2026-08-10 A/B: deepseek in −10%/out −32%/wall −65%, qwen 4/5→3/5 악화 —
  악화 리스크 고지 후 사용자 결정으로 기본값 교체). 30줄 미만 갭 hunk는 하나의
  연속 치환으로 병합돼 V203이 구조적으로 발생하지 않는다.
- create_agent는 넘긴 도구만 등록하므로 `execute`/`task`/`write_file`은 구조적으로
  부재. HarnessControlMiddleware의 DENIED_TOOLS 필터는 방어층으로 유지 (이중 배제)
- `submit_fix`가 VerifierTool(§6)을 반드시 호출한다 — 검증 생략 불가
- 검증 PASS 후보는 pending으로 보관하고 모델의 정확한 REVIEW_APPROVED 응답 뒤에만
  성공으로 승격한다. 예산 소진/연속 편집 실패/wall-clock 초과는 미들웨어가 종료한다
- 기준선에서 Bash가 하던 역할: 시각기록→하네스, verifier 실행→submit_fix 내부,
  part 작성→하네스의 envelope 조립 (§5.2)
"""

from __future__ import annotations

import difflib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError

from deepagents_pipeline.chunking import (
    CodeLanguageHint,
    SourceChunk,
    build_source_chunks,
    find_chunk_for_line,
    resolve_language,
)
from deepagents_pipeline.contracts import (
    ERROR_INTERNAL,
    ERROR_MODEL_CONNECT,
    ERROR_UNSUPPORTED_INPUT,
    ERROR_VERIFY_EXHAUSTED,
    AgentModuleErrorResponse,
    AgentModuleRequest,
    AgentModuleResponse,
    AgentUserRequest,
    build_error_response,
    build_success_response,
)
from deepagents_pipeline.prompts import (
    SYSTEM_PROMPT,
    InitialTask,
    build_initial_message,
)
from deepagents_pipeline.replacers import find_unique_match
from deepagents_pipeline.verifier import VerifierError, VerifierResult, verify_candidate

MAX_FIX_ATTEMPTS = 3
MAX_DESC_LENGTH = 300
# §6(b): 재시도 예산과 별개의 전체 상한 (모든 arm 동일 적용).
# 모델 턴 상한 25. create_agent 그래프는 턴당 model→after_model→tools 3스텝을 쓴다
# (2026-08-27 after_model 넛지 훅 추가로 2→3; 50 고정이던 recursion_limit이 17턴에서
# 끊기는 회귀가 전수 실행에서 발견됨). 훅을 추가·제거하면 이 값을 같이 맞춘다.
MAX_MODEL_TURNS = 25
GRAPH_STEPS_PER_TURN = 3
MAX_AGENT_STEPS = MAX_MODEL_TURNS * GRAPH_STEPS_PER_TURN
MAX_WALL_CLOCK_SECONDS = 300.0  # 개별 LLM 호출 timeout(90s)과 별개의 레코드당 데드라인
# 2026-08-10 A/B에서 관측된 갭: 편집 매칭 실패는 검증 예산을 안 쓰므로 상한이
# 없으면 recursion_limit까지 실패를 반복한다(qwen idx 2·4, 8~9연속). 연속 실패
# 캡이 그 나선을 구조적으로 끊는다. 성공 시 카운터는 리셋된다.
MAX_CONSECUTIVE_EDIT_FAILURES = 5
# 도구 호출 없는 턴에 대한 재촉 상한 (2026-08-27). 27b train 6057건에서 agent_stop 8건은
# 전부 '작업본에 편집을 올린 뒤 tool_calls 없는 응답(빈 텍스트, 새어 나온 <tool_call>
# 템플릿 토큰, 퇴화 출력)'으로 create_agent 루프가 그 자리에서 끝난 것이었다 — 4건은
# 체커 표준 수정이 이미 적용된 상태. 검증 시도가 남아 있으면 최대 2회 submit_fix를
# 재촉하고, 그래도 도구 호출이 없으면 기존처럼 agent_stop으로 끝낸다.
MAX_NO_TOOL_NUDGES = 2
NUDGE_MARKER = "harness_nudge"
# 이 미만 갭의 hunk는 병합. verify_fix.MIN_MULTI_SITE_GAP(30)에 맞추던 값을 5로
# 낮췄다 (2026-08-25): 30줄 병합은 hunk 사이의 미변경 줄까지 delete+add 스팬에
# 흡수해 315건 중 64건에서 555줄을 "수정과 무관한 삭제"로 표기했다(실제 소실은
# 없으나 적용 시 충돌 면적과 과잉삭제 오독을 키운다). 실측된 hunk 간격은 0-5줄이
# 전체 병합 대상의 70%라 5에서도 인접 파편 결합이라는 원 취지는 유지되고 잔여
# 부풀림은 130줄로 줄어든다. 대신 5~30줄 간격 hunk가 분리 출고되므로 검증은
# enforce_multi_site_policy=False 경로를 쓴다 (verifier.verify_candidate 참조).
MERGE_GAP = 5

_HANGUL = re.compile(r"[가-힣]")
# 식별자·구두점 런 — 한국어 desc 판정 전에 제거한다 (Java 식별자는 desc에 불가피)
_IDENT_RUN = re.compile(r"[A-Za-z0-9_]+|[.()\[\]<>:;,\-/=\"'`]+")

DENIED_TOOLS = frozenset({"execute", "task", "write_file", "edit_file", "delete", "glob", "grep"})

# deepagents 0.7.4 read_file 시맨틱 재현 상수 (filesystem.py:889, backends/utils.py:74)
DEFAULT_READ_LIMIT = 100
MAX_READ_LINES = 2000
MAX_READ_BYTES = 50 * 1024
MAX_CUMULATIVE_READ_BYTES = 200 * 1024
MAX_LINE_LENGTH = 5000

TerminalCause = Literal[
    "pass",
    "budget_exhausted",
    "edit_exhausted",
    "wall_clock",
    "recursion_limit",
    "agent_stop",
    "connection_error",
    "internal_error",
    "unsupported_input",
    "protocol_error",
]


@dataclass(frozen=True, slots=True)
class DiffConversion:
    modifications: list[dict]
    multi_site: bool


@dataclass(frozen=True, slots=True)
class DiffConversionFailure:
    message: str


type DiffConversionOutcome = DiffConversion | DiffConversionFailure


@dataclass(frozen=True, slots=True)
class DiffConversionError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass
class SubmissionHolder:
    """제출·검증 이력 수집 (기존 FixSubmissionHolder 대응, fix_loop.py:52).

    §12(b) 계측(턴·토큰)도 여기에 누적한다. 그래프 최종 state가 아니라 모델 호출
    시점에 기록하므로, 예외(E202/E999)·recursion_limit로 invoke가 중단돼도
    그때까지 실제로 지출한 토큰이 보존된다 (비용 지표가 실패 레코드를 0으로
    세지 않도록 — §12(c) 우월성 게이트 2·3이 총 output 토큰과 지연을 본다).
    """

    attempts: list[VerifierResult] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    accepted: dict | None = None
    terminal_cause: TerminalCause | None = None
    model_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # edit_code 계측 — 매칭 실패는 검증 예산 미소모라 별도 축으로 기록한다.
    edit_calls: int = 0
    edit_errors: int = 0
    consecutive_edit_failures: int = 0
    edit_failure_kinds: list[str] = field(default_factory=list)
    roundtrip_failures: int = 0
    internal_error_message: str | None = None
    # 폴백 매칭 계측 (2026-08-20 replacer 체인 도입) — A/B 귀속용
    fuzzy_matches: int = 0
    fuzzy_kinds: list[str] = field(default_factory=list)
    reindented_edits: int = 0
    # 도구 호출 없는 턴 재촉 횟수 (agent_stop 회수 계측)
    nudges: int = 0

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def passed(self) -> bool:
        return self.accepted is not None

    def record_usage(self, response: Any) -> None:
        """ModelResponse(result: list[BaseMessage])에서 실제 모델 턴·토큰을 누적."""
        for msg in getattr(response, "result", None) or []:
            if not isinstance(msg, AIMessage):
                continue
            self.model_turns += 1
            usage = getattr(msg, "usage_metadata", None) or {}
            self.input_tokens += usage.get("input_tokens", 0)
            self.output_tokens += usage.get("output_tokens", 0)


class HarnessControlMiddleware(AgentMiddleware):
    """도구 배제 + 구조적 종료 (2026-08-22: 리뷰 단계 제거, PASS 즉시 승격)."""

    def __init__(
        self,
        holder: SubmissionHolder | None = None,
        *,
        deadline: float | None = None,
        denied: frozenset[str] = DENIED_TOOLS,
    ) -> None:
        super().__init__()
        self.holder = holder
        self.deadline = deadline
        self.denied = denied

    @staticmethod
    def _terminate(cause: TerminalCause, text: str) -> AIMessage:
        # 합성 종료 메시지 — model_turns 집계에서 제외할 수 있도록 마킹 (§12(b))
        return AIMessage(content=text, additional_kwargs={"harness_terminated": cause})

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        if self.holder is not None:
            if self.holder.terminal_cause == "internal_error":
                return self._terminate(
                    "internal_error", "Internal harness failure; loop terminated by harness."
                )
            if self.holder.passed:
                self.holder.terminal_cause = "pass"
                return self._terminate("pass", "Verified fix accepted; loop terminated by harness.")
            if self.holder.attempt_count >= MAX_FIX_ATTEMPTS:
                self.holder.terminal_cause = "budget_exhausted"
                return self._terminate(
                    "budget_exhausted", "Verification budget exhausted; loop terminated by harness."
                )
            if self.holder.consecutive_edit_failures >= MAX_CONSECUTIVE_EDIT_FAILURES:
                self.holder.terminal_cause = "edit_exhausted"
                return self._terminate(
                    "edit_exhausted",
                    "Too many consecutive failed edit_code matches; loop terminated by harness.",
                )
        if self.deadline is not None and time.monotonic() > self.deadline:
            if self.holder is not None:
                self.holder.terminal_cause = "wall_clock"
            return self._terminate(
                "wall_clock", "Wall-clock deadline exceeded; loop terminated by harness."
            )
        allowed = [t for t in request.tools if getattr(t, "name", None) not in self.denied]
        model_settings = {
            **(request.model_settings or {}),
            "parallel_tool_calls": False,
        }
        response = handler(
            request.override(tools=allowed, model_settings=model_settings)
        )
        if self.holder is not None:
            # 합성 종료 메시지는 handler를 거치지 않으므로 자동으로 집계에서 빠진다.
            self.holder.record_usage(response)
        tool_attempts = sum(
            len(message.tool_calls) + len(message.invalid_tool_calls)
            for message in getattr(response, "result", None) or []
            if isinstance(message, AIMessage)
        )
        if tool_attempts >= 2:
            if self.holder is not None:
                self.holder.terminal_cause = "protocol_error"
            return self._terminate(
                "protocol_error",
                "Multiple tool calls in one model response are a harness protocol error; "
                "loop terminated by harness.",
            )
        return response

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """도구 호출 없는 응답을 곧바로 종료로 받지 않고 submit_fix를 재촉한다.

        create_agent는 tool_calls가 빈 AIMessage를 최종 답변으로 보고 END로 간다.
        검증 시도가 남아 있고 제출도 없는 상태라면 그 응답은 답변이 아니라 이탈이므로,
        HumanMessage로 재촉하고 모델 노드로 되돌린다(jump_to). 하네스가 만든 합성
        종료 메시지·PASS·예산 소진·편집 실패 상한은 건드리지 않으며, 재촉은
        MAX_NO_TOOL_NUDGES회로 끝나 무한 루프가 되지 않는다.
        """
        del runtime
        holder = self.holder
        if holder is None:
            return None
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        if last.additional_kwargs.get("harness_terminated"):
            return None
        if (
            holder.passed
            or holder.attempt_count >= MAX_FIX_ATTEMPTS
            or holder.consecutive_edit_failures >= MAX_CONSECUTIVE_EDIT_FAILURES
            or holder.nudges >= MAX_NO_TOOL_NUDGES
        ):
            return None
        holder.nudges += 1
        applied = max(0, holder.edit_calls - holder.edit_errors)
        remaining = MAX_FIX_ATTEMPTS - holder.attempt_count
        if applied:
            situation = (
                f"The working copy has {applied} applied edit(s) that were never verified"
            )
        else:
            situation = "No edit has been applied to the working copy yet"
        text = (
            "You ended your turn without calling a tool, so nothing was submitted. "
            f"{situation}; {remaining} verification attempt(s) remain. "
            "Do not reply with text. Call submit_fix(desc=<ONE Korean sentence>) now"
            + (
                ", or edit_code first if a change is still needed."
                if applied
                else " after applying the fix with edit_code."
            )
        )
        return {
            "messages": [
                HumanMessage(content=text, additional_kwargs={NUDGE_MARKER: holder.nudges})
            ],
            "jump_to": "model",
        }


def _numbered(lines: list[str], start_line: int) -> str:
    """deepagents format_content_with_line_numbers 재현 — 우측정렬 거터 + 2칸 구분.

    5000자 초과 줄은 `N.1`, `N.2` 연속 마커로 분할된다 (Java 소스에선 사실상
    미발생이지만 형식 계약을 그대로 유지한다).
    """
    rows: list[tuple[str, str]] = []
    width = 0
    for i, line in enumerate(lines):
        chunks = [line[s : s + MAX_LINE_LENGTH] for s in range(0, len(line), MAX_LINE_LENGTH)] or [line]
        for ci, chunk in enumerate(chunks):
            marker = str(start_line + i) if ci == 0 else f"{start_line + i}.{ci}"
            rows.append((marker, chunk))
            width = max(width, len(marker))
    return "\n".join(f"{marker:>{width}}  {chunk}" for marker, chunk in rows)


def _make_read_code_tool(lines_provider: Callable[[], list[str]]):
    """현재 작업본을 페이징하는 read_code를 생성한다."""
    cumulative_output_bytes = 0

    @tool("read_code")
    def read_code(offset: int = 0, limit: int = DEFAULT_READ_LIMIT) -> str:
        """Read the current working copy. Returns content with 1-based line
        numbers in the gutter. `offset` is the 0-based line to start from;
        `limit` is capped at 2000 lines (default 100). Each response is capped
        at 50KB and this tool instance has a cumulative 200KB output budget.
        If the output says lines remain, continue from the reported offset."""
        nonlocal cumulative_output_bytes
        lines = lines_provider()
        if lines and lines[-1] == "":
            lines = lines[:-1]
        clamp_notice = ""
        if offset < 0:
            clamp_notice = (
                f"\n\n[Requested offset {offset} is before the start of the file; "
                "read from line 1 instead.]"
            )
            offset = 0
        total = len(lines)
        if offset >= total:
            return f"Error: offset {offset} exceeds the file length ({total} lines)"
        if limit <= 0:
            return "Error: limit must be a positive number of lines"

        if cumulative_output_bytes >= MAX_CUMULATIVE_READ_BYTES:
            return (
                "Error: cumulative read budget exhausted "
                f"({MAX_CUMULATIVE_READ_BYTES // 1024}KB)"
            )

        start_line = offset + 1
        first_line = _numbered([lines[offset]], start_line)
        if len(first_line.encode("utf-8")) > MAX_READ_BYTES:
            return (
                "Error: single line at offset "
                f"{offset} exceeds the {MAX_READ_BYTES // 1024}KB read limit"
            )

        requested_count = min(limit, MAX_READ_LINES, total - offset)
        limit_notice = (
            f"\n\n[Requested limit {limit} exceeds the maximum {MAX_READ_LINES}; "
            f"capped at {MAX_READ_LINES} lines.]"
            if limit > MAX_READ_LINES
            else ""
        )

        def render(line_count: int) -> str:
            end_line = offset + line_count
            body = _numbered(lines[offset:end_line], start_line)
            if end_line < total:
                remaining = total - end_line
                body += (
                    f"\n\n[Read {line_count} {'line' if line_count == 1 else 'lines'} "
                    f"(lines {start_line}-{end_line} of {total} total). "
                    f"{remaining} {'line' if remaining == 1 else 'lines'} remaining "
                    f"from offset {end_line}.]"
                )
            return body + limit_notice + clamp_notice

        response_budget = min(
            MAX_READ_BYTES,
            MAX_CUMULATIVE_READ_BYTES - cumulative_output_bytes,
        )
        low = 1
        high = requested_count
        accepted_count = 0
        accepted_response = ""
        while low <= high:
            midpoint = (low + high) // 2
            candidate_response = render(midpoint)
            if len(candidate_response.encode("utf-8")) <= response_budget:
                accepted_count = midpoint
                accepted_response = candidate_response
                low = midpoint + 1
            else:
                high = midpoint - 1

        if accepted_count == 0:
            return (
                "Error: cumulative read budget exhausted; "
                "the next complete source line cannot fit"
            )

        response_bytes = len(accepted_response.encode("utf-8"))
        cumulative_output_bytes += response_bytes
        return accepted_response

    return read_code


def make_read_code_tool(code_text: str):
    """Option A(2026-08-19) — 소스 문자열을 페이징하는 독립 read_code.

    경로 추상화 없이 소스 파일 하나만 노출한다. 페이지네이션 시맨틱은 deepagents
    0.7.4 read_file을 그대로 잇는다: 0-based offset, 기본 limit 100, 잘림 시
    남은 줄 안내(재개 offset 포함), 음수 offset은 0으로 클램프 후 고지한다.
    호출당 2000줄/50KB 및 도구 인스턴스당 누적 200KB를 강제한다.
    """
    lines = code_text.split("\n")
    return _make_read_code_tool(lambda: lines)


def _format_errors(errors: list[VerifierError]) -> str:
    return "\n".join(f"- [{e['stage']}/{e['code']}] {e['message']}" for e in errors)


def _validate_desc_before_verifier(desc: str) -> list[VerifierError]:
    if not desc.strip():
        return [
            VerifierError(code="V101", stage="schema", message="desc is empty.")
        ]
    if len(desc) > MAX_DESC_LENGTH:
        return [
            VerifierError(
                code="V102",
                stage="schema",
                message=(
                    f"desc has {len(desc)} chars, exceeding the "
                    f"{MAX_DESC_LENGTH} char limit."
                ),
            )
        ]
    prose = "".join(_IDENT_RUN.sub("", desc).split())
    hangul = len(_HANGUL.findall(prose))
    if not prose or hangul < 2 or hangul < 0.5 * len(prose):
        return [
            VerifierError(
                code="V110",
                stage="schema",
                message="desc must be one Korean sentence (<=300 chars, mostly Hangul).",
            )
        ]
    return []


def _blank_new_segment(mod: list[str], hunk: tuple[int, int, int, int]) -> bool:
    """hunk의 수정본 쪽 조각이 공백뿐이라 add 연산으로 표현할 수 없는가.

    guards.validate_candidate는 content가 공백뿐인 add를 V108로 거부한다. 넓게
    병합하던 시절에는 이런 hunk가 큰 블록에 묻혀 드러나지 않았으나, 갭이 좁아지면
    단독 연산으로 분리돼 계약 위반이 된다.
    """
    _, _, j1, j2 = hunk
    return j2 > j1 and not "\n".join(mod[j1:j2]).strip()


def _expand_blank_region(
    orig: list[str],
    mod: list[str],
    opcodes: list[tuple[str, int, int, int, int]],
    hunk: tuple[int, int, int, int],
) -> tuple[int, int, int, int] | None:
    i1, i2, j1, j2 = hunk
    while _blank_new_segment(mod, (i1, i2, j1, j2)):
        if i2 < len(orig) and j2 < len(mod) and orig[i2] == mod[j2]:
            i2, j2 = i2 + 1, j2 + 1
            continue
        next_opcode = next(
            (
                opcode
                for opcode in opcodes
                if opcode[1] == i2 and opcode[3] == j2 and opcode[0] != "equal"
            ),
            None,
        )
        if next_opcode is not None:
            _, _, i2, _, j2 = next_opcode
            continue
        if i1 > 0 and j1 > 0 and orig[i1 - 1] == mod[j1 - 1]:
            i1, j1 = i1 - 1, j1 - 1
            continue
        previous_opcode = next(
            (
                opcode
                for opcode in reversed(opcodes)
                if opcode[2] == i1 and opcode[4] == j1 and opcode[0] != "equal"
            ),
            None,
        )
        if previous_opcode is None:
            return None
        _, i1, _, j1, _ = previous_opcode
    return i1, i2, j1, j2


def _convert_diff(
    orig: list[str], mod: list[str], *, validate_roundtrip: bool = True
) -> DiffConversionOutcome:
    sm = difflib.SequenceMatcher(a=orig, b=mod, autojunk=False)
    opcodes = list(sm.get_opcodes())
    regions: list[tuple[int, int, int, int]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        hunk = (i1, i2, j1, j2)
        region = (
            _expand_blank_region(orig, mod, opcodes, hunk)
            if _blank_new_segment(mod, hunk)
            else hunk
        )
        if region is None:
            return DiffConversionFailure(
                "diff contains a blank-only replacement that cannot be represented "
                "by the public add/delete contract"
            )
        if regions and region[0] - regions[-1][1] < MERGE_GAP:
            previous = regions[-1]
            regions[-1] = (
                previous[0],
                max(previous[1], region[1]),
                previous[2],
                max(previous[3], region[3]),
            )
        else:
            regions.append(region)

    mods: list[dict] = []
    for i1, i2, j1, j2 in regions:
        if i2 > i1 and j2 > j1:  # 치환
            mods.append({"type": "delete", "startLine": i1 + 1, "endLine": i2})
            mods.append({"type": "add", "startLine": i1 + 1, "content": "\n".join(mod[j1:j2])})
        elif i2 > i1:  # 순수 삭제
            mods.append({"type": "delete", "startLine": i1 + 1, "endLine": i2})
        else:  # 순수 삽입 (원본 i1+1번 줄 앞)
            mods.append({"type": "add", "startLine": i1 + 1, "content": "\n".join(mod[j1:j2])})
    if any(modification["type"] == "add" and not modification["content"].strip() for modification in mods):
        return DiffConversionFailure("generated wire operations include a blank-only add")
    if validate_roundtrip and not _roundtrip_ok(orig, mods, mod):
        return DiffConversionFailure("generated wire operations did not round-trip")
    return DiffConversion(modifications=mods, multi_site=len(regions) > 1)


def mods_from_diff(orig: list[str], mod: list[str]) -> tuple[list[dict], bool]:
    conversion = _convert_diff(orig, mod)
    match conversion:
        case DiffConversion(modifications=modifications, multi_site=multi_site):
            return modifications, multi_site
        case DiffConversionFailure(message=message):
            raise DiffConversionError(message)


def _roundtrip_ok(orig: list[str], mods: list[dict], expect: list[str]) -> bool:
    """생성한 ops를 verify_fix의 _group_ops/_apply로 되적용해 작업본과 일치 확인."""
    from deepagents_pipeline.verifier import _verify_fix_module

    vf = _verify_fix_module()
    ops, errs = vf._group_ops(mods)
    if errs:
        return False
    new_lines, _ = vf._apply(orig, ops)
    return new_lines == expect


def _reindent_new_string(span: str, old_string: str, new_string: str) -> str:
    """fuzzy 매칭 시 new_string을 파일 실재 들여쓰기로 이동한다.

    기준은 첫 줄: 원문 span과 old_string의 leading whitespace 차이를, old_string의
    첫 줄 들여쓰기로 시작하는 new_string의 모든 줄에 동일하게 적용한다. 줄 간
    상대 구조는 모델이 쓴 그대로 유지된다. (c5 rerun2: 잔여 실패 6건 중 5건이
    미보정 삽입 → 들여쓰기 복구 나선으로 recursion_limit.)
    """

    def leading(line: str) -> str:
        return line[: len(line) - len(line.lstrip(" \t"))]

    span_ws = leading(span.split("\n", 1)[0])
    old_ws = leading(old_string.split("\n", 1)[0])
    if span_ws == old_ws:
        return new_string
    corrected: list[str] = []
    for line in new_string.split("\n"):
        if not line.strip():
            corrected.append(line)
        elif line.startswith(old_ws):
            corrected.append(span_ws + line[len(old_ws) :])
        else:
            corrected.append(line)
    return "\n".join(corrected)


def make_fix_tools(
    code_lines: list[str],
    holder: SubmissionHolder,
    *,
    language: str = "java",
    file_extension: str = ".java",
):
    """read_code/edit_code/submit_fix 도구 팩토리 — 공개 편집·검증 경계.

    세 도구는 같은 작업본을 공유한다. 편집 매칭 실패는 검증 예산을 소모하지 않는 대신
    연속 실패 카운터로 집계되고, HarnessControlMiddleware가
    MAX_CONSECUTIVE_EDIT_FAILURES에서 루프를 구조적으로 종료한다
    (2026-08-10 A/B에서 관측된 recursion_limit 나선 방지).

    language/file_extension은 submit_fix의 문법 검증(V401)이 쓸 파서를 고른다.
    """
    working = {"lines": list(code_lines)}
    read_code = _make_read_code_tool(lambda: working["lines"])

    def _region(start_1b: int, n_lines: int) -> str:
        lines = working["lines"]
        lo = max(1, start_1b - 3)
        hi = min(len(lines), start_1b + max(n_lines, 1) - 1 + 3)
        return "\n".join(f"{i:5d}. {lines[i - 1]}" for i in range(lo, hi + 1))

    def _edit_failure(kind: str, message: str) -> str:
        holder.edit_errors += 1
        holder.consecutive_edit_failures += 1
        holder.edit_failure_kinds.append(kind)
        return message

    @tool("edit_code")
    def edit_code(old_string: str, new_string: str) -> str:
        """Replace old_string with new_string in the working copy of the source file.
        old_string must match EXACTLY ONE location: copy it verbatim from read_code
        output WITHOUT the line-number gutter, and include neighboring lines to make
        it unique. Edits accumulate on the working copy; submit_fix diffs it against
        the original. Failed matches do not consume verification attempts."""
        holder.edit_calls += 1
        if holder.passed:
            return "Already PASSED. Do not edit further; finish now."
        if old_string == "":
            return _edit_failure(
                "empty", "ERROR: old_string is empty. Provide the exact text to replace."
            )
        if old_string == new_string:
            return _edit_failure(
                "noop",
                "ERROR: old_string and new_string are identical — this edit would "
                "change nothing. Provide a real change. No verification attempt "
                "was consumed.",
            )
        text = "\n".join(working["lines"])
        match, err = find_unique_match(text, old_string)
        if err == "not_found":
            first = old_string.split("\n", 1)[0][:80]
            return _edit_failure(
                "not_found",
                "ERROR: old_string not found in the file. Check exact whitespace and "
                f"do NOT include line-number prefixes. First line searched: {first!r}. "
                "No verification attempt was consumed.",
            )
        if err == "ambiguous":
            return _edit_failure(
                "ambiguous",
                "ERROR: old_string matches more than one location; include more "
                "surrounding lines so it matches exactly once. No verification "
                "attempt was consumed.",
            )
        if err == "disproportionate":
            return _edit_failure(
                "disproportionate",
                "ERROR: the closest match is much larger than your old_string. "
                "Re-read the region and provide the full exact text to replace. "
                "No verification attempt was consumed.",
            )
        new_text = new_string
        if match.replacer != "simple":
            new_text = _reindent_new_string(match.span, old_string, new_string)
        if match.span == new_text:
            # fuzzy 매칭이 잡은 실제 원문이 new_string과 동일 — 파일은 이미 원하는
            # 상태다. OK로 통과시키면 어떤 카운터에도 안 잡히는 no-op 나선이 된다.
            return _edit_failure(
                "noop",
                "ERROR: the matched text is already identical to new_string — the "
                "file is already in the desired state at that location. Re-read the "
                "region before editing again. No verification attempt was consumed.",
            )
        holder.consecutive_edit_failures = 0
        fuzzy_note = ""
        if match.replacer != "simple":
            holder.fuzzy_matches += 1
            holder.fuzzy_kinds.append(match.replacer)
            reindent_note = ""
            if new_text != new_string:
                holder.reindented_edits += 1
                reindent_note = (
                    " new_string was re-indented to the file's actual leading "
                    "whitespace before insertion."
                )
            fuzzy_note = (
                f"\nNote: old_string did not match exactly; it was matched via "
                f"'{match.replacer}' normalization.{reindent_note} Check the region "
                "above to confirm the edit landed at the intended site."
            )
        pos = match.index
        replacement_end = pos + len(match.span)
        line_aligned = (pos == 0 or text[pos - 1] == "\n") and (
            replacement_end == len(text) or text[replacement_end] == "\n"
        )
        if not new_text.strip() and line_aligned:
            new_text = ""
            if replacement_end < len(text):
                replacement_end += 1
            elif pos > 0:
                pos -= 1
        start_line = text[:pos].count("\n") + 1
        working["lines"] = (
            text[:pos] + new_text + text[replacement_end:]
        ).split("\n")
        return (
            "OK. Working copy updated. Affected region (current coordinates):\n"
            f"{_region(start_line, len(new_text.split(chr(10))))}\n"
            "Apply more edits if needed, then call submit_fix(desc=...)."
            f"{fuzzy_note}"
        )

    @tool("submit_fix")
    def submit_fix(desc: str) -> str:
        """Diff the working copy against the original file, build the structured fix,
        and run the deterministic verifier in-process. `desc` must be ONE Korean
        sentence (<=300 chars) describing the fix. Returns PASS, or FAIL with
        structured errors plus the affected region of the modified file in NEW
        coordinates. Maximum 3 verification attempts per finding."""
        if holder.passed:
            return "Already PASSED. Do not submit again; finish now."
        if holder.attempt_count >= MAX_FIX_ATTEMPTS:
            return (
                f"BUDGET EXHAUSTED: all {MAX_FIX_ATTEMPTS} verification attempts are used. "
                "Stop working; the harness will emit the error response."
            )
        if working["lines"] == code_lines:
            # 편집 없는 제출은 제출이 아니다 — 시도 예산 미소모.
            return (
                "ERROR: no edits applied yet — the working copy is identical to the "
                "original. Use edit_code first. No verification attempt was consumed."
            )

        desc_errors = _validate_desc_before_verifier(desc)
        conversion = _convert_diff(
            code_lines,
            working["lines"],
            validate_roundtrip=not desc_errors,
        )
        match conversion:
            case DiffConversion(modifications=mods, multi_site=multi):
                pass
            case DiffConversionFailure(message=message):
                holder.roundtrip_failures += 1
                holder.internal_error_message = message
                holder.terminal_cause = "internal_error"
                return "INTERNAL: diff conversion cannot be represented; record will terminate."
        attempt = holder.attempt_count + 1
        candidate = {"desc": desc, "codeModifications": mods, "multi_site": multi}
        if desc_errors:
            result = VerifierResult(
                passed=False,
                errors=desc_errors,
                regions="",
                attempt=attempt,
            )
        else:
            result = verify_candidate(
                code_lines,
                candidate,
                attempt=attempt,
                language=language,
                file_extension=file_extension,
            )

        holder.attempts.append(result)
        holder.candidates.append(candidate)

        if result["passed"]:
            holder.accepted = candidate
            return (
                f"PASS (attempt {attempt}/{MAX_FIX_ATTEMPTS}). Affected region of the modified "
                f"file (new coordinates):\n{result['regions']}\n"
                "The candidate is accepted and the fix is complete. Stop now."
            )

        remaining = MAX_FIX_ATTEMPTS - holder.attempt_count
        regions_part = f"\nApplied region (new coordinates):\n{result['regions']}" if result["regions"] else ""
        closing = (
            "Revise with further edit_code calls and call submit_fix again "
            f"({remaining} attempt(s) left)."
            if remaining > 0
            else "No attempts left. Stop working; the harness will emit the error response."
        )
        return (
            f"FAIL (attempt {attempt}/{MAX_FIX_ATTEMPTS}):\n"
            f"{_format_errors(result['errors'])}{regions_part}\n{closing}"
        )

    return read_code, edit_code, submit_fix


def build_agent(
    model: BaseChatModel,
    tools: list[Any],
    *,
    holder: SubmissionHolder | None = None,
    deadline: float | None = None,
):
    return create_agent(
        model=model,
        tools=list(tools),
        system_prompt=SYSTEM_PROMPT,
        middleware=[
            HarnessControlMiddleware(holder, deadline=deadline),
        ],
    )


def english_first(en: str | None, ko: str | None) -> str:
    """En/Ko 병기 필드를 영문 우선 단일 값으로 정규화한다 (프로덕션 규칙 미러)."""
    return (en or "").strip() or (ko or "").strip()


def build_meta(ur: AgentUserRequest) -> dict:
    """초기 메시지 상단의 scalar metadata — 긴 본문은 별도 Markdown 섹션이 싣는다.

    2026-08-21 개정: En/Ko 병기를 영문 우선으로 접고, `sourceCode` 발췌는
    build_target_chunks의 AST 청크로 대체돼 여기서 빠졌다. entireCode/APIKEY는
    이전과 동일하게 제외된다.
    """
    return {
        # fileExtension이 programmingLanguage를 덮어쓰는 규칙까지 반영된 정규화 이름.
        # 청킹·검증과 같은 값을 모델에도 보여 언어 가정이 갈리지 않게 한다.
        "programmingLanguage": resolve_language(
            CodeLanguageHint(ur.programming_language, ur.file_extension)
        ),
        # verifier 좌표계(split("\n"))와 동일한 기준의 줄 수
        "totalLines": len(ur.entire_code.split("\n")),
        "locale": ur.locale,
        "issueLineNumber": ur.issue_line_number,
        "issueRisk": ur.issue_risk,
        "issueName": english_first(ur.issue_name_en, ur.issue_name_ko),
    }


def build_target_chunks(ur: AgentUserRequest) -> list[SourceChunk]:
    """sink 청크를 맨 앞에 두고 trace 이벤트가 가리키는 청크를 중복 없이 잇는다.

    test_re 코퍼스 315건에서는 모든 이벤트가 sink 청크 안에 들어와 결과가 sink 단일
    청크와 같지만, source가 다른 청크에 있는 코퍼스에서도 흐름의 출발점이 보이도록
    이벤트 청크까지 포함한다.
    """
    chunks = build_source_chunks(
        ur.entire_code,
        CodeLanguageHint(ur.programming_language, ur.file_extension),
    )
    wanted = [ur.issue_line_number, *(event.line_no for event in ur.contexts)]
    selected: list[SourceChunk] = []
    for line in wanted:
        chunk = find_chunk_for_line(chunks, line)
        if chunk is not None and chunk not in selected:
            selected.append(chunk)
    return selected


def render_target_chunks(chunks: list[SourceChunk]) -> str:
    """read_code와 동일한 거터로 청크를 렌더링한다 (모델이 두 출처를 같게 취급하도록)."""
    blocks: list[str] = []
    for chunk in chunks:
        ancestry = " > ".join(chunk.ancestors)
        label = f"lines {chunk.start_line}-{chunk.end_line}"
        if ancestry:
            label += f" — {ancestry}"
        body = _numbered(chunk.content.split("\n"), chunk.start_line)
        blocks.append(f"{label}\n```java\n{body}\n```")
    return "\n\n".join(blocks)


def _is_llm_error(exc: BaseException) -> bool:
    """프로덕션 미러 (fix.py:92): OpenAIError/OSError/TimeoutError/RuntimeError → E202."""
    from openai import OpenAIError

    return any(
        isinstance(e, (OpenAIError, OSError, TimeoutError, RuntimeError))
        for e in _walk_causes(exc)
    )


def _walk_causes(exc: BaseException):
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


@dataclass
class FixOutcome:
    """계약 envelope + §12(b) 계측 (제출 이력·종료 사유·턴/토큰)."""

    response: AgentModuleResponse | AgentModuleErrorResponse
    attempts: list[VerifierResult]
    candidates: list[dict]
    terminal_cause: TerminalCause
    model_turns: int
    input_tokens: int
    output_tokens: int
    fix_elapsed_sec: float
    graph_elapsed_sec: float
    edit_calls: int = 0
    edit_errors: int = 0
    edit_failure_kinds: list[str] = field(default_factory=list)
    roundtrip_failures: int = 0
    fuzzy_matches: int = 0
    fuzzy_kinds: list[str] = field(default_factory=list)
    reindented_edits: int = 0
    nudges: int = 0

    def trace_dict(self) -> dict:
        return {
            "terminal_cause": self.terminal_cause,
            "attempts": [
                {"attempt": a["attempt"], "passed": a["passed"], "errors": a["errors"]}
                for a in self.attempts
            ],
            "candidates": self.candidates,
            "model_turns": self.model_turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "edit_calls": self.edit_calls,
            "edit_errors": self.edit_errors,
            "edit_failure_kinds": self.edit_failure_kinds,
            "roundtrip_failures": self.roundtrip_failures,
            "fuzzy_matches": self.fuzzy_matches,
            "fuzzy_kinds": self.fuzzy_kinds,
            "reindented_edits": self.reindented_edits,
            "nudges": self.nudges,
            "fix_elapsed_sec": round(self.fix_elapsed_sec, 3),
            "graph_elapsed_sec": round(self.graph_elapsed_sec, 3),
        }


def _usage_totals(state: dict | None) -> tuple[int, int, int]:
    turns = in_tok = out_tok = 0
    for msg in (state or {}).get("messages", []):
        if isinstance(msg, AIMessage):
            if msg.additional_kwargs.get("harness_terminated"):
                continue  # 합성 종료 메시지는 실제 모델 턴이 아니다
            turns += 1
            usage = getattr(msg, "usage_metadata", None) or {}
            in_tok += usage.get("input_tokens", 0)
            out_tok += usage.get("output_tokens", 0)
    return turns, in_tok, out_tok


def run_fix_with_trace(
    request: AgentModuleRequest,
    model: BaseChatModel,
    *,
    wall_clock_seconds: float = MAX_WALL_CLOCK_SECONDS,
    invoke_config: dict[str, Any] | None = None,
) -> FixOutcome:
    """요청 1건을 완전형 하네스로 처리하고 envelope + 계측을 반환한다."""
    started = time.monotonic()
    ur = request.user_request

    # §6(c): java 전용 — 다른 언어 레코드가 잘못 라우팅되면 조용히 처리하지 않는다.
    declared_language = (ur.programming_language or "").strip().lower()
    declared_extension = (ur.file_extension or "").strip().lower()
    if declared_language != "java" or declared_extension not in {"", ".java"}:
        return FixOutcome(
            response=build_error_response(
                request,
                error=ERROR_UNSUPPORTED_INPUT,
                message=(
                    "only declared Java with an empty or .java fileExtension is supported "
                    "by this experimental harness; "
                    f"got programmingLanguage={ur.programming_language!r}, "
                    f"fileExtension={ur.file_extension!r}."
                ),
                error_node="input",
                error_type="UnsupportedLanguageError",
            ),
            attempts=[],
            candidates=[],
            terminal_cause="unsupported_input",
            model_turns=0,
            input_tokens=0,
            output_tokens=0,
            fix_elapsed_sec=0.0,
            graph_elapsed_sec=time.monotonic() - started,
        )

    # verify_fix.main과 동일한 분해 기준 (split("\n"))
    code_lines = ur.entire_code.split("\n")

    holder = SubmissionHolder()
    language = resolve_language(
        CodeLanguageHint(ur.programming_language, ur.file_extension)
    )
    read_code, edit_code, submit_fix = make_fix_tools(
        code_lines,
        holder,
        language=language,
        file_extension=ur.file_extension or "",
    )
    agent = build_agent(
        model,
        [read_code, edit_code, submit_fix],
        holder=holder,
        deadline=started + wall_clock_seconds,
    )

    initial = build_initial_message(
        InitialTask(
            issue_line_number=ur.issue_line_number,
            total_lines=len(code_lines),
            language=language,
            meta_json=json.dumps(build_meta(ur), ensure_ascii=False, indent=2),
            trace_json=json.dumps(
                [event.model_dump(by_alias=True) for event in ur.contexts],
                ensure_ascii=False,
                indent=2,
            ),
            target_chunks=render_target_chunks(build_target_chunks(ur)),
            checker_description=english_first(
                ur.issue_description_en, ur.issue_description_ko
            ),
            dangerous_example=english_first(
                ur.dangerous_example_en, ur.dangerous_example_ko
            ),
            safe_example=english_first(ur.safe_example_en, ur.safe_example_ko),
        )
    )

    state: dict | None = None
    error: tuple[tuple[str, str], str] | None = None
    error_type: str | None = None
    invoke_started = time.monotonic()
    try:
        # 관측용 config(콜백·메타데이터)는 병합하되 recursion_limit은 하네스가 강제한다
        config: dict[str, Any] = {**(invoke_config or {}), "recursion_limit": MAX_AGENT_STEPS}
        state = agent.invoke({"messages": [HumanMessage(initial)]}, config=config)
    except GraphRecursionError:
        holder.terminal_cause = holder.terminal_cause or "recursion_limit"
        error_type = "GraphRecursionError"
    except Exception as exc:  # noqa: BLE001 — 배치에서 레코드 1건이 전체 실행을 죽이면 안 된다
        if _is_llm_error(exc):
            holder.terminal_cause = "connection_error"
            # 프로덕션 메시지 형태 미러 (fix.py:93) — 상세는 trace에만 남긴다
            error = (ERROR_MODEL_CONNECT, f"fix LLM request failed: {type(exc).__name__}")
        else:
            holder.terminal_cause = "internal_error"
            error = (ERROR_INTERNAL, str(exc) or type(exc).__name__)
        error_type = type(exc).__name__
        # 프로덕션 미러 (fix.py:100-103): 예외 전에 PASS된 제출이 있으면 살린다 —
        # 아래에서 holder.accepted 분기가 error보다 먼저 평가된다.
    fix_elapsed = time.monotonic() - invoke_started

    if holder.accepted is not None:
        holder.terminal_cause = holder.terminal_cause or "pass"
        graph_elapsed = time.monotonic() - started
        response: AgentModuleResponse | AgentModuleErrorResponse = build_success_response(
            request,
            desc=holder.accepted.get("desc", ""),
            code_modifications=holder.accepted.get("codeModifications", []),
            fix_elapsed_time=fix_elapsed,
            graph_elapsed_time=graph_elapsed,
        )
    elif holder.internal_error_message is not None:
        graph_elapsed = time.monotonic() - started
        response = build_error_response(
            request,
            error=ERROR_INTERNAL,
            message=holder.internal_error_message,
            error_node="fix_branch",
            error_type="DiffConversionError",
        )
    elif error is not None:
        graph_elapsed = time.monotonic() - started
        response = build_error_response(
            request,
            error=error[0],
            message=error[1],
            error_node="fix_branch",
            error_type=error_type,
        )
    else:
        holder.terminal_cause = holder.terminal_cause or (
            "budget_exhausted" if holder.attempt_count >= MAX_FIX_ATTEMPTS else "agent_stop"
        )
        graph_elapsed = time.monotonic() - started
        if holder.attempt_count == 0:
            if holder.terminal_cause == "edit_exhausted":
                message = (
                    f"agent could not produce a matching edit: {holder.edit_errors} failed "
                    f"edit_code attempt(s), {holder.consecutive_edit_failures} consecutive "
                    f"(kinds: {', '.join(holder.edit_failure_kinds[-5:])})."
                )
            else:
                message = f"agent finished without submitting a candidate ({holder.terminal_cause})."
        else:
            last = holder.attempts[-1]
            message = (
                f"verification failed after {holder.attempt_count} attempt(s) "
                f"({holder.terminal_cause}); "
                f"last errors: {'; '.join(e['message'] for e in last['errors'])}"
            )
        response = build_error_response(
            request,
            error=ERROR_VERIFY_EXHAUSTED,
            message=message,
            error_node="fix_branch",
            error_type=error_type or "GenerationError",
        )

    # 미들웨어 누적치가 1차 출처다 — invoke가 예외로 중단돼도(state=None) 지출한
    # 토큰이 남는다. state 폴백은 wrap_model_call을 우회하는 상류 변경에 대한 방어.
    turns, in_tok, out_tok = holder.model_turns, holder.input_tokens, holder.output_tokens
    if turns == 0 and state is not None:
        turns, in_tok, out_tok = _usage_totals(state)

    return FixOutcome(
        response=response,
        attempts=holder.attempts,
        candidates=holder.candidates,
        terminal_cause=holder.terminal_cause or "agent_stop",
        model_turns=turns,
        input_tokens=in_tok,
        output_tokens=out_tok,
        fix_elapsed_sec=fix_elapsed,
        graph_elapsed_sec=graph_elapsed,
        edit_calls=holder.edit_calls,
        edit_errors=holder.edit_errors,
        edit_failure_kinds=holder.edit_failure_kinds,
        roundtrip_failures=holder.roundtrip_failures,
        fuzzy_matches=holder.fuzzy_matches,
        fuzzy_kinds=holder.fuzzy_kinds,
        reindented_edits=holder.reindented_edits,
        nudges=holder.nudges,
    )


def run_fix(
    request: AgentModuleRequest,
    model: BaseChatModel,
) -> AgentModuleResponse | AgentModuleErrorResponse:
    """계약 envelope만 필요한 호출자를 위한 편의 래퍼."""
    return run_fix_with_trace(request, model).response
