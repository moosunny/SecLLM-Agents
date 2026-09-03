"""G004 모델 팩토리 테스트 — 샘플링 카브아웃과 registry 계약 미러 (§3, §5.1)."""

import pytest

from deepagents_pipeline.contracts import AgentModuleRequest
from deepagents_pipeline.models import (
    ModelFactoryError,
    create_chat_model,
    resolve_sampling,
)


def _request(provider: str = "openai", model: str = "qwen3.5-9b", host: str = "http://localhost", port: str = "8000"):
    return AgentModuleRequest.model_validate(
        {
            "userId": 1,
            "moduleKind": "ActiveSuggestion",
            "requestIdx": 0,
            "model": model,
            "provider": provider,
            "serviceHost": host,
            "servicePort": port,
            "userRequest": {
                "APIKEY": "sk-x",
                "locale": "ko",
                "programmingLanguage": "java",
                "issueLineNumber": 1,
                "entireCode": "class A {}",
            },
        }
    )


def test_claude_carveout_no_sampling_knobs():
    temperature, extra_body = resolve_sampling("claude-haiku-4-5-20251001")
    assert temperature is None
    assert extra_body == {}
    model = create_chat_model(_request(model="claude-haiku-4-5-20251001"))
    assert model.temperature is None
    assert not model.extra_body  # vLLM 노브가 페이로드에 없어야 400이 안 난다


def test_gpt_oss_carveout():
    temperature, extra_body = resolve_sampling("gpt-oss-120b")
    assert temperature == 1.0
    assert extra_body == {"top_p": 1.0}


def test_qwen35_carveout():
    # 2026-08-20 사용자 지정: qwen3.5 계열 권장 샘플링 (lora 파생 포함)
    for name in ("qwen3.5-9b", "qwen3.5-9b-lora"):
        temperature, extra_body = resolve_sampling(name)
        assert temperature == 1.0, name
        assert extra_body == {
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 1.5,
            "repetition_penalty": 1.0,
        }, name


def test_qwen38_carveout():
    # 2026-08-19 사용자 지정: qwen3.8 계열 권장 샘플링
    temperature, extra_body = resolve_sampling("qwen3.8-27b")
    assert temperature == 1.0
    assert extra_body == {
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
    }


def test_deepseek_v4_flash_0731_carveout():
    temperature, extra_body = resolve_sampling("deepseek-v4-flash-0731")
    assert temperature == 1.0
    assert extra_body == {"top_p": 0.95}


def test_default_instruct_general_preset():
    # qwen3.5/3.8 카브아웃에 걸리지 않는 일반 instruct 모델
    temperature, extra_body = resolve_sampling("generic-instruct-model")
    assert temperature == 0.7
    assert extra_body["top_p"] == 0.8
    assert extra_body["presence_penalty"] == 1.5


def test_non_openai_provider_rejected():
    with pytest.raises(ModelFactoryError, match="openai"):
        create_chat_model(_request(provider="vllm"))


def test_base_url_priority_and_requirement():
    model = create_chat_model(_request(), base_url_override="http://proxy:9999/v1")
    assert "proxy:9999" in str(model.openai_api_base)

    model2 = create_chat_model(_request())
    assert "localhost:8000" in str(model2.openai_api_base)

    with pytest.raises(ModelFactoryError, match="base URL"):
        create_chat_model(_request(host="", port=""))


def test_model_override():
    model = create_chat_model(_request(), model_override="claude-haiku-4-5-20251001")
    assert model.model_name == "claude-haiku-4-5-20251001"
    assert model.temperature is None
