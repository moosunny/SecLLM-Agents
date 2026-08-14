from __future__ import annotations

from typing import Literal, assert_never

from agents.schema import AgentModuleErrorResponse, AgentModuleRequest

type ErrorCode = Literal["E101", "E102", "E103", "E104", "E201", "E202", "E301", "E901", "E999"]
type ErrorStage = Literal["input", "explore", "fix", "select", "output"]
type InputErrorCode = Literal["E101", "E102", "E103", "E104"]
type LLMConnectionErrorCode = Literal["E201", "E202"]
type GenerationErrorCode = Literal["E301"]
type InternalErrorCode = Literal["E901", "E999"]


class ActiveSuggestionError(Exception):
    error_code: ErrorCode
    error_stage: ErrorStage
    message: str

    def __init__(self, error_code: ErrorCode, error_stage: ErrorStage, message: str) -> None:
        self.error_code = error_code
        self.error_stage = error_stage
        self.message = message
        Exception.__init__(self, message)

    def __str__(self) -> str:
        return self.message


class InputError(ActiveSuggestionError):
    def __init__(self, error_code: InputErrorCode, message: str) -> None:
        ActiveSuggestionError.__init__(self, error_code, "input", message)


class LLMConnectionError(ActiveSuggestionError):
    def __init__(self, error_code: LLMConnectionErrorCode, message: str) -> None:
        match error_code:
            case "E201":
                stage: ErrorStage = "explore"
            case "E202":
                stage = "fix"
            case unreachable:
                assert_never(unreachable)
        ActiveSuggestionError.__init__(self, error_code, stage, message)


class GenerationError(ActiveSuggestionError):
    def __init__(self, error_code: GenerationErrorCode, message: str) -> None:
        ActiveSuggestionError.__init__(self, error_code, "select", message)


class InternalError(ActiveSuggestionError):
    def __init__(self, error_code: InternalErrorCode, message: str) -> None:
        ActiveSuggestionError.__init__(self, error_code, "output", message)


def error_response_from_exception(
    error: BaseException,
    request: AgentModuleRequest | None = None,
) -> AgentModuleErrorResponse:
    if isinstance(error, ActiveSuggestionError):
        return AgentModuleErrorResponse(
            user_id=_request_user_id(request),
            request_idx=_request_idx(request),
            error_code=error.error_code,
            error_stage=error.error_stage,
            error_message=error.message,
        )
    return AgentModuleErrorResponse(
        user_id=_request_user_id(request),
        request_idx=_request_idx(request),
        error_code="E999",
        error_stage="output",
        error_message=type(error).__name__,
    )


def _request_user_id(request: AgentModuleRequest | None) -> int | None:
    if request is None:
        return None
    return request.user_id


def _request_idx(request: AgentModuleRequest | None) -> int | None:
    if request is None:
        return None
    return request.request_idx
