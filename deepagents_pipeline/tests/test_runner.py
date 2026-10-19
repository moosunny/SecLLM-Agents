"""G004 러너 견고성 테스트 — 레코드 단위 강등, E102 메시지 위생, 식별자 복구."""

import json
import threading
from pathlib import Path
from typing import get_args

import deepagents_pipeline.runner as runner_mod
import pytest
from deepagents_pipeline.harness import TerminalCause
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from deepagents_pipeline.loop_trace import LoopTimelineCallback, loop_output
from deepagents_pipeline.runner import process_record

VALID_LINE = json.dumps(
    {
        "userId": 3,
        "moduleKind": "ActiveSuggestion",
        "requestIdx": 9,
        "model": "qwen3.5-9b",
        "provider": "openai",
        "serviceHost": "http://localhost",
        "servicePort": "8000",
        "userRequest": {
            "locale": "ko",
            "programmingLanguage": "java",
            "issueLineNumber": 1,
            "entireCode": "class A {}",
        },
    }
)


def test_schema_failure_maps_to_e102_without_input_values():
    bad = json.dumps(
        {
            "userId": 3,
            "moduleKind": "ActiveSuggestion",
            "requestIdx": 9,
            "model": "m",
            "provider": "openai",
            "serviceHost": "h",
            "servicePort": "p",
            "userRequest": {
                "locale": "ko",
                "programmingLanguage": "java",
                "issueLineNumber": 999,  # entireCode 줄 수 초과 → ValidationError
                "entireCode": "class A {}",
                "APIKEY": "sk-super-secret",
            },
        }
    )
    row = process_record(bad, base_url_override=None, model_override=None)
    resp = row["response"]
    assert resp["isSuccess"] is False
    assert resp["errorCode"] == "E102"
    assert resp["errorStage"] == "input"
    # 입력값(특히 APIKEY)이 메시지로 새어 나가면 안 된다
    assert "sk-super-secret" not in resp["errorMessage"]
    assert "input_value" not in resp["errorMessage"]
    # raw JSON에서 식별자 복구 (§12(c) 대응표본 식별)
    assert resp["userId"] == 3
    assert resp["requestIdx"] == 9
    assert row["requestIdx"] == 9


def test_unconstructable_base_url_maps_to_e104_not_e999():
    # serviceHost/servicePort 조합 불가는 입력 설정 오류이므로 registry 계약(E104/input)이다.
    record = json.loads(VALID_LINE)
    record["serviceHost"] = "https://api.anthropic.com"
    record["servicePort"] = "v1"  # 포트 자리에 경로 → httpx가 클라이언트 생성 시 거부
    out = process_record(
        json.dumps(record, ensure_ascii=False), base_url_override=None, model_override=None
    )
    resp = out["response"]
    assert resp["isSuccess"] is False
    assert resp["errorCode"] == "E104"
    assert resp["errorStage"] == "input"


def test_non_openai_provider_maps_to_e104():
    line = json.loads(VALID_LINE)
    line["provider"] = "vllm"
    row = process_record(json.dumps(line), base_url_override=None, model_override=None)
    assert row["response"]["errorCode"] == "E104"
    assert row["response"]["errorStage"] == "input"


