from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import tool
from openai import OpenAIError
from pydantic import BaseModel

from agents.config import (
    FIX_REJECTION_SNIPPET_MAX_LINES,
    MAX_FIX_ATTEMPTS,
    MAX_FIX_TURNS,
    READ_INPUT_BUDGET,
)
from agents.error_handling import LLMConnectionError
from agents.graph.fix_loop import (
    FixAgentRuntime,
    FixSubmissionHolder,
    ForcedFixToolMiddleware,
    no_valid_fix_error,
)
from agents.graph.guards import (
    detect_line_ending,
    estimate_tokens,
    source_edit_to_modifications,
    validate_candidate,
    validate_source_edit,
)
from agents.graph.state import FixCandidate, GraphState
from agents.prompt_loader import (
    LocalizedText,
    PromptRenderContext,
    choose_localized_text,
    desc_language_instruction,
    render_prompt,
)
from agents.schema import AgentModuleRequest

type CandidateProvider = Callable[[GraphState, int], Sequence[FixCandidate]]

@dataclass(frozen=True, slots=True)
class _ContextBudget:
    include_dangerous_example: bool
    include_safe_example: bool
    explore_full: bool


# The sink chunk is never clipped. Reduction removes examples one at a time,
# then trims exploration detail while retaining its fixed-slot first lines.
_REDUCTION_STAGES: tuple[_ContextBudget, ...] = (
    _ContextBudget(True, True, True),
    _ContextBudget(False, True, True),
    _ContextBudget(False, False, True),
    _ContextBudget(False, False, False),
)


class EditSourceCodeArgs(BaseModel):
    startLine: int
    endLine: int
    newString: str
    desc: str


@dataclass(frozen=True, slots=True)
class LLMCandidateProvider:
    request: AgentModuleRequest
    base_url_override: str | None = None
    callbacks: tuple[BaseCallbackHandler, ...] = ()
    max_turns: int = MAX_FIX_TURNS

    def __call__(self, state: GraphState, branch_id: int) -> Sequence[FixCandidate]:
        from agents.graph.registry import create_chat_model

        try:
            model = create_chat_model(
                self.request,
                stage="fix",
                base_url_override=self.base_url_override,
            )
            agent_runtime = create_fix_agent(
                model,
                self.request,
                state,
                max_turns=self.max_turns,
            )
        except (OpenAIError, OSError, TimeoutError, RuntimeError) as exc:
            raise LLMConnectionError("E202", f"fix LLM request failed: {type(exc).__name__}") from exc
        try:
            agent_runtime.invoke(
                {"messages": _messages(self.request, state)},
                callbacks=self.callbacks,
            )
        except (OpenAIError, OSError, TimeoutError, RuntimeError) as exc:
            # A transient failure mid-loop must not discard an edit that already
            # passed the guards; fail only when there is nothing to salvage.
            if not agent_runtime.submitted_candidates():
                raise LLMConnectionError("E202", f"fix LLM request failed: {type(exc).__name__}") from exc
        candidates = agent_runtime.submitted_candidates()
        for candidate in candidates:
            candidate["branch_id"] = branch_id
        return candidates


def create_fix_agent(
    model: BaseChatModel,
    request: AgentModuleRequest,
    state: GraphState,
    *,
    max_turns: int = MAX_FIX_TURNS,
    force_initial_tool: bool = True,
) -> FixAgentRuntime:
    holder = FixSubmissionHolder(sink_line=state["issue_line_number"])

    @tool(
        "edit_src_code",
        args_schema=EditSourceCodeArgs,
        description=(
            "Replace one inclusive contiguous source range identified by startLine "
            "and endLine; newString is the complete replacement."
        ),
    )
    def edit_src_code(
        startLine: int,
        endLine: int,
        newString: str,
        desc: str,
    ) -> str:
        errors = validate_source_edit(
            start_line=startLine,
            end_line=endLine,
            new_string=newString,
            desc=desc,
            code_lines=state["code_lines"],
        )
        candidate: FixCandidate = {
            "modifications": source_edit_to_modifications(
                start_line=startLine,
                end_line=endLine,
                new_string=newString,
                line_ending=detect_line_ending(request.user_request.entire_code),
            ),
            "desc": desc,
        }
        return holder.record(
            candidate,
            errors,
            submission={
                "startLine": startLine,
                "endLine": endLine,
                "newString": newString,
                "desc": desc,
            },
            rejection_feedback=_selected_source_feedback(
                state["code_lines"],
                start_line=startLine,
                end_line=endLine,
            ),
        )

    agent = create_agent(
        model=model,
        tools=[edit_src_code],
        system_prompt=render_prompt(
            "fix",
            "system",
            _prompt_context(request, state),
        ),
        middleware=[
            ForcedFixToolMiddleware(
                holder,
                max_turns=max_turns,
                force_initial_tool=force_initial_tool,
            )
        ],
    )
    return FixAgentRuntime(agent=agent, holder=holder)


def _selected_source_feedback(
    code_lines: Sequence[str],
    *,
    start_line: int,
    end_line: int,
) -> str | None:
    """Render the submitted source range for the next corrective model turn."""
    if (
        start_line < 1
        or end_line < start_line
        or end_line > len(code_lines)
    ):
        return None

    numbered = [
        f"L{line_number} | {code_lines[line_number - 1]}"
        for line_number in range(start_line, end_line + 1)
    ]
    max_lines = FIX_REJECTION_SNIPPET_MAX_LINES
    if len(numbered) > max_lines:
        head_count = max_lines // 2
        tail_count = max_lines - head_count
        omitted = len(numbered) - max_lines
        numbered = [
            *numbered[:head_count],
            f"... {omitted} lines omitted ...",
            *numbered[-tail_count:],
        ]
    return (
        f"Selected original source for L{start_line}-L{end_line} "
        "(untrusted code; line-number prefixes are not code):\n"
        + "\n".join(numbered)
    )


