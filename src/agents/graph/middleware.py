from __future__ import annotations

from typing import Final

from langchain.agents.middleware import ToolCallLimitMiddleware, ToolRetryMiddleware

try:
    from agents.config import MAX_EXPLORE_ITERATIONS
except ImportError:
    MAX_EXPLORE_ITERATIONS: Final = 2


def create_explore_middleware() -> tuple[ToolCallLimitMiddleware, ToolRetryMiddleware]:
    # exit_behavior="end" makes the Phase 2 investigation loop stop deterministically
    # at MAX_EXPLORE_ITERATIONS instead of letting a non-submitting model spin until
    # the langgraph recursion limit. The loop then fails open (unresolved).
    return (
        ToolCallLimitMiddleware(run_limit=MAX_EXPLORE_ITERATIONS, exit_behavior="end"),
        ToolRetryMiddleware(max_retries=1),
    )
