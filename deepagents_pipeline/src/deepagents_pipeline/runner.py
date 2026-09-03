"""JSONL 배치 러너 — eval 러너가 격리 venv 인터프리터로 기동하는 진입점 (설계 §1.3).

    deepagents_pipeline/.venv/bin/python -m deepagents_pipeline.runner \
        --input tests/experiment_inputs/java/test.jsonl --output out.jsonl \
        [--indices 0,1,2] [--base-url URL] [--model-override NAME] \
        [--wall-clock-seconds 300] [--concurrency 5]

입력 각 줄은 AgentModuleRequest wire-format JSON이다. 출력 각 줄:
{"idx", "requestIdx", "elapsed_sec", "response": <계약 envelope>, "trace": <§12(b) 계측>,
 "passed", "terminal_cause", "model_turns", "input_tokens", "output_tokens",
 "timeline": <모델 턴·도구 결과 요약>, "loop": <에이전트 루프 전문 (2026-09-02)>}.
레코드 단위 오류는 오류 envelope 행으로 강등되고 배치는 계속된다.
러너 프로세스 기동은 에이전트 노출 도구가 아니므로 Bash 배제 원칙과 무관하다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from pydantic import ValidationError

from deepagents_pipeline.contracts import (
    ERROR_INPUT_SCHEMA,
    ERROR_INTERNAL,
    ERROR_REGISTRY,
    AgentModuleRequest,
    build_error_response,
)
from deepagents_pipeline.harness import MAX_WALL_CLOCK_SECONDS, run_fix_with_trace
from deepagents_pipeline.loop_trace import LoopTimelineCallback, loop_output
from deepagents_pipeline.models import ModelFactoryError, create_chat_model


def _validation_details(exc: ValidationError) -> str:
    """프로덕션 미러 (schema.py:187) — 위치·사유만 남기고 입력값은 노출하지 않는다."""
    parts = []
    for error in exc.errors():
        loc = error.get("loc", ())
        path = ".".join(str(p) for p in loc) if isinstance(loc, tuple) else str(loc)
        parts.append(f"{path}: {error.get('msg', 'invalid value')}")
    return "; ".join(parts)


def load_project_dotenv() -> None:
    """deepagents_pipeline/.env를 로딩한다 — 이미 설정된 셸 env는 절대 덮지 않는다.

    CRLF·양끝 따옴표를 정리하고 주석/빈 줄은 건너뛴다. 파일이 없으면 no-op.
    (메인 레포 .env는 CRLF라 source가 깨지는 관례적 문제의 우회이기도 하다.)
    """
    dotenv = Path(__file__).resolve().parents[2] / ".env"
    if not dotenv.is_file():
        return
    for raw in dotenv.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


# 러너·드라이버(process_record 임포트 경로 포함)가 별도 설정 없이 .env를 쓰도록
# 임포트 시점에 1회 로딩한다. setdefault만 하므로 셸 env가 항상 우선한다.
load_project_dotenv()


def init_langfuse_handler() -> Any | None:
    """LANGFUSE_TRACING=1일 때만 CallbackHandler 생성 — 기본은 완전 비활성.

    초기화 실패는 경고 후 무추적으로 강등한다 (관측 장애가 배치를 죽이면 안 된다).
    LANGFUSE_HOST 미설정 시 .env 관례인 LANGFUSE_BASE_URL을 대신 쓴다.
    """
    if os.getenv("LANGFUSE_TRACING") != "1":
        return None
    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        host = (os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL") or "").strip('"')
        Langfuse(host=host or None)  # 키는 표준 LANGFUSE_PUBLIC/SECRET_KEY env에서
        return CallbackHandler()
    except Exception as exc:  # noqa: BLE001
        print(
            f"[runner] langfuse init failed ({type(exc).__name__}); tracing disabled",
            file=sys.stderr,
        )
        return None


def flush_langfuse() -> None:
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:  # noqa: BLE001, S110 — 관측 계층 종료 실패는 무시
        pass


def _langfuse_invoke_config(
    handler: Any, request: AgentModuleRequest, *, session_id: str, model_name: str
) -> dict:
    return {
        "callbacks": [handler],
        "run_name": f"fix-idx{request.request_idx}",
        "metadata": {
            "langfuse_session_id": session_id,
            "langfuse_tags": [model_name, f"checker:{request.user_request.checker_id}"],
            "requestIdx": request.request_idx,
            "issueLineNumber": request.user_request.issue_line_number,
        },
    }


def _salvage_ids(line: str) -> tuple[int | None, int | None]:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None, None
    user_id = raw.get("userId")
    request_idx = raw.get("requestIdx")
    return (
        user_id if isinstance(user_id, int) else None,
        request_idx if isinstance(request_idx, int) else None,
    )


def process_record(
    line: str,
    *,
    base_url_override: str | None,
    model_override: str | None,
    wall_clock_seconds: float = MAX_WALL_CLOCK_SECONDS,
    langfuse_handler: Any | None = None,
    langfuse_session_id: str = "",
    callbacks: Sequence[BaseCallbackHandler] = (),
) -> dict:
    started = time.monotonic()
    request: AgentModuleRequest | None = None
    trace: dict | None = None
    salvaged_user_id, salvaged_idx = _salvage_ids(line)
    # 최후 방벽이 본문 전체를 감싼다 — 파싱·모델 생성·실행 어느 단계의 예외도
    # 오류 행 1건으로 강등되고 배치는 계속된다.
    try:
        try:
            request = AgentModuleRequest.model_validate_json(line)
            model = create_chat_model(
                request, base_url_override=base_url_override, model_override=model_override
            )
        except ValidationError as exc:
            response = build_error_response(
                None,
                error=ERROR_INPUT_SCHEMA,
                message=f"schema validation failed: {_validation_details(exc)}",
                user_id=salvaged_user_id,
                request_idx=salvaged_idx,
                error_node="input",
                error_type=type(exc).__name__,
            )
        except ModelFactoryError as exc:
            response = build_error_response(
                request,
                error=ERROR_REGISTRY,
                message=str(exc),
                user_id=salvaged_user_id,
                request_idx=salvaged_idx,
                error_node="input",
                error_type=type(exc).__name__,
            )
        else:
            invoke_config: dict[str, Any] = {}
            if langfuse_handler is not None:
                invoke_config = _langfuse_invoke_config(
                    langfuse_handler,
                    request,
                    session_id=langfuse_session_id,
                    model_name=model_override or request.model_name,
                )
            if callbacks:
                invoke_config["callbacks"] = [
                    *invoke_config.get("callbacks", []),
                    *callbacks,
                ]
            outcome = run_fix_with_trace(
                request,
                model,
                wall_clock_seconds=wall_clock_seconds,
                invoke_config=invoke_config or None,
            )
            response = outcome.response
            trace = outcome.trace_dict()
    except Exception as exc:  # noqa: BLE001
        response = build_error_response(
            request,
            error=ERROR_INTERNAL,
            message=str(exc) or type(exc).__name__,
            user_id=salvaged_user_id,
            request_idx=salvaged_idx,
            error_node="output",
            error_type=type(exc).__name__,
        )
        trace = None

    return {
        "requestIdx": request.request_idx if request else salvaged_idx,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "response": response.model_dump(by_alias=True),
        "trace": trace,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--indices", default=None, help="쉼표 구분 requestIdx 필터 (기본: 전체)")
    parser.add_argument("--base-url", default=None, dest="base_url")
    parser.add_argument("--model-override", default=None, dest="model_override")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument(
        "--wall-clock-seconds",
        type=float,
        default=MAX_WALL_CLOCK_SECONDS,
        dest="wall_clock_seconds",
        help="레코드당 데드라인 (§6(b), 모든 arm 동일 적용)",
    )
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")

    wanted: set[int] | None = None
    if args.indices:
        wanted = {int(tok) for tok in args.indices.split(",") if tok.strip()}

    langfuse_handler = init_langfuse_handler()
    session_id = f"{args.input.stem}->{args.output.stem}"
    if langfuse_handler is not None:
        print(f"[runner] langfuse tracing ON (session={session_id})", file=sys.stderr)

    def run_line(item: tuple[int, str]) -> tuple[dict[str, Any], dict[str, Any]]:
        idx, line = item
        timeline = LoopTimelineCallback()
        record = process_record(
            line,
            base_url_override=args.base_url,
            model_override=args.model_override,
            wall_clock_seconds=args.wall_clock_seconds,
            langfuse_handler=langfuse_handler,
            langfuse_session_id=session_id,
            callbacks=(timeline,),
        )
        result = {"idx": idx, **record}
        output_row = loop_output(result, timeline.events, timeline.loop)
        return result, output_row

    processed = 0
    with args.input.open(encoding="utf-8") as fin, args.output.open("w", encoding="utf-8") as fout:
        lines: list[str] = []
        for line in fin:
            line = line.strip()
            if not line:
                continue
            if wanted is not None and _salvage_ids(line)[1] not in wanted:
                continue
            lines.append(line)
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            indexed_lines = enumerate(lines)
            for processed, (result, output_row) in enumerate(
                executor.map(run_line, indexed_lines), start=1
            ):
                fout.write(json.dumps(output_row, ensure_ascii=False) + "\n")
                fout.flush()
                resp = result["response"]
                cause = (result["trace"] or {}).get("terminal_cause") if result["trace"] else None
                print(
                    f"[runner] idx={result['idx']} requestIdx={result['requestIdx']} "
                    f"isSuccess={resp.get('isSuccess')} "
                    f"cause={cause} elapsed={result['elapsed_sec']}s",
                    file=sys.stderr,
                )

    if langfuse_handler is not None:
        flush_langfuse()
    print(f"[runner] done: {processed} record(s) -> {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
