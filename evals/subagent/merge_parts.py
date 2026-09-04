"""Merge per-index part files into an evals-format JSONL.

Re-runs the full ``verify_fix.check_candidate`` validation on every fix part
(the agent's own verify claim is not trusted) and records failures in
``guard_errors``.  Output records use the same key layout as
``evals/260729/subagent_sonnet5_java.jsonl``, plus ``multi_site`` and
``verify``.

Usage:
    .venv/bin/python evals/subagent/merge_parts.py \
        --test-jsonl tests/experiment_inputs/java/test.jsonl \
        --parts <workdir>/parts \
        --out evals/<date>/subagent_<model>_java.jsonl \
        --model claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_fix import check_candidate  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]


def _build_record(idx: int, source: dict, part: dict | None, model: str) -> dict:
    user = source["userRequest"]
    code_lines = user["entireCode"].split("\n")

    error: str | None = None
    guard_errors: list[str] = []
    multi_site = False
    final_response: dict | None = None
    verify = part.get("verify") if part else None
    elapsed = part.get("elapsed_sec") if part else None

    if part is None:
        error = "part file missing"
    elif part.get("error"):
        error = str(part["error"])
        if not verify:
            guard_errors.append(
                "error record has no verify evidence (errors are only allowed "
                "after failed verification attempts)"
            )
    else:
        candidate = {
            "desc": part.get("desc"),
            "codeModifications": part.get("codeModifications"),
            "multi_site": part.get("multi_site", False),
        }
        result = check_candidate(code_lines, candidate)
        guard_errors = result["errors"]
        multi_site = result["multi_site"]
        final_response = {
            "isSuccess": True,
            "userId": source.get("userId", 0),
            "moduleKind": "ActiveSuggestion",
            "requestIdx": source.get("requestIdx", idx),
            "desc": part.get("desc"),
            "codeModifications": part.get("codeModifications"),
            "timings": {
                "explore_elapsed_time": 0.0,
                "fix_elapsed_time": 0.0,
                "select_elapsed_time": 0.0,
                "graph_elapsed_time": float(elapsed or 0.0),
            },
        }

    return {
        "idx": idx,
        "model": model,
        "runner": "subagent",
        "tool_choice": None,
        "thinking": False,
        "thinking_parameter": None,
        "reasoning_effort": None,
        "checkerId": user["checkerId"],
        "issueNameKo": user["issueNameKo"],
        "issueLineNumber": user["issueLineNumber"],
        "elapsed_sec": elapsed,
        "fix_turns": None,
        "review_status": None,
        "guard_errors": guard_errors,
        "flagged": None,
        "error": error,
        "multi_site": multi_site,
        "verify": verify,
        "fix_inputs": [],
        "fix_outputs": [],
        "tool_events": [],
        "final_response": final_response,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--test-jsonl",
        type=Path,
        default=_REPO / "tests/experiment_inputs/java/test.jsonl",
    )
    parser.add_argument("--parts", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    records = []
    with args.test_jsonl.open(encoding="utf-8") as handle:
        for idx, line in enumerate(handle):
            source = json.loads(line)
            part_path = args.parts / f"{idx:04d}.json"
            part = None
            if part_path.exists():
                part = json.loads(part_path.read_text(encoding="utf-8"))
            records.append(_build_record(idx, source, part, args.model))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    fixed = [r for r in records if r["final_response"]]
    errored = [r for r in records if r["error"]]
    guard_failed = [r for r in records if r["guard_errors"]]
    multi = [r for r in records if r["multi_site"]]
    attempts = [
        r["verify"]["attempts"]
        for r in records
        if isinstance(r.get("verify"), dict) and "attempts" in r["verify"]
    ]
    print(f"total: {len(records)} | fixed: {len(fixed)} | error: {len(errored)}")
    print(f"guard failures at merge: {len(guard_failed)} "
          f"({[r['idx'] for r in guard_failed][:20]})")
    print(f"multi_site: {len(multi)} ({[r['idx'] for r in multi][:20]})")
    if attempts:
        print(f"verify attempts: mean={sum(attempts) / len(attempts):.2f} "
              f"max={max(attempts)}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
