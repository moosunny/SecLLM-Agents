"""Split a fix eval test.jsonl into per-index input directories.

Each record becomes ``<out>/NNNN/`` with:
  - ``meta.json``  — checker metadata, SAST trace (``contexts``), examples
  - ``code<ext>``  — the original source file (``entireCode`` verbatim)

Usage:
    .venv/bin/python evals/subagent/prepare_inputs.py \
        --test-jsonl tests/experiment_inputs/java/test.jsonl \
        --out <workdir>/inputs
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

_META_FIELDS = (
    "checkerId",
    "issueLineNumber",
    "issueRisk",
    "issueNameKo",
    "issueNameEn",
    "issueDescriptionKo",
    "issueDescriptionEn",
    "sourceCode",
    "contexts",
    "safeExampleKo",
    "safeExampleEn",
    "dangerousExampleKo",
    "dangerousExampleEn",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--test-jsonl",
        type=Path,
        default=_REPO / "tests/experiment_inputs/java/test.jsonl",
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    count = 0
    with args.test_jsonl.open(encoding="utf-8") as handle:
        for idx, line in enumerate(handle):
            record = json.loads(line)
            user = record["userRequest"]
            target = args.out / f"{idx:04d}"
            target.mkdir(parents=True, exist_ok=True)

            entire_code = user["entireCode"]
            meta = {"idx": idx, "totalLines": len(entire_code.split("\n"))}
            for field in _META_FIELDS:
                meta[field] = user.get(field)
            (target / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
            )

            extension = user.get("fileExtension") or ".java"
            (target / f"code{extension}").write_text(entire_code, encoding="utf-8")
            count += 1

    print(f"wrote {count} input directories under {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