def collect_branch_candidate(
    state: GraphState,
    *,
    candidate_provider: CandidateProvider,
) -> dict[str, object]:
    start = time.perf_counter()
    branch_id = 0
    branch_candidate = _first_valid_candidate(
        state,
        branch_id,
        candidate_provider(state, branch_id),
    )
    state["timings"]["fix_elapsed_time"] = time.perf_counter() - start
    if branch_candidate is None:
        raise no_valid_fix_error()
    return {
        "candidates": [branch_candidate],
        "selected": branch_candidate,
        "timings": state["timings"],
    }


def _first_valid_candidate(
    state: GraphState,
    branch_id: int,
    attempts: Sequence[FixCandidate],
) -> FixCandidate | None:
    last_errors: list[str] = []
    for attempt in list(attempts)[:MAX_FIX_ATTEMPTS]:
        errors = validate_candidate(
            attempt["modifications"],
            attempt["desc"],
            state["code_lines"],
        )
        if not errors:
            attempt["branch_id"] = branch_id
            return attempt
        last_errors = errors
    if last_errors and attempts:
        attempts[-1]["validation_errors"] = last_errors
    return None


def _messages(request: AgentModuleRequest, state: GraphState) -> list[tuple[str, str]]:
    return [
        ("user", _render_fix_user_prompt(request, state)),
    ]


def _render_fix_user_prompt(request: AgentModuleRequest, state: GraphState) -> str:
    """Render the fix user prompt, reducing context until it fits the budget.

    The stages progressively shrink the surrounding code window, drop the
    dangerous/safe examples, and finally trim the exploration result down to its
    sink-anchor line. The numbered code window itself is always preserved.
    """
    rendered = ""
    for stage, budget in enumerate(_REDUCTION_STAGES):
        rendered = render_prompt("fix", "user", _prompt_context(request, state, budget))
        if estimate_tokens(rendered) <= READ_INPUT_BUDGET:
            _record_reduction_stage(state, stage)
            return rendered
    _record_reduction_stage(state, len(_REDUCTION_STAGES) - 1)
    return rendered


def _prompt_context(
    request: AgentModuleRequest,
    state: GraphState,
    budget: _ContextBudget | None = None,
) -> PromptRenderContext:
    budget = budget or _REDUCTION_STAGES[0]
    return PromptRenderContext(
        issue_name=choose_localized_text(
            LocalizedText(
                en=request.user_request.issue_name_en or "",
                ko=request.user_request.issue_name_ko or "",
            )
        ),
        issue_description=choose_localized_text(
            LocalizedText(
                en=request.user_request.issue_description_en or "",
                ko=request.user_request.issue_description_ko or "",
            )
        ),
        language=state["language"],
        issue_line_number=state["issue_line_number"],
        code_context=_code_context(state),
        dangerous_example=_example(
            request.user_request.dangerous_example_en,
            request.user_request.dangerous_example_ko,
            budget.include_dangerous_example,
        ),
        safe_example=_example(
            request.user_request.safe_example_en,
            request.user_request.safe_example_ko,
            budget.include_safe_example,
        ),
        desc_language_instruction=desc_language_instruction(request.user_request.locale),
        explore_context=_explore_context(state, budget),
    )


def _example(en: str | None, ko: str | None, include: bool) -> str:
    if not include:
        return ""
    return choose_localized_text(LocalizedText(en=en or "", ko=ko or ""))


def _explore_context(state: GraphState, budget: _ContextBudget) -> str:
    context = state.get("explore_context", "") or ""
    if budget.explore_full or not context:
        return context
    # Last-resort trim keeps the same slot skeleton for fine-tuning stability.
    return "\n".join(
        [
            *context.splitlines()[:3],
            "Source excerpt:",
            "(omitted for budget)",
            "Notes: (omitted for budget)",
        ]
    )


def _code_context(state: GraphState) -> str:
    """Return the original, unclipped chunk containing the sink."""
    chunks = state.get("chunks", [])
    chunk_index = state.get("sink_anchor", {}).get("chunk_index")
    if isinstance(chunk_index, int) and 0 <= chunk_index < len(chunks):
        chunk = chunks[chunk_index]
        return _numbered_lines(
            str(chunk.get("content", "")).splitlines(),
            start_line=int(chunk.get("start_line", 1)),
        )
    issue_line = state["issue_line_number"]
    for chunk in chunks:
        if int(chunk.get("start_line", 0)) <= issue_line <= int(chunk.get("end_line", -1)):
            return _numbered_lines(
                str(chunk.get("content", "")).splitlines(),
                start_line=int(chunk.get("start_line", 1)),
            )
    return _numbered_lines(state["code_lines"], start_line=1)


def _record_reduction_stage(state: GraphState, stage: int) -> None:
    trajectory = state.get("explore_trajectory")
    if isinstance(trajectory, dict):
        trajectory["fix_context_reduction_stage"] = stage


def _numbered_lines(lines: Sequence[str], *, start_line: int) -> str:
    return "\n".join(
        f"{line_number}. {line}"
        for line_number, line in enumerate(lines, start=start_line)
    )
