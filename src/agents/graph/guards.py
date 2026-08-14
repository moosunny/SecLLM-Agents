from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

NUM_CHARS_PER_TOKEN: Final = 4
MAX_DESC_STRING_LIMIT: Final = 300
MIN_DELETE_NON_WHITESPACE_CHARS: Final = 3
MAX_DELETE_RATIO: Final = 0.40
MAX_STRUCTURAL_DELETE_LINES: Final = 80

ModificationValue = int | str | None
ModificationInput = Mapping[str, ModificationValue]
_NUMBERED_PREFIX = re.compile(r"^(\s*)\d+\.\s?(.*)$")


def estimate_tokens(text: str) -> float:
    return len(text) / NUM_CHARS_PER_TOKEN


def detect_line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def convert_to_line_ending(text: str, line_ending: str) -> str:
    return normalize_line_endings(text).replace("\n", line_ending)


def sanitize_line_prefixes(text: str) -> str:
    """Remove prompt line-number prefixes while preserving code indentation."""
    sanitized: list[str] = []
    for line in normalize_line_endings(text).split("\n"):
        match = _NUMBERED_PREFIX.match(line)
        sanitized.append(f"{match.group(1)}{match.group(2)}" if match else line)
    return "\n".join(sanitized)


def validate_source_edit(
    *,
    start_line: int,
    end_line: int,
    new_string: str,
    desc: str,
    code_lines: Sequence[str],
) -> list[str]:
    """Validate one inclusive, contiguous source replacement."""
    errors = _validate_desc(desc)
    max_line = len(code_lines)
    if start_line < 1 or start_line > max_line:
        errors.append(
            f"startLine {start_line} is outside the allowed range 1-{max_line}."
        )
        return errors
    if end_line < 1 or end_line > max_line:
        errors.append(f"endLine {end_line} is outside the allowed range 1-{max_line}.")
        return errors
    if start_line > end_line:
        errors.append(f"startLine {start_line} is after endLine {end_line}.")
        return errors

    target = "\n".join(code_lines[start_line - 1 : end_line])
    replacement = sanitize_line_prefixes(new_string)
    if normalize_line_endings(target).rstrip("\n") == normalize_line_endings(
        replacement
    ).rstrip("\n"):
        errors.append("replacement is a no-op.")

    old_lines = target.splitlines()
    new_lines = replacement.splitlines()
    net_removed_lines = max(0, len(old_lines) - len(new_lines))
    ratio = net_removed_lines / len(code_lines) if code_lines else 1.0
    if ratio > MAX_DELETE_RATIO:
        errors.append(
            f"replacement removes {net_removed_lines} net lines from {len(code_lines)} "
            f"lines ({ratio:.0%}), exceeding the {MAX_DELETE_RATIO:.0%} limit."
        )
    if net_removed_lines > MAX_STRUCTURAL_DELETE_LINES:
        errors.append(
            f"replacement removes {net_removed_lines} net structural lines, exceeding "
            f"the {MAX_STRUCTURAL_DELETE_LINES} line limit."
        )

    if not replacement.strip():
        non_ws_chars = sum(1 for char in target if not char.isspace())
        if non_ws_chars < MIN_DELETE_NON_WHITESPACE_CHARS:
            errors.append(
                f"replacement removes {non_ws_chars} non-whitespace chars, below the "
                f"minimum {MIN_DELETE_NON_WHITESPACE_CHARS}."
            )
    return errors


def source_edit_to_modifications(
    *,
    start_line: int,
    end_line: int,
    new_string: str,
    line_ending: str,
) -> list[dict[str, int | str]]:
    """Convert the internal replace label to the unchanged public add/delete contract."""
    modifications: list[dict[str, int | str]] = [
        {"type": "delete", "startLine": start_line, "endLine": end_line}
    ]
    replacement = sanitize_line_prefixes(new_string)
    if replacement.strip():
        modifications.append(
            {
                "type": "add",
                "startLine": start_line,
                "content": convert_to_line_ending(replacement, line_ending),
            }
        )
    return modifications


def validate_candidate(
    modifications: Sequence[ModificationInput],
    desc: str,
    code_lines: Sequence[str],
) -> list[str]:
    """Validate the public modification shape after an edit has been accepted."""
    errors = _validate_desc(desc)
    if not modifications:
        errors.append("modifications is empty.")
        return errors
    for index, modification in enumerate(modifications, start=1):
        mod_type = _as_str(modification.get("type"))
        start_line = _as_int(modification.get("startLine"))
        if mod_type not in ("add", "delete"):
            errors.append(
                f"modification {index} has unsupported type {mod_type!r}; expected add or delete."
            )
            continue
        max_line = len(code_lines) + (1 if mod_type == "add" else 0)
        if start_line is None:
            errors.append(f"modification {index} {mod_type} is missing startLine.")
            continue
        if start_line < 1 or start_line > max_line:
            errors.append(
                f"modification {index} {mod_type} startLine {start_line} is outside "
                f"the allowed range 1-{max_line}."
            )
        if mod_type == "delete":
            end_line = _as_int(modification.get("endLine"))
            if end_line is None:
                errors.append(f"modification {index} delete is missing endLine.")
            elif end_line < start_line or end_line > len(code_lines):
                errors.append(
                    f"modification {index} delete endLine {end_line} is outside "
                    f"the allowed range {start_line}-{len(code_lines)}."
                )
        else:
            content = _as_str(modification.get("content"))
            if content is None or not content.strip():
                errors.append(f"modification {index} add is missing content.")
            elif any(_NUMBERED_PREFIX.match(line) for line in content.splitlines()):
                errors.append(
                    f"add L{start_line} content appears to include a line-number prefix."
                )
    return errors


def _validate_desc(desc: str) -> list[str]:
    if not desc.strip():
        return ["desc is empty."]
    if len(desc) > MAX_DESC_STRING_LIMIT:
        return [
            f"desc has {len(desc)} chars, exceeding the {MAX_DESC_STRING_LIMIT} char limit."
        ]
    return []


def _as_int(value: ModificationValue | None) -> int | None:
    return value if type(value) is int else None


def _as_str(value: ModificationValue | None) -> str | None:
    return value if isinstance(value, str) else None
