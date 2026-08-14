from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final, Literal, Self, assert_never

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agents.config import ACTIVE_SUGGESTION_MODULE_KIND, ActiveSuggestionModuleKind


CONTRACT_MODEL_CONFIG: Final = ConfigDict(
    extra="ignore",
    frozen=True,
    populate_by_name=True,
    serialize_by_alias=True,
)
INPUT_ENCODINGS: Final[tuple[str, ...]] = ("utf-8-sig", "utf-8", "cp949")


class ContractModel(BaseModel):
    model_config = CONTRACT_MODEL_CONFIG


class ContextEvent(ContractModel):
    line_no: int = Field(alias="lineNo")
    event_type: str = Field(alias="eventType")
    message_key: str = Field(alias="messageKey")
    message: str
    params: dict[str, str | int] = Field(default_factory=dict)

    @property
    def lineNo(self) -> int:
        return self.line_no

    @property
    def eventType(self) -> str:
        return self.event_type

    @property
    def messageKey(self) -> str:
        return self.message_key


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

    @property
    def APIKEY(self) -> str:
        return self.api_key

    @property
    def programmingLanguage(self) -> str:
        return self.programming_language

    @property
    def fileExtension(self) -> str | None:
        return self.file_extension

    @property
    def issueLineNumber(self) -> int:
        return self.issue_line_number

    @property
    def entireCode(self) -> str:
        return self.entire_code


class AgentModuleRequest(ContractModel):
    user_id: int = Field(alias="userId")
    module_kind: ActiveSuggestionModuleKind = Field(alias="moduleKind")
    request_idx: int = Field(alias="requestIdx")
    model_name: str = Field(alias="model")
    provider: str
    service_host: str = Field(alias="serviceHost")
    service_port: str = Field(alias="servicePort")
    user_request: AgentUserRequest = Field(alias="userRequest")

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def serviceHost(self) -> str:
        return self.service_host

    @property
    def servicePort(self) -> str:
        return self.service_port

    @property
    def userRequest(self) -> AgentUserRequest:
        return self.user_request


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
    module_kind: ActiveSuggestionModuleKind = Field(alias="moduleKind")
    request_idx: int = Field(alias="requestIdx")
    desc: str
    code_modifications: list[CodeModification] = Field(alias="codeModifications")
    timings: Timings


class AgentModuleErrorResponse(ContractModel):
    is_success: Literal[False] = Field(default=False, alias="isSuccess")
    user_id: int | None = Field(default=None, alias="userId")
    module_kind: ActiveSuggestionModuleKind = Field(default=ACTIVE_SUGGESTION_MODULE_KIND, alias="moduleKind")
    request_idx: int | None = Field(default=None, alias="requestIdx")
    desc: str = ""
    code_modifications: list[CodeModification] = Field(default_factory=list, alias="codeModifications")
    timings: Timings = Field(default_factory=Timings)
    error_code: str = Field(alias="errorCode")
    error_stage: str = Field(alias="errorStage")
    error_message: str = Field(alias="errorMessage")


def parse_request_json_bytes(raw: bytes, source_name: str = "<bytes>") -> AgentModuleRequest:
    from agents.error_handling import InputError

    decoded: str | None = None
    for encoding in INPUT_ENCODINGS:
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if decoded is None:
        encodings = ", ".join(INPUT_ENCODINGS)
        raise InputError("E101", f"Could not decode {source_name}; tried encodings: {encodings}")

    try:
        data = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise InputError("E102", f"{source_name}: invalid JSON at line {exc.lineno}, column {exc.colno}") from exc

    try:
        return AgentModuleRequest.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(_validation_error_location(error) for error in exc.errors())
        raise InputError("E102", f"{source_name}: schema validation failed: {details}") from exc


def _validation_error_location(error: Mapping[str, str | int | tuple[str | int, ...]]) -> str:
    location = error.get("loc", ())
    message = error.get("msg", "invalid value")
    match location:
        case tuple(parts):
            path = ".".join(str(part) for part in parts)
        case str(path):
            pass
        case int(line):
            path = str(line)
        case unreachable:
            assert_never(unreachable)
    return f"{path}: {message}"
