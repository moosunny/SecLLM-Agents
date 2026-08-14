from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain.agents.middleware import hook_config
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from agents.config import MAX_FIX_ATTEMPTS, MAX_FIX_TURNS
from agents.error_handling import GenerationError
from agents.graph.forced_tool import ForcedToolMiddleware, forced_tool_choice
from agents.graph.state import FixCandidate

FORCED_SUBMIT_TOOL_NAME = "edit_src_code"
FORCED_SUBMIT_TOOL_CHOICE = forced_tool_choice(FORCED_SUBMIT_TOOL_NAME)
REVIEW_APPROVED = "REVIEW_APPROVED"
REVIEW_REJECTED = "REVIEW_REJECTED"
NO_VALID_FIX_MESSAGE = "No valid fix candidates were submitted."


def no_valid_fix_error() -> GenerationError:
    """Build the shared E301 error while preserving the wire-stage contract."""
    return GenerationError("E301", NO_VALID_FIX_MESSAGE)


def _covers_line(candidate: FixCandidate, line: int) -> bool:
    """Return whether any modification in the candidate touches the given line."""
    for modification in candidate["modifications"]:
        start = int(modification.get("startLine") or 0)
        if modification.get("type") == "delete":
            end = int(modification.get("endLine") or start)
            if start <= line <= end:
                return True
        elif start == line:
            return True
    return False


class AgentInvoker(Protocol):
    def invoke(
        self,
        payload: Mapping[str, Sequence[tuple[str, str]]],
        config: RunnableConfig | None = None,
    ) -> Mapping[str, Sequence[BaseMessage]]:
        ...


@dataclass(slots=True)
class FixSubmissionHolder:
    """Mutable accumulator owned by one LangChain fix-agent invocation."""

    sink_line: int | None = None
    attempts: int = 0
    model_turns: int = 0
    accepted_candidate: FixCandidate | None = None
    last_valid_candidate: FixCandidate | None = None
    review_pending: bool = False
    review_completed: bool = False
    review_rejected: bool = False
    review_protocol_error: bool = False
    tool_protocol_error: bool = False
    tool_call_pending: bool = False
    missing_initial_tool: bool = False
    rejection_errors: list[list[str]] = field(default_factory=list)
    attempt_records: list[dict[str, object]] = field(default_factory=list)

    def record(
        self,
        candidate: FixCandidate,
        errors: Sequence[str],
        *,
        submission: Mapping[str, object] | None = None,
        rejection_feedback: str | None = None,
    ) -> str:
        self.attempt_records.append(
            {
                "submission": dict(submission or {}),
                "errors": list(errors),
            }
        )
        if errors:
            self.rejection_errors.append(list(errors))
            response = "Submission rejected: " + "; ".join(errors)
            if rejection_feedback:
                response += "\n\n" + rejection_feedback
            return response
        previous_valid = self.last_valid_candidate
        self.accepted_candidate = candidate
        self.last_valid_candidate = candidate
        self.review_pending = True
        self.review_completed = False
        self.review_rejected = False
        self.review_protocol_error = False
        message = self.acceptance_message()
        if (
            self.sink_line is not None
            and previous_valid is not None
            and _covers_line(previous_valid, self.sink_line)
            and not _covers_line(candidate, self.sink_line)
        ):
            message += (
                "\nWarning: this edit does not modify flagged line "
                f"{self.sink_line}, and it fully REPLACES your earlier accepted "
                "edit that did — only one submission survives. If both changes "
                "are needed, call edit_src_code once with a single range that "
                "covers the flagged line and contains every needed change."
            )
        return message

    def is_accepted(self) -> bool:
        return self.accepted_candidate is not None

    def is_reviewed(self) -> bool:
        return (
            self.accepted_candidate is not None
            and self.review_completed
            and not self.tool_protocol_error
        )

    def is_submittable(self) -> bool:
        """Return whether the latest edit passed tool parsing and code guards."""
        return self.accepted_candidate is not None and not self.tool_protocol_error

    def submittable_candidate(self) -> FixCandidate | None:
        """Return the current candidate, else the last one that passed the guards.

        A revision tool call clears ``accepted_candidate`` before the replacing
        submission reaches the guards, so a rejected revision must not discard an
        edit that was already guard-valid. A tool-protocol violation likewise ends
        the loop without discarding earlier guard-valid work: every candidate held
        here passed the guards individually, so shipping one is always safe.
        """
        return self.accepted_candidate or self.last_valid_candidate

    def acceptance_message(self) -> str:
        remaining = max(0, MAX_FIX_ATTEMPTS - self.attempts)
        correction_instruction = (
            "If it must be corrected, call edit_src_code once more with the full "
            "corrected replacement; the latest accepted submission replaces the "
            "earlier one."
            if remaining
            else (
                "No mutation submissions remain. If the edit is defective, reply "
                f"with exactly {REVIEW_REJECTED} and nothing else."
            )
        )
        return (
            "Submission accepted.\n"
            "Before finishing, review your accepted edit once:\n"
            "1. Is the replacement syntactically complete in its surrounding context?\n"
            "2. Does it remediate the flagged issue at the sink line?\n"
            "3. Does it avoid unrelated changes?\n"
            "Never reconsider whether the issue itself is real.\n"
            f"If the edit is appropriate, reply with exactly {REVIEW_APPROVED} "
            "and nothing else.\n"
            f"{correction_instruction}"
        )

    def begin_revision(self) -> None:
        """Invalidate the unreviewed candidate before a corrective tool call.

        ``last_valid_candidate`` survives so a revision that never reaches the
        guards leaves the previous guard-valid edit submittable.
        """
        self.accepted_candidate = None
        self.review_pending = False
        self.review_completed = False
        self.review_rejected = False
        self.review_protocol_error = False

    def record_review_response(self, message: AIMessage) -> None:
        """Apply the strict no-extra-tool review protocol to one model response."""
        if not self.review_pending:
            return
        if message.tool_calls:
            self.begin_revision()
            return
        content = message.text.strip()
        self.review_pending = False
        if content == REVIEW_APPROVED:
            self.review_completed = True
            return
        if content == REVIEW_REJECTED:
            self.review_rejected = True
            return
        self.review_protocol_error = True


