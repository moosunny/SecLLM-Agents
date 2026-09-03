---
name: deepagents-eval
description: Run repeatable deepagents_pipeline JSONL evaluations, including smoke or full test_re batches, unified response and loop output, schema validation, coverage checks, aggregate statistics, and failure timeline review. Use for deepagents evaluation, loop JSONL, test_re 전체 테스트, 배치 평가, 결과 스키마 검증, 실패 리뷰.
---

# Deepagents Evaluation

Run the `deepagents_pipeline` evaluation from preflight through final failure
review. The result format is owned by this skill:

`schemas/loop-result.schema.json`

Every output row always includes the full contract `response` (including
`codeModifications`), the full harness `trace`, compact aggregate fields, and
the model/tool `timeline`. The runner has no output-format switch.
`idx` is the zero-based position among the selected input records; the
original request identifier is preserved separately as `requestIdx`.

Do not require or compare against an older result file merely to discover the
output shape. A previous result may be used only when the user explicitly asks
for metric comparison.

## Resolve paths

Resolve all relative paths against the `deepagents_pipeline` project root, not
against the skill directory. The project root is three parents above this
`SKILL.md`.

Required run inputs:

- `output`: destination JSONL path.

Optional run inputs:

- `input`: request JSONL path. When omitted, always use
  `/home/moosunny/active-suggestion-nipa/tests/experiment_inputs/java/test_re.jsonl`.
- `model`: value for `--model-override`; omit to use each request's model.
- `indices`: comma-separated requestIdx values for a subset.
- `wall_clock_seconds`: per-record deadline; default to the runner default.
- `mode`: `smoke` or `full`; infer `smoke` when `indices` is supplied,
  otherwise `full`.
- `overwrite`: false by default. Never replace an existing output unless the
  user explicitly requests it.

Always use `$LLM_B200_API` as `--base-url` unless the user explicitly requests
a different server. Always pass `--concurrency 5`; concurrency is not an
inferred or user-omitted default in this skill.

Never print API keys or source-code payloads from the input.

## Evaluation sequence

### Preflight

1. Confirm the input exists and every non-empty line is JSON.
2. Confirm the output differs from the input. Create only its parent directory.
3. Refuse an existing output unless `overwrite=true`.
4. Run the focused harness and runner tests.
5. In `full` mode, run the complete `deepagents_pipeline` test suite.
6. When `base_url` is provided, make one bounded model-server health probe.
   Record an unavailable server as a blocker before starting the paid/full run.
7. Confirm `read_code` working-copy coverage and
   `parallel_tool_calls=False` coverage are green before continuing.

### Batch execution

Run from the `deepagents_pipeline` project root:

```text
uv run python -m deepagents_pipeline.runner
  --input <input-or-default-test_re.jsonl>
  --output <output>
  --base-url "$LLM_B200_API"
  --concurrency 5
  [--indices <indices>]
  [--model-override <model>]
  [--wall-clock-seconds <seconds>]
```

Construct this as one command with the applicable arguments; do not copy the
line breaks above literally. For a long batch, run it as a background terminal
session and subscribe to completion with a monitor. Never sleep or poll.

Do not retry individual records during the primary run. Retries would make the
batch incomparable and could duplicate model cost.

### Schema and coverage validation

Treat `schemas/loop-result.schema.json` as the only output-format authority.
Validate it as a Draft 2020-12 schema, then validate every non-empty output
line independently.

Validation must prove all of the following:

- Every line parses as one JSON object.
- Every object passes the bundled schema.
- Top-level and timeline event properties contain no undeclared fields.
- Output `idx` values exactly equal `0..N-1` in row order.
- For valid request records, output `requestIdx` values exactly equal the
  selected input requestIdx values in input order after applying `indices`.
- Missing, unexpected, and duplicate `requestIdx` values are all zero.
- The runner exits with code 0.

Schema failure is a failed evaluation artifact even when the model run itself
completed.

### Aggregate results

Compute and report:

- record count, pass count, failure count, and pass rate;
- terminal-cause counts;
- model turns: total, mean, p50, p95, max;
- input and output tokens: total, mean, p50, p95, max;
- elapsed seconds: total, mean, p50, p95, max;
- timeline event counts;
- tool-call counts by tool name;
- records containing two or more `edit_code` calls.

Use deterministic percentile computation and state the method if it differs
from NumPy's default linear percentile.

### Failure classification

An accepted candidate that survives a later broken loop is shipped as a
fallback success. Classify accordingly:

1. Count fallback passes separately: rows with `passed=true` and
   `terminal_cause != "pass"` shipped an accepted candidate despite a later
   abnormal termination. Report their count and causes next to the plain pass
   rate.
2. Classify failed rows with only `FAIL` submissions as
   `verification_fail_then_stop`.
3. Classify failed rows with no submission as `stopped_without_submit`.
4. A failed row whose timeline contains a `PASS` submission is a harness
   anomaly (the fallback should have shipped it); flag it, do not bucket it.

Review every non-`pass`, non-review-protocol failure individually. Include:

- requestIdx and checker metadata;
- issue line and relevant source excerpt;
- final timeline transition;
- read/edit/submit counts;
- confirmed root cause;
- whether the failure belongs to the candidate, verifier, harness, model
  protocol, or infrastructure;
- the smallest recommended correction.

Do not label a plausible explanation as confirmed unless the recorded timeline
or a reproduction distinguishes it from alternatives.

## Completion report

Lead with the artifact path and whether execution, schema, and coverage gates
passed. Then provide aggregate statistics, protocol-only failures, genuine
failures, and any unavailable verification.

The run is complete only when:

- the batch process has exited;
- schema and ID coverage checks are clean;
- statistics are computed from the saved artifact;
- every genuine failure has been reviewed;
- no monitor or background batch remains live.
