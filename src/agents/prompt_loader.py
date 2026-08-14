from __future__ import annotations

import hashlib
import string
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, Mapping, TypeAlias


Stage: TypeAlias = Literal["explore", "fix"]
Role: TypeAlias = Literal["system", "user"]
Locale: TypeAlias = Literal["en", "ko"]
PromptValue: TypeAlias = str | int

_DESC_LANGUAGE_INSTRUCTIONS: Final[Mapping[Locale, str]] = MappingProxyType(
    {
        "en": "Write the response desc field in English as a concise complete sentence.",
        "ko": (
            "Write the response desc field in Korean as a complete formal sentence. "
            "End it with 합니다, 됩니다, or 입니다; prefer 합니다."
        ),
    },
)
_FORMATTER: Final = string.Formatter()


@dataclass(frozen=True, slots=True)
class LocalizedText:
    en: str
    ko: str


@dataclass(frozen=True, slots=True)
class PromptRenderContext:
    issue_name: str
    issue_description: str
    language: str
    issue_line_number: int
    code_context: str
    dangerous_example: str
    safe_example: str
    desc_language_instruction: str
    explore_context: str = ""

    def to_format_values(self) -> Mapping[str, PromptValue]:
        return MappingProxyType(
            {
                "issue_name": self.issue_name,
                "issue_description": self.issue_description,
                "language": self.language,
                "issue_line_number": self.issue_line_number,
                "code_context": self.code_context,
                "dangerous_example": self.dangerous_example,
                "safe_example": self.safe_example,
                "desc_language_instruction": self.desc_language_instruction,
                "explore_context": self.explore_context,
            },
        )


@dataclass(frozen=True, slots=True)
class PromptRenderError(Exception):
    template_name: str
    missing_field: str

    def __str__(self) -> str:
        return f"{self.template_name} requires missing_field {self.missing_field!r}"


def desc_language_instruction(locale: Locale) -> str:
    return _DESC_LANGUAGE_INSTRUCTIONS[locale]


def choose_localized_text(text: LocalizedText) -> str:
    english = text.en.strip()
    if english:
        return english
    return text.ko.strip()


def load_prompt(stage: str, role: str) -> str:
    return (_prompt_root() / stage / f"{role}.md").read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def prompt_version(stage: str) -> str:
    """Short content hash over a stage's prompt assets.

    Recorded on traces so a prompt edit automatically separates the score
    distributions before and after it. Hashing the *templates* (not the rendered
    text) keeps the identifier stable across requests, which per-request code
    substitution would not.
    """
    root = _prompt_root() / stage
    try:
        assets = sorted(root.glob("*.md"))
    except OSError:  # pragma: no cover - unreadable prompt dir degrades to unknown
        return "unknown"
    digest = hashlib.sha1(usedforsecurity=False)
    for asset in assets:
        digest.update(asset.name.encode("utf-8"))
        digest.update(asset.read_bytes())
    return digest.hexdigest()[:8]


def render_prompt(stage: Stage, role: Role, context: PromptRenderContext) -> str:
    template_name = f"{stage}/{role}"
    return render_template(template_name, load_prompt(stage, role), context)


def render_prompt_mapping(
    stage: str,
    role: str,
    values: Mapping[str, PromptValue],
) -> str:
    """Render a prompt asset from a plain field mapping.

    Used by the phased explore prompts, whose fields (sink chunk, slice context,
    candidate pack, ...) differ from the fixed :class:`PromptRenderContext` shape.
    Missing placeholders raise :class:`PromptRenderError`, matching
    :func:`render_template`.
    """
    template_name = f"{stage}/{role}"
    template = load_prompt(stage, role)
    for field in sorted(_required_fields(template) - values.keys()):
        raise PromptRenderError(template_name=template_name, missing_field=field)
    return template.format_map(values)


def render_template(
    template_name: str,
    template: str,
    context: PromptRenderContext,
) -> str:
    values = context.to_format_values()
    for field in sorted(_required_fields(template) - values.keys()):
        raise PromptRenderError(template_name=template_name, missing_field=field)
    return template.format_map(values)


def _prompt_root() -> Path:
    frozen_root = sys.__dict__.get("_MEIPASS")
    if frozen_root is not None:
        return Path(str(frozen_root)) / "agents" / "prompts"
    return Path(__file__).resolve().parent / "prompts"


def _required_fields(template: str) -> set[str]:
    fields: set[str] = set()
    for _, field_name, _, _ in _FORMATTER.parse(template):
        if field_name is not None:
            root_field = field_name.partition(".")[0].partition("[")[0]
            if root_field:
                fields.add(root_field)
    return fields