def test_unexpected_exception_degrades_to_e999(monkeypatch):
    def boom(request, model, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(runner_mod, "run_fix_with_trace", boom)
    row = process_record(VALID_LINE, base_url_override=None, model_override=None)
    resp = row["response"]
    assert resp["isSuccess"] is False
    assert resp["errorCode"] == "E999"
    assert resp["errorNode"] == "output"
    assert resp["errorType"] == "RuntimeError"
    assert resp["errorMessage"] == "kaboom"
    assert row["trace"] is None


def test_model_factory_unexpected_exception_also_degrades(monkeypatch):
    # 모델 생성 블록의 비-ModelFactoryError 예외도 행 단위로 강등돼야 한다
    def boom(request, *, base_url_override, model_override):
        raise RuntimeError("bad base url")

    monkeypatch.setattr(runner_mod, "create_chat_model", boom)
    row = process_record(VALID_LINE, base_url_override=None, model_override=None)
    resp = row["response"]
    assert resp["isSuccess"] is False
    assert resp["errorCode"] == "E999"
    assert resp["userId"] == 3 and resp["requestIdx"] == 9  # 식별자 보존
    assert row["requestIdx"] == 9


def test_loop_timeline_captures_model_turn_and_tool_result():
    callback = LoopTimelineCallback()
    message = AIMessage(
        "",
        tool_calls=[
            {
                "name": "read_code",
                "args": {"offset": 10, "limit": 20},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )

    callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))
    callback.on_tool_start({"name": "read_code"}, "{}", run_id="run-1")
    callback.on_tool_end("10  first\n11  second", run_id="run-1")

    assert callback.events == [
        {
            "ev": "model_turn",
            "turn": 1,
            "tool_calls": [{"tool": "read_code", "args_brief": "offset=10 limit=20"}],
            "text_head": "",
        },
        {
            "ev": "tool_result",
            "tool": "read_code",
            "result_head": "10  first\n11  second",
            "result_lines": 2,
        },
    ]


def test_loop_output_includes_response_trace_and_timeline():
    record = {
        "idx": 9,
        "requestIdx": 1001,
        "elapsed_sec": 1.25,
        "response": {
            "isSuccess": True,
            "codeModifications": [
                {"type": "add", "startLine": 1, "content": "class A {}"}
            ],
        },
        "trace": {
            "terminal_cause": "pass",
            "model_turns": 4,
            "input_tokens": 100,
            "output_tokens": 20,
        },
    }

    row = loop_output(record, [{"ev": "model_turn", "turn": 1}])

    assert list(row) == [
        "idx",
        "requestIdx",
        "passed",
        "terminal_cause",
        "model_turns",
        "input_tokens",
        "output_tokens",
        "elapsed_sec",
        "response",
        "trace",
        "timeline",
    ]
    assert row["idx"] == 9
    assert row["requestIdx"] == 1001
    assert row["passed"] is True
    assert row["response"] == record["response"]
    assert row["trace"] == record["trace"]
    assert row["timeline"] == [{"ev": "model_turn", "turn": 1}]


def test_loop_schema_terminal_causes_match_harness():
    schema_path = (
        Path(__file__).parents[1]
        / ".agents/skills/deepagents-eval/schemas/loop-result.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    def enums_at_terminal_cause(node: object) -> list[set[str]]:
        if isinstance(node, dict):
            found = []
            properties = node.get("properties")
            if isinstance(properties, dict):
                terminal_cause = properties.get("terminal_cause")
                if isinstance(terminal_cause, dict) and isinstance(
                    terminal_cause.get("enum"), list
                ):
                    found.append(
                        {value for value in terminal_cause["enum"] if value is not None}
                    )
            for value in node.values():
                found.extend(enums_at_terminal_cause(value))
            return found
        if isinstance(node, list):
            return [enum for value in node for enum in enums_at_terminal_cause(value)]
        return []

    enums = enums_at_terminal_cause(schema)

    assert len(enums) == 2
    assert all(enum == set(get_args(TerminalCause)) for enum in enums)


def test_loop_schema_includes_nudges_trace_counter():
    schema_path = (
        Path(__file__).parents[1]
        / ".agents/skills/deepagents-eval/schemas/loop-result.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    trace_schema = schema["$defs"]["trace"]

    assert "nudges" in trace_schema["required"]
    assert trace_schema["properties"]["nudges"] == {
        "type": "integer",
        "minimum": 0,
    }


def test_loop_schema_error_response_includes_diagnostic_fields():
    schema_path = (
        Path(__file__).parents[1]
        / ".agents/skills/deepagents-eval/schemas/loop-result.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    error_schema = schema["$defs"]["errorResponse"]

    assert {"errorNode", "errorType"} <= set(error_schema["required"])
    assert error_schema["properties"]["errorNode"] == {"type": "string"}
    assert error_schema["properties"]["errorType"] == {"type": "string"}


def test_main_processes_five_records_concurrently(monkeypatch, tmp_path):
    # Given: five records whose fake processing synchronizes at one barrier.
    barrier = threading.Barrier(5)
    thread_ids: set[int] = set()
    lock = threading.Lock()
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    records = []
    for idx in range(5):
        record = json.loads(VALID_LINE)
        record["requestIdx"] = 100 + idx * 10
        records.append(json.dumps(record))
    input_path.write_text("\n".join(records) + "\n", encoding="utf-8")

    def fake_process_record(line, **kwargs):
        del kwargs
        with lock:
            thread_ids.add(threading.get_ident())
        barrier.wait(timeout=2)
        request_idx = json.loads(line)["requestIdx"]
        return {
            "requestIdx": request_idx,
            "elapsed_sec": 0.1,
            "response": {"isSuccess": True},
            "trace": None,
        }

    monkeypatch.setattr(runner_mod, "process_record", fake_process_record)
    monkeypatch.setattr(runner_mod, "init_langfuse_handler", lambda: None)

    # When: the CLI requests five concurrent records.
    exit_code = runner_mod.main(
        [
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--concurrency",
            "5",
        ]
    )

    # Then: all five records overlap and are emitted in input order.
    assert exit_code == 0
    assert len(thread_ids) == 5
    output_rows = [
        json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    output_ids = [row["idx"] for row in output_rows]
    request_ids = [row["requestIdx"] for row in output_rows]
    assert output_ids == list(range(5))
    assert request_ids == [100, 110, 120, 130, 140]
    first_row = output_rows[0]
    assert first_row["response"] == {"isSuccess": True}
    assert first_row["trace"] is None
    assert first_row["timeline"] == []


def test_main_rejects_removed_loop_format_argument(tmp_path):
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    input_path.write_text("", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        runner_mod.main(
            [
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--loop-format",
            ]
        )

    assert exc_info.value.code == 2


def test_loop_full_capture_records_prompt_turns_and_tool_results():
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    callback = LoopTimelineCallback()
    system = SystemMessage("SYS")
    task = HumanMessage("TASK")
    callback.on_chat_model_start({}, [[system, task]])
    message = AIMessage(
        "thinking done",
        tool_calls=[
            {"name": "edit_code", "args": {"old_string": "a", "new_string": "b"},
             "id": "call-1", "type": "tool_call"}
        ],
        additional_kwargs={"reasoning_content": "R"},
        response_metadata={"finish_reason": "tool_calls"},
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))
    callback.on_tool_start({"name": "edit_code"}, "{}", run_id="run-1")
    callback.on_tool_end(
        ToolMessage("OK. Working copy updated.", tool_call_id="call-1"), run_id="run-1"
    )
    nudge = HumanMessage("nudge")
    callback.on_chat_model_start({}, [[system, task, message,
                                       ToolMessage("OK.", tool_call_id="call-1"), nudge]])

    assert callback.loop == [
        {"ev": "prompt", "turn": 1,
         "messages": [{"role": "system", "content": "SYS"}, {"role": "human", "content": "TASK"}]},
        {"ev": "model_turn", "turn": 1, "text": "thinking done", "reasoning": "R",
         "tool_calls": [{"id": "call-1", "tool": "edit_code",
                         "args": {"old_string": "a", "new_string": "b"}}],
         "invalid_tool_calls": [], "finish_reason": "tool_calls",
         "usage": {"input_tokens": 10, "output_tokens": 5}},
        {"ev": "tool_result", "turn": 1, "tool": "edit_code", "tool_call_id": "call-1",
         "result": "OK. Working copy updated."},
        {"ev": "prompt", "turn": 2, "messages": [{"role": "human", "content": "nudge"}]},
    ]
    # 요약 timeline은 기존 형식 그대로 유지된다
    assert callback.events[0]["text_head"] == "thinking done"

    record = {"idx": 0, "requestIdx": 0, "elapsed_sec": 1.0,
              "response": {"isSuccess": True}, "trace": {"terminal_cause": "pass"}}
    row = loop_output(record, callback.events, callback.loop)
    assert list(row)[-2:] == ["timeline", "loop"]
    assert row["loop"][-1] == {"ev": "harness_end", "cause": "pass"}
    assert "loop" not in loop_output(record, callback.events)


def test_loop_records_unwrapped_args_with_raw_original():
    callback = LoopTimelineCallback()
    inner = {"old_string": "int a = 1;", "new_string": "int a = 2;"}
    wrapped = {"arguments": json.dumps(inner)}
    message = AIMessage(
        "",
        tool_calls=[{"name": "edit_code", "args": wrapped, "id": "call-1", "type": "tool_call"}],
    )

    callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))

    call = callback.loop[-1]["tool_calls"][0]
    assert call["args"] == inner
    assert call["args_unwrapped"] is True
    assert call["raw_args"] == wrapped
    assert callback.events[-1]["tool_calls"][0]["args_brief"] == "old[0]='int a = 1;' (1L->1L)"


def test_loop_canonical_args_have_no_unwrap_fields():
    callback = LoopTimelineCallback()
    message = AIMessage(
        "",
        tool_calls=[
            {"name": "submit_fix", "args": {"desc": "x"}, "id": "call-1", "type": "tool_call"}
        ],
    )

    callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))

    assert callback.loop[-1]["tool_calls"][0] == {
        "id": "call-1",
        "tool": "submit_fix",
        "args": {"desc": "x"},
    }
