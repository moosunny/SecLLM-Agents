"""격리 프로젝트의 모델 팩토리 — 기존 registry 계약 미러 (설계 §5.1, §3).

`src/agents/graph/registry.py`의 계약을 유지한다:
- openai provider 전용 (위반 시 ModelFactoryError → 러너가 E104/input으로 매핑)
- base URL 우선순위: override > serviceHost:servicePort
- 샘플링 카브아웃: gpt-oss와 DeepSeek V4 Flash는 temperature/top_p만,
  claude 계열은 노브 전무
  (vLLM extra_body 전송 시 400 — 2026-07-29 스모크), 그 외 instruct_general 프리셋
- API key: 요청 APIKEY > LITELLM_MASTER_KEY > "EMPTY"
"""

from __future__ import annotations

import os
from typing import Any, Final

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI as _BaseChatOpenAI

from deepagents_pipeline.contracts import AgentModuleRequest


class ChatOpenAI(_BaseChatOpenAI):
    """reasoning 필드 보존 — 프로덕션 registry.py:11-33 미러 (arm 간 대칭성)."""

    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict[str, Any] | None = None,
    ):
        result = super()._create_chat_result(response, generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump()
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

GPT_OSS_MODEL_MARKER: Final = "gpt-oss"
GPT_OSS_TEMPERATURE: Final = 1.0
GPT_OSS_TOP_P: Final = 1.0
DEEPSEEK_V4_FLASH_0731_MODEL_MARKER: Final = "deepseek-v4-flash-0731"
DEEPSEEK_V4_FLASH_0731_TEMPERATURE: Final = 1.0
DEEPSEEK_V4_FLASH_0731_TOP_P: Final = 0.95
CLAUDE_MODEL_MARKER: Final = "claude"

# qwen3.5 계열 권장 샘플링 (2026-08-20 사용자 지정) — lora 파생 모델명도 포함된다
QWEN35_MODEL_MARKER: Final = "qwen3.5"
QWEN35_TEMPERATURE: Final = 1.0
QWEN35_EXTRA_BODY: Final[dict[str, object]] = {
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "repetition_penalty": 1.0,
}

# qwen3.8 계열 권장 샘플링 (2026-08-19 사용자 지정)
QWEN38_MODEL_MARKER: Final = "qwen3.8"
QWEN38_TEMPERATURE: Final = 1.0
QWEN38_EXTRA_BODY: Final[dict[str, object]] = {
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 0.0,
    "repetition_penalty": 1.0,
}

INSTRUCT_GENERAL_TEMPERATURE: Final = 0.7
INSTRUCT_GENERAL_EXTRA_BODY: Final[dict[str, object]] = {
    "top_p": 0.8,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "repetition_penalty": 1.0,
}

# 2026-08-20 8192→16384 (사용자 결정): 27b 사고 모드 agent_stop 13/315가 전부
# finish=length — 사고 블록이 8192에서 절단돼 tool call을 못 냈다 (fullhistory 판독).
FIX_MAX_TOKENS: Final = 16384
FIX_SRC_CODE_TIMEOUT: Final = 90.0


class ModelFactoryError(Exception):
    """registry 오류 대응 — 러너가 E104/input으로 매핑한다."""


def resolve_sampling(model: str) -> tuple[float | None, dict[str, object]]:
    lowered = model.lower()
    if GPT_OSS_MODEL_MARKER in lowered:
        return GPT_OSS_TEMPERATURE, {"top_p": GPT_OSS_TOP_P}
    if DEEPSEEK_V4_FLASH_0731_MODEL_MARKER in lowered:
        return DEEPSEEK_V4_FLASH_0731_TEMPERATURE, {
            "top_p": DEEPSEEK_V4_FLASH_0731_TOP_P,
        }
    if CLAUDE_MODEL_MARKER in lowered:
        return None, {}
    if QWEN35_MODEL_MARKER in lowered:
        return QWEN35_TEMPERATURE, dict(QWEN35_EXTRA_BODY)
    if QWEN38_MODEL_MARKER in lowered:
        return QWEN38_TEMPERATURE, dict(QWEN38_EXTRA_BODY)
    return INSTRUCT_GENERAL_TEMPERATURE, dict(INSTRUCT_GENERAL_EXTRA_BODY)


def _resolve_base_url(
    *, base_url_override: str | None, service_host: str, service_port: str
) -> str:
    if base_url_override:
        return base_url_override
    if service_host and service_port:
        return f"{service_host.rstrip('/')}:{service_port.lstrip('/')}"
    raise ModelFactoryError("A base URL is required via --base-url or serviceHost/servicePort.")


def create_chat_model(
    request: AgentModuleRequest,
    *,
    base_url_override: str | None = None,
    model_override: str | None = None,
) -> BaseChatModel:
    if request.provider != "openai":
        raise ModelFactoryError("Only the openai provider is supported.")

    model_name = model_override or request.model_name
    base_url = _resolve_base_url(
        base_url_override=base_url_override,
        service_host=request.service_host,
        service_port=request.service_port,
    )
    temperature, extra_body = resolve_sampling(model_name)
    try:
        return ChatOpenAI(
            model=model_name,
            base_url=base_url,
            api_key=request.user_request.api_key or os.getenv("LITELLM_MASTER_KEY", "") or "EMPTY",
            temperature=temperature,
            extra_body=extra_body,
            max_completion_tokens=FIX_MAX_TOKENS,
            timeout=FIX_SRC_CODE_TIMEOUT,
        )
    except Exception as exc:
        # serviceHost/servicePort가 조합 불가한 URL이면 httpx가 생성 시점에 던진다.
        # 이는 입력 설정 오류이므로 E104/input이어야 하고, E999/output이면 안 된다.
        raise ModelFactoryError(
            f"cannot construct the chat model for base URL {base_url!r}: {type(exc).__name__}"
        ) from exc
