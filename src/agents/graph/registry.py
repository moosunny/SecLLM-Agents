from __future__ import annotations

import os
from typing import Any, Final, Literal, Protocol, assert_never

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI as _BaseChatOpenAI


class ChatOpenAI(_BaseChatOpenAI):
    """Preserve reasoning fields returned by OpenAI-compatible chat servers."""

    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict[str, Any] | None = None,
    ):
        result = super()._create_chat_result(response, generation_info)
        response_dict = (
            response if isinstance(response, dict) else response.model_dump()
        )
        choices = response_dict.get("choices") or []
        for generation, choice in zip(result.generations, choices, strict=False):
            raw_message = choice.get("message") or {}
            provider_fields = raw_message.get("provider_specific_fields") or {}
            reasoning = (
                raw_message.get("reasoning_content")
                or raw_message.get("reasoning")
                or provider_fields.get("reasoning")
            )
            if reasoning and isinstance(generation.message, AIMessage):
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

from agents.error_handling import InputError

try:
    from agents.config import (
        CLAUDE_MODEL_MARKER,
        EXPLORE_MAX_TOKENS,
        FIX_MAX_TOKENS,
        FIX_SRC_CODE_TIMEOUT,
        GPT_OSS_MODEL_MARKER,
        GPT_OSS_TEMPERATURE,
        GPT_OSS_TOP_P,
        SAMPLING_PRESETS,
    )
except ImportError:
    CLAUDE_MODEL_MARKER: Final = "claude"
    EXPLORE_MAX_TOKENS: Final = 2048
    FIX_MAX_TOKENS: Final = 4096
    FIX_SRC_CODE_TIMEOUT: Final = 90.0
    GPT_OSS_MODEL_MARKER: Final = "gpt-oss"
    GPT_OSS_TEMPERATURE: Final = 1.0
    GPT_OSS_TOP_P: Final = 1.0
    SAMPLING_PRESETS: Final = {
        "instruct_general": {
            "temperature": 0.7,
            "extra_body": {
                "top_p": 0.8,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 1.5,
                "repetition_penalty": 1.0,
            },
        },
    }

LLMStage = Literal["explore", "fix"]
LLM_SAMPLING_PRESET_NAME: Final = "instruct_general"


class LLMRegistryError(InputError):
    def __init__(self, message: str) -> None:
        InputError.__init__(self, "E104", message)


class UserRequestLike(Protocol):
    APIKEY: str


class ModuleRequestLike(Protocol):
    model: str
    provider: str
    serviceHost: str
    servicePort: str
    userRequest: UserRequestLike


def create_chat_model(
    request: ModuleRequestLike,
    *,
    stage: LLMStage,
    base_url_override: str | None = None,
    max_completion_tokens_override: int | None = None,
    timeout_override: float | None = None,
    extra_body_override: dict[str, object] | None = None,
) -> BaseChatModel:
    if request.provider != "openai":
        raise LLMRegistryError("Only the openai provider is supported.")

    base_url = _resolve_base_url(
        base_url_override=base_url_override,
        service_host=request.serviceHost,
        service_port=request.servicePort,
    )
    temperature, extra_body = _resolve_sampling(request.model)
    return ChatOpenAI(
        model=request.model,
        base_url=base_url,
        api_key=_resolve_api_key(request.userRequest),
        temperature=temperature,
        extra_body={
            **extra_body,
            **(extra_body_override or {}),
        },
        max_completion_tokens=(
            max_completion_tokens_override
            if max_completion_tokens_override is not None
            else _max_completion_tokens(stage)
        ),
        timeout=(
            timeout_override
            if timeout_override is not None
            else FIX_SRC_CODE_TIMEOUT
        ),
    )


def _resolve_sampling(model: str) -> tuple[float | None, dict[str, object]]:
    """gpt-oss takes temperature/top_p, Claude no sampling knobs; others the preset.

    Claude 5 deprecates ``temperature``; ``None`` keeps it out of the payload.
    """
    lowered = model.lower()
    if GPT_OSS_MODEL_MARKER in lowered:
        return GPT_OSS_TEMPERATURE, {"top_p": GPT_OSS_TOP_P}
    if CLAUDE_MODEL_MARKER in lowered:
        return None, {}
    preset = SAMPLING_PRESETS[LLM_SAMPLING_PRESET_NAME]
    return preset["temperature"], dict(preset["extra_body"])


def _resolve_api_key(user_request: UserRequestLike) -> str:
    return user_request.APIKEY or os.getenv("LITELLM_MASTER_KEY", "") or "EMPTY"


def _max_completion_tokens(stage: LLMStage) -> int:
    match stage:
        case "explore":
            return EXPLORE_MAX_TOKENS
        case "fix":
            return FIX_MAX_TOKENS
        case unreachable:
            assert_never(unreachable)


def _resolve_base_url(
    *,
    base_url_override: str | None,
    service_host: str,
    service_port: str,
) -> str:
    if base_url_override:
        return base_url_override
    if service_host and service_port:
        return f"{service_host.rstrip('/')}:{service_port.lstrip('/')}"
    raise LLMRegistryError(
        "A base URL is required via --base-url or serviceHost/servicePort."
    )