@dataclass(frozen=True, slots=True)
class FixAgentRuntime:
    agent: AgentInvoker
    holder: FixSubmissionHolder

    def invoke(
        self,
        payload: Mapping[str, Sequence[tuple[str, str]]],
        *,
        callbacks: Sequence[BaseCallbackHandler] = (),
    ) -> Mapping[str, Sequence[BaseMessage]]:
        config = _callback_config(callbacks)
        if config is None:
            return self.agent.invoke(payload)
        return self.agent.invoke(payload, config=config)

    def submitted_candidates(self) -> list[FixCandidate]:
        candidate = self.holder.submittable_candidate()
        if candidate is None:
            return []
        return [candidate]


def _callback_config(callbacks: Sequence[BaseCallbackHandler]) -> RunnableConfig | None:
    if not callbacks:
        return None
    return {"callbacks": list(callbacks)}


class ForcedFixToolMiddleware(ForcedToolMiddleware):
    """Fix-stage specialization of the generic forced-tool middleware.

    The edit tool is forced only until the first submission is accepted. After
    acceptance the loop relaxes to ``tool_choice`` auto so the model reviews
    its own edit: exact ``REVIEW_APPROVED`` text approves it, while another
    ``edit_src_code`` call invalidates the unreviewed candidate and submits a
    revision. Mutation attempts and total model turns are bounded separately.
    At the bound, the latest tool- and guard-valid candidate remains
    submittable even without review approval — including when the revision that
    replaced it was itself rejected by the guards.
    """

    def __init__(
        self,
        holder: FixSubmissionHolder,
        *,
        max_turns: int = MAX_FIX_TURNS,
        force_initial_tool: bool = True,
    ) -> None:
        if max_turns < 2:
            raise ValueError("max_turns must be at least 2.")
        super().__init__(
            FORCED_SUBMIT_TOOL_NAME,
            holder,
            max_attempts=MAX_FIX_ATTEMPTS,
        )
        self.max_turns = max_turns
        self.force_initial_tool = force_initial_tool

    def wrap_model_call(self, request, handler):
        reviewing = self.holder.review_pending
        model_settings = {
            **(request.model_settings or {}),
            "parallel_tool_calls": False,
        }
        if reviewing:
            tool_choice = (
                "auto"
                if self.holder.attempts < self.max_attempts
                else "none"
            )
        else:
            tool_choice = (
                self.forced_tool_choice if self.force_initial_tool else "auto"
            )
        response = handler(
            request.override(tool_choice=tool_choice, model_settings=model_settings)
        )
        self.holder.model_turns += 1
        ai_messages = [
            message for message in response.result if isinstance(message, AIMessage)
        ]
        if not ai_messages:
            self.holder.tool_protocol_error = True
            self.holder.tool_call_pending = False
            return response
        message = ai_messages[-1]
        self.holder.tool_call_pending = bool(message.tool_calls)
        self.holder.missing_initial_tool = (
            not reviewing
            and not message.tool_calls
            and not self.holder.is_accepted()
        )
        edit_calls = [
            call
            for call in message.tool_calls
            if call.get("name") == FORCED_SUBMIT_TOOL_NAME
        ]
        self.holder.attempts += len(edit_calls)
        if len(message.tool_calls) != len(edit_calls) or len(edit_calls) > 1:
            self.holder.tool_protocol_error = True
        if reviewing:
            self.holder.record_review_response(message)
        return response

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, str] | None:
        del state, runtime
        if self.holder.is_reviewed():
            return {"jump_to": "end"}
        if self.holder.tool_protocol_error:
            return {"jump_to": "end"}
        if self.holder.model_turns >= self.max_turns:
            return {"jump_to": "end"}
        if self.holder.review_pending:
            return None
        if self.holder.attempts >= self.max_attempts:
            return {"jump_to": "end"}
        return None

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime) -> dict[str, Any] | None:
        """Continue revising after a non-approved text review when budget remains."""
        del state, runtime
        if self.holder.is_reviewed() or self.holder.tool_protocol_error:
            return None
        if self.holder.model_turns >= self.max_turns:
            return None
        if self.holder.attempts >= self.max_attempts:
            return None
        if self.holder.tool_call_pending:
            return None
        if not self.force_initial_tool and self.holder.missing_initial_tool:
            return {
                "messages": [
                    HumanMessage(
                        content=(
                            "Your previous response did not call edit_src_code. "
                            "Call edit_src_code now with a concrete fix; do not "
                            "answer with plain text."
                        )
                    )
                ],
                "jump_to": "model",
            }
        if self.holder.is_submittable() and not self.holder.review_pending:
            return {"jump_to": "model"}
        return None

    def after_agent(self, state, runtime) -> None:
        """Fail unless the bounded loop produced a guard-valid edit."""
        del state, runtime
        if self.holder.submittable_candidate() is None:
            raise no_valid_fix_error()
