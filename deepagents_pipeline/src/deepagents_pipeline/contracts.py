"""입출력 계약 미러 (설계 §5) — 메인 repo `agents.schema`와 wire-format 동일.

격리 원칙(§1.3)에 따라 `agents` 패키지를 import하지 않고, camelCase alias를
가진 독립 pydantic 모델로 미러링한다. 필드 단위 매핑 근거는 설계 문서 §5의
표이고, 왕복 동등성은 tests/test_contracts.py가 검증한다.

오류 계약 (§5.2): 3회 검증 실패 E301/select, 모델 연결 실패 E202/fix,
registry 실패 E104/input.
"""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MODULE_KIND: Final = "ActiveSuggestion"

ERROR_VERIFY_EXHAUSTED: Final = ("E301", "select")
ERROR_MODEL_CONNECT: Final = ("E202", "fix")
ERROR_REGISTRY: Final = ("E104", "input")
ERROR_INPUT_SCHEMA: Final = ("E102", "input")
ERROR_UNSUPPORTED_INPUT: Final = ("E103", "input")
ERROR_INTERNAL: Final = ("E999", "output")
_ERROR_TYPE_BY_CODE: Final[dict[str, str]] = {
    "E102": "ValidationError",
    "E103": "UnsupportedLanguageError",
    "E104": "LLMRegistryError",
    "E202": "LLMConnectionError",
    "E301": "GenerationError",
    "E999": "InternalError",
}

CONTRACT_MODEL_CONFIG: Final = ConfigDict(
    extra="ignore",
    frozen=True,
    populate_by_name=True,
    serialize_by_alias=True,
)


class ContractModel(BaseModel):
    model_config = CONTRACT_MODEL_CONFIG


class ContextEvent(ContractModel):
    line_no: int = Field(alias="lineNo")
    event_type: str = Field(alias="eventType")
    message_key: str = Field(alias="messageKey")
    message: str
    params: dict[str, str | int] = Field(default_factory=dict)


class AgentUserRequest(ContractModel):
    api_key: str = Field(default="", alias="APIKEY")
    locale: Literal["ko", "en"]
    programming_language: str = Field(alias="programmingLanguage")
    file_extension: str | None = Field(default=None, alias="fileExtension")
    checker_id: int | None = Field(default=None, alias="checkerId")
    issue_risk: str | None = Field(default=None, alias="issueRisk")
    issue_name_ko: str | None = Field(default=None, alias="issueNameKo")
    issue_name_en: str | None = Field(default=None, alias="issueNameEn")
    issue_description_ko: str | None = Field(default=None, alias="issueDescriptionKo")
    issue_description_en: str | None = Field(default=None, alias="issueDescriptionEn")
    source_code: str | None = Field(default=None, alias="sourceCode")
    issue_line_number: int = Field(alias="issueLineNumber", ge=1)
    entire_code: str = Field(alias="entireCode")
    dangerous_example_ko: str | None = Field(default=None, alias="dangerousExampleKo")
    dangerous_example_en: str | None = Field(default=None, alias="dangerousExampleEn")
    safe_example_ko: str | None = Field(default=None, alias="safeExampleKo")
    safe_example_en: str | None = Field(default=None, alias="safeExampleEn")
    contexts: list[ContextEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_issue_line_range(self) -> Self:
        line_count = len(self.entire_code.splitlines())
        if self.issue_line_number > line_count:
            raise ValueError(
                f"issueLineNumber {self.issue_line_number} exceeds "
                f"entireCode line count {line_count}"
            )
        return self


class AgentModuleRequest(ContractModel):
    user_id: int = Field(alias="userId")
    module_kind: Literal["ActiveSuggestion"] = Field(alias="moduleKind")
    request_idx: int = Field(alias="requestIdx")
    model_name: str = Field(alias="model")
    provider: str
    service_host: str = Field(alias="serviceHost")
    service_port: str = Field(alias="servicePort")
    user_request: AgentUserRequest = Field(alias="userRequest")


class CodeModification(ContractModel):
    type: Literal["add", "delete"]
    start_line: int = Field(alias="startLine")
    end_line: int | None = Field(default=None, alias="endLine")
    content: str | None = None


class Timings(ContractModel):
    explore_elapsed_time: float = 0.0
    fix_elapsed_time: float = 0.0
    select_elapsed_time: float = 0.0
    graph_elapsed_time: float = 0.0


class AgentModuleResponse(ContractModel):
    is_success: Literal[True] = Field(default=True, alias="isSuccess")
    user_id: int = Field(alias="userId")
    module_kind: Literal["ActiveSuggestion"] = Field(default=MODULE_KIND, alias="moduleKind")
    request_idx: int = Field(alias="requestIdx")
    desc: str
    code_modifications: list[CodeModification] = Field(alias="codeModifications")
    timings: Timings


class AgentModuleErrorResponse(ContractModel):
    is_success: Literal[False] = Field(default=False, alias="isSuccess")
    user_id: int | None = Field(default=None, alias="userId")
    module_kind: Literal["ActiveSuggestion"] = Field(default=MODULE_KIND, alias="moduleKind")
    request_idx: int | None = Field(default=None, alias="requestIdx")
    desc: str = ""
    code_modifications: list[CodeModification] = Field(default_factory=list, alias="codeModifications")
    timings: Timings = Field(default_factory=Timings)
    error_code: str = Field(alias="errorCode")
    error_stage: str = Field(alias="errorStage")
    error_node: str = Field(alias="errorNode")
    error_type: str = Field(alias="errorType")
    error_message: str = Field(alias="errorMessage")


def build_success_response(
    request: AgentModuleRequest,
    *,
    desc: str,
    code_modifications: list[dict],
    fix_elapsed_time: float,
    graph_elapsed_time: float,
) -> AgentModuleResponse:
    """PASS candidate → 성공 envelope. `multi_site`는 내부 검증 필드라 응답에서 제거(§5.2).

    §5.2 정의: fix_elapsed_time = agent+verifier 루프, select_elapsed_time = 0,
    graph_elapsed_time = 전체 wall clock.
    """
    return AgentModuleResponse(
        userId=request.user_id,
        moduleKind=request.module_kind,
        requestIdx=request.request_idx,
        desc=desc,
        codeModifications=[CodeModification.model_validate(m) for m in code_modifications],
        timings=Timings(
            fix_elapsed_time=fix_elapsed_time,
            select_elapsed_time=0.0,
            graph_elapsed_time=graph_elapsed_time,
        ),
    )


def build_error_response(
    request: AgentModuleRequest | None,
    *,
    error: tuple[str, str],
    message: str,
    user_id: int | None = None,
    request_idx: int | None = None,
    error_node: str | None = None,
    error_type: str | None = None,
) -> AgentModuleErrorResponse:
    """오류 envelope. request가 없어도 raw JSON에서 건진 식별자를 넘길 수 있다."""
    code, stage = error
    return AgentModuleErrorResponse(
        userId=request.user_id if request else user_id,
        requestIdx=request.request_idx if request else request_idx,
        errorCode=code,
        errorStage=stage,
        errorNode=error_node or stage,
        errorType=error_type or _ERROR_TYPE_BY_CODE.get(code, "AgentError"),
        errorMessage=message,
    )
