"""Reusable ``tool_choice``-forcing agent middleware.

A single named tool is forced on every model call, parallel tool calls are
disabled, and the agent loop terminates when a valid submission is accepted or
the bounded attempt count is exhausted.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from langchain.agents.middleware import AgentMiddleware, hook_config


def forced_tool_choice(tool_name: str) -> dict[str, object]:
    """Return the OpenAI ``tool_choice`` payload that pins one tool."""
    return {"type": "function", "function": {"name": tool_name}}


@runtime_checkable
class SubmissionHolder(Protocol):
    """State the middleware inspects to decide when to stop the agent loop."""

    attempts: int

    def is_accepted(self) -> bool:
        """True once a valid submission has been recorded."""
        ...


class ForcedToolMiddleware(AgentMiddleware):
    """Force one tool and jump to ``end`` on acceptance or attempt exhaustion."""

    def __init__(
        self,
        tool_name: str,
        holder: SubmissionHolder,
        *,
        max_attempts: int,
    ) -> None:
        super().__init__()
        self.holder = holder
        self.max_attempts = max_attempts
        self.forced_tool_choice = forced_tool_choice(tool_name)

    def wrap_model_call(self, request, handler):
        model_settings = {
            **(request.model_settings or {}),
            "parallel_tool_calls": False,
        }
        return handler(
            request.override(
                tool_choice=self.forced_tool_choice,
                model_settings=model_settings,
            )
        )

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, str] | None:
        if self.holder.is_accepted():
            return {"jump_to": "end"}
        if self.holder.attempts >= self.max_attempts:
            return {"jump_to": "end"}
        return None


class SubmissionTerminationMiddleware(AgentMiddleware):
    """Stop a free-choice tool loop once a submission is accepted.

    Unlike :class:`ForcedToolMiddleware`, this does not pin ``tool_choice`` — the
    Phase 2 investigation loop must be free to call its investigation tools
    (resolve_identifier/read_code) before it submits. It only short-circuits to
    ``end`` after the submission tool records a result; the surrounding
    tool-call limit handles exhaustion.
    """

    def __init__(self, holder: SubmissionHolder) -> None:
        super().__init__()
        self.holder = holder

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, str] | None:
        if self.holder.is_accepted():
            return {"jump_to": "end"}
        return None
