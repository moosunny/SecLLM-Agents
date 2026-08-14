from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from agents.error_handling import InternalError, error_response_from_exception
from agents.schema import AgentModuleErrorResponse, AgentModuleResponse, parse_request_json_bytes

run_graph = None


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    _load_env()
    request = None
    exit_code = 0
    try:
        request = parse_request_json_bytes(args.input.read_bytes(), source_name=str(args.input))
        response = _run_graph(
            request,
            base_url_override=args.base_url,
            max_fix_turns=args.max_fix_turns,
        )
    except Exception as error:  # noqa: BROAD_EXCEPT_OK - CLI boundary must always emit JSON.
        response = error_response_from_exception(error, request)
        exit_code = 1
    try:
        _emit_response(response, args.output)
    except InternalError as error:
        error_response = error_response_from_exception(error, request)
        print(json.dumps(error_response.model_dump(), ensure_ascii=False))
        return 1
    return exit_code


def _run_graph(request, *, base_url_override=None, max_fix_turns=None):
    global run_graph
    if run_graph is None:
        from agents.graph.builder import run_graph as imported_run_graph

        run_graph = imported_run_graph
    kwargs = {"base_url_override": base_url_override}
    if max_fix_turns is not None:
        kwargs["max_fix_turns"] = max_fix_turns
    return run_graph(request, **kwargs)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="active-suggestion CLI")
    parser.add_argument("-i", "--input", required=True, type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument(
        "--max-fix-turns",
        type=_fix_turn_limit,
        help="maximum model turns inside the fix agent (default: configured value)",
    )
    return parser.parse_args(argv)


def _fix_turn_limit(value: str) -> int:
    parsed = int(value)
    if parsed < 2:
        raise argparse.ArgumentTypeError("max fix turns must be at least 2")
    return parsed


def _load_env() -> None:
    if getattr(sys, "frozen", False):
        env_path = Path(sys.executable).parent / ".env"
    else:
        env_path = Path.cwd() / ".env"
    load_dotenv(env_path)


def _emit_response(
    response: AgentModuleResponse | AgentModuleErrorResponse,
    output: Path | None,
) -> None:
    text = json.dumps(response.model_dump(), ensure_ascii=False)
    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise InternalError("E901", f"Could not write output file {output}: {type(exc).__name__}") from exc
    print(text)


if __name__ == "__main__":
    raise SystemExit(main())
