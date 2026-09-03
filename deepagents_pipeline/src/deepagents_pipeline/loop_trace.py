"""Unified runner rows with full responses, compact timelines, and the full agent loop.

`timeline`은 헤드 120자만 남기는 요약 이벤트(2026-09-01 형식 유지)이고, `loop`는
2026-09-02에 추가된 전문 기록이다 — 첫 모델 호출의 프롬프트(system + 초기 task),
매 모델 턴의 텍스트·reasoning·tool_calls 전체 인자·finish_reason·usage, 도구 결과
전문, 하네스가 중간에 끼워 넣은 human 메시지(넛지), 도구/LLM 오류. 하네스 합성
종료 메시지는 LLM 호출이 아니므로 콜백에 잡히지 않고 `harness_end` 이벤트로
terminal_cause만 남긴다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage

HEAD_CHARS = 120


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _args_brief(tool_name: str, args: Mapping[str, Any]) -> str:
    if tool_name == "read_code":
        return f"offset={args.get('offset', 0)} limit={args.get('limit', 100)}"
    if tool_name == "edit_code":
        old = str(args.get("old_string", ""))
        new = str(args.get("new_string", ""))
        first = old.splitlines()[0][:60] if old.splitlines() else ""
        return (
            f"old[0]={first!r} "
            f"({max(1, len(old.splitlines()))}L->{max(1, len(new.splitlines()))}L)"
        )
    if tool_name == "submit_fix":
        return f"desc={str(args.get('desc', ''))[:80]!r}"
    if tool_name == "write_todos":
        todos = args.get("todos", [])
        count = len(todos) if isinstance(todos, list) else 0
        return f"{count} todo(s)"
    return json.dumps(dict(args), ensure_ascii=False, default=str)[:HEAD_CHARS]


class LoopTimelineCallback(BaseCallbackHandler):
    """Capture compact model turns and tool results without source-sized payloads."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.loop: list[dict[str, Any]] = []
        self._turn = 0
        self._tool_names: dict[str, str] = {}
        self._seen_prompt_len = 0

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        **kwargs: Any,
    ) -> None:
        """모델에 실제로 들어간 프롬프트 중 아직 기록되지 않은 비-AI/비-tool 메시지를 남긴다.

        첫 호출: system + 초기 task 전문. 이후 호출: 하네스가 끼워 넣은 human
        메시지(넛지)만 새로 기록된다 — AI/tool 메시지는 on_llm_end/on_tool_end가 담당.
        """
        del serialized, kwargs
        if not messages:
            return
        prompt = list(messages[0])
        fresh = prompt[self._seen_prompt_len :]
        self._seen_prompt_len = len(prompt)
        recorded = [
            {"role": m.type, "content": _text(m)}
            for m in fresh
            if m.type not in ("ai", "tool")
        ]
        if recorded:
            self.loop.append(
                {"ev": "prompt", "turn": self._turn + 1, "messages": recorded}
            )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        del kwargs
        self.loop.append(
            {
                "ev": "llm_error",
                "turn": self._turn + 1,
                "error_type": type(error).__name__,
                "error": str(error)[:2000],
            }
        )

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        del kwargs
        for generation_group in getattr(response, "generations", []) or []:
            for generation in generation_group:
                message = getattr(generation, "message", None)
                if not isinstance(message, AIMessage):
                    continue
                self._turn += 1
                self.loop.append(_full_model_turn(self._turn, message))
                calls = [
                    {
                        "tool": str(call.get("name", "")),
                        "args_brief": _args_brief(
                            str(call.get("name", "")),
                            call.get("args", {}) if isinstance(call.get("args"), dict) else {},
                        ),
                    }
                    for call in message.tool_calls
                ]
                self.events.append(
                    {
                        "ev": "model_turn",
                        "turn": self._turn,
                        "tool_calls": calls,
                        "text_head": message.text[:HEAD_CHARS],
                    }
                )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        del input_str, kwargs
        self._tool_names[str(run_id)] = str(serialized.get("name", ""))

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        text = _text(output)
        tool = self._tool_names.pop(str(run_id), "")
        self.events.append(
            {
                "ev": "tool_result",
                "tool": tool,
                "result_head": text[:HEAD_CHARS],
                "result_lines": len(text.splitlines()),
            }
        )
        self.loop.append(
            {
                "ev": "tool_result",
                "turn": self._turn,
                "tool": tool,
                "tool_call_id": getattr(output, "tool_call_id", None),
                "result": text,
            }
        )

    def on_tool_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        self.loop.append(
            {
                "ev": "tool_error",
                "turn": self._turn,
                "tool": self._tool_names.pop(str(run_id), ""),
                "error_type": type(error).__name__,
                "error": str(error)[:2000],
            }
        )


def _full_model_turn(turn: int, message: AIMessage) -> dict[str, Any]:
    extra = message.additional_kwargs or {}
    meta = message.response_metadata or {}
    usage = getattr(message, "usage_metadata", None) or {}
    return {
        "ev": "model_turn",
        "turn": turn,
        "text": message.text,
        "reasoning": extra.get("reasoning_content"),
        "tool_calls": [
            {
                "id": call.get("id"),
                "tool": str(call.get("name", "")),
                "args": call.get("args", {}),
            }
            for call in message.tool_calls
        ],
        "invalid_tool_calls": [
            {
                "id": call.get("id"),
                "tool": call.get("name"),
                "args": call.get("args"),
                "error": call.get("error"),
            }
            for call in message.invalid_tool_calls
        ],
        "finish_reason": meta.get("finish_reason"),
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        },
    }


def loop_output(
    record: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    loop: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Combine the full runner envelope with compact loop metrics, events, and the full loop.

    `loop`가 None이면(구형 호출) 2026-09-01 형식 그대로 — `loop` 키를 붙이지 않는다.
    """
    response = record["response"]
    raw_trace = record.get("trace")
    trace = raw_trace or {}
    row = {
        "idx": record["idx"],
        "requestIdx": record["requestIdx"],
        "passed": bool(response.get("isSuccess")),
        "terminal_cause": trace.get("terminal_cause"),
        "model_turns": int(trace.get("model_turns", 0)),
        "input_tokens": int(trace.get("input_tokens", 0)),
        "output_tokens": int(trace.get("output_tokens", 0)),
        "elapsed_sec": float(record["elapsed_sec"]),
        "response": response,
        "trace": raw_trace,
        "timeline": timeline,
    }
    if loop is not None:
        full = list(loop)
        if trace.get("terminal_cause"):
            full.append({"ev": "harness_end", "cause": trace["terminal_cause"]})
        row["loop"] = full
    return row
