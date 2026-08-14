from __future__ import annotations

import time
from collections.abc import Sequence

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from agents.graph.chunking import CodeLanguageHint, resolve_language
from agents.graph.explore import prepare_explore_state
from agents.graph.fix import CandidateProvider, LLMCandidateProvider, collect_branch_candidate
from agents.graph.select import finalize_response
from agents.graph.state import GraphState
from agents.prompt_loader import prompt_version
from agents.schema import AgentModuleRequest, AgentModuleResponse
from agents.tracing import (
    langfuse_handlers,
    observed_span,
    shutdown_langfuse,
    trace_attributes,
    trace_metadata,
)


def run_graph(
    request: AgentModuleRequest,
    *,
    base_url_override: str | None = None,
    candidate_provider: CandidateProvider | None = None,
    max_fix_turns: int | None = None,
) -> AgentModuleResponse:
    start = time.perf_counter()
    state = _initial_state(request)
    callbacks = langfuse_handlers()
    if candidate_provider is None:
        provider = (
            LLMCandidateProvider(
                request,
                base_url_override=base_url_override,
                callbacks=callbacks,
            )
            if max_fix_turns is None
            else LLMCandidateProvider(
                request,
                base_url_override=base_url_override,
                callbacks=callbacks,
                max_turns=max_fix_turns,
            )
        )
    else:
        provider = candidate_provider
    graph = _build_graph(
        provider,
        base_url_override=base_url_override,
        callbacks=callbacks,
    )
    try:
        config = _callback_config(callbacks)
        # Trace attributes are not injectable through the LangChain callback
        # config in the OTel-based SDK: they belong to the active span and
        # propagate to its children. Opening a root span and wrapping the whole
        # invoke is what binds session/tags/metadata to the same trace the
        # generation spans land in.
        with observed_span("active-suggestion") as span, trace_attributes(**_trace_attributes(request)):
            final_state = (
                graph.invoke(state) if config is None else graph.invoke(state, config=config)
            )
            span.update(output={"error": final_state.get("error")})
    finally:
        if callbacks:
            shutdown_langfuse()
    final_state["timings"]["graph_elapsed_time"] = time.perf_counter() - start
    return finalize_response(final_state)


def _trace_attributes(request: AgentModuleRequest) -> dict[str, object]:
    """Trace-level facts, all sourced from the request schema."""
    user_request = request.user_request
    return {
        "session_id": f"{request.user_id}:{request.request_idx}",
        "tags": ["explore", "active-suggestion"],
        "metadata": trace_metadata(
            {
                "checker_id": user_request.checker_id,
                # Korean first: the checker taxonomy the scorer segments by is
                # keyed on the Korean issue names, so an English-first name here
                # would silently miss the lookup and drop the trace into the
                # "unknown" category — the one segment nobody reads.
                "issue_name": user_request.issue_name_ko or user_request.issue_name_en,
                # The *resolved* language, not the raw request hint: slice
                # production rates are only comparable per grammar.
                "language": resolve_language(
                    CodeLanguageHint(
                        programming_language=user_request.programming_language,
                        file_extension=user_request.file_extension,
                    )
                ),
                "locale": user_request.locale,
                "model": request.model_name,
                "explore_prompt_version": prompt_version("explore"),
                "fix_prompt_version": prompt_version("fix"),
            }
        ),
    }


def _build_graph(
    candidate_provider: CandidateProvider,
    *,
    base_url_override: str | None,
    callbacks: Sequence[BaseCallbackHandler],
):
    graph = StateGraph(GraphState)
    graph.add_node(
        "explore",
        lambda state: prepare_explore_state(
            state,
            base_url_override=base_url_override,
            callbacks=callbacks,
        ),
    )
    graph.add_node(
        "fix_branch",
        lambda state: collect_branch_candidate(
            state,
            candidate_provider=candidate_provider,
        ),
    )
    graph.add_edge(START, "explore")
    graph.add_edge("explore", "fix_branch")
    graph.add_edge("fix_branch", END)
    return graph.compile()


def _callback_config(callbacks: Sequence[BaseCallbackHandler]) -> RunnableConfig | None:
    if not callbacks:
        return None
    return {"callbacks": list(callbacks)}


def _initial_state(request: AgentModuleRequest) -> GraphState:
    return GraphState(
        request=request,
        branch_id=0,
        language="",
        code_lines=[],
        chunks=[],
        issue_line_number=0,
        explore_context="",
        full_code_inline=False,
        sink_anchor={},
        source_finding=None,
        explore_trajectory=None,
        candidates=[],
        selected=None,
        timings={
            "explore_elapsed_time": 0.0,
            "fix_elapsed_time": 0.0,
            "select_elapsed_time": 0.0,
            "graph_elapsed_time": 0.0,
        },
        error=None,
    )
