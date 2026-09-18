"""AST 청킹 어댑터 — 프로덕션 `agents/graph/chunking.py`의 이식본 (2026-08-21).

astchunk 라이브러리 자체는 `deepagents_pipeline/src/astchunk`에 무수정 복사돼 있고
(메인 레포 `src/astchunk/src/astchunk`와 byte-identical), 이 모듈은 그 위에 얹히는
얇은 래퍼다. 원본과의 유일한 차이는 예외 타입 하나뿐이다: 프로덕션의
`agents.error_handling.InputError`(E103) 대신 하네스 계약의
`ERROR_UNSUPPORTED_INPUT`를 실어 나른다. 청크 좌표·fallback·헤더 처리 규칙은
프로덕션과 동일하게 유지해야 비교 가능성이 남는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol, TypedDict

from deepagents_pipeline.contracts import ERROR_UNSUPPORTED_INPUT

NUM_CHARS_PER_TOKEN: Final = 4
TOOL_TOKEN_LIMIT: Final = 2500
DEFAULT_MAX_CHUNK_CHARS: Final = TOOL_TOKEN_LIMIT * NUM_CHARS_PER_TOKEN

_EXTENSION_TO_LANGUAGE: Final[dict[str, str]] = {
    ".py": "python",
    ".java": "java",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".go": "go",
    ".js": "javascript",
    ".jsx": "javascript",
}
_NAME_TO_LANGUAGE: Final[dict[str, str]] = {
    "python": "python",
    "java": "java",
    "typescript": "typescript",
    "ts": "typescript",
    "c": "c",
    "cpp": "cpp",
    "c++": "cpp",
    "go": "go",
    "golang": "go",
    "javascript": "javascript",
    "js": "javascript",
}


class UnsupportedLanguageError(Exception):
    """E103 — 지원하지 않는 언어 힌트. 호출부가 error envelope로 강등한다."""

    error = ERROR_UNSUPPORTED_INPUT


@dataclass(frozen=True, slots=True)
class CodeLanguageHint:
    programming_language: str
    file_extension: str | None = None


@dataclass(frozen=True, slots=True)
class SourceChunk:
    start_line: int
    end_line: int
    content: str
    # Enclosing class/function headers (outermost first). Empty on the fallback
    # line-splitting path, which has no AST ancestry.
    ancestors: tuple[str, ...] = ()


class AstChunkMetadata(TypedDict, total=False):
    start_line_no: int
    end_line_no: int
    startLine: int
    endLine: int


class AstCodeWindow(TypedDict):
    content: str
    metadata: AstChunkMetadata


class AstChunkBuilder(Protocol):
    def chunkify(self, code: str) -> Sequence[AstCodeWindow]: ...


class AstChunkBuilderFactory(Protocol):
    def __call__(self, **configs: str | int) -> AstChunkBuilder: ...


ASTChunkBuilder: AstChunkBuilderFactory | None = None


def resolve_language(hint: CodeLanguageHint) -> str:
    extension = (hint.file_extension or "").lower().strip()
    if extension:
        language = _EXTENSION_TO_LANGUAGE.get(extension)
        if language is not None:
            return language

    name = hint.programming_language.lower().strip()
    language = _NAME_TO_LANGUAGE.get(name)
    if language is not None:
        return language

    supported = ", ".join(sorted(set(_EXTENSION_TO_LANGUAGE.values())))
    raise UnsupportedLanguageError(
        "Unsupported language hint "
        f"fileExtension={hint.file_extension!r}, "
        f"programmingLanguage={hint.programming_language!r}; "
        f"supported languages: {supported}."
    )


def resolve_declared_language(hint: CodeLanguageHint) -> str:
    """하네스 입구용 엄격판 — 두 힌트가 모두 알려진 값이고 서로 일치해야 한다.

    `resolve_language`는 프로덕션 규칙대로 확장자가 이름을 조용히 덮어쓰지만, 입구
    게이트(harness.run_fix_with_trace)는 잘못 라우팅된 레코드를 조용히 처리하면 안
    되므로 ① 비어 있지 않은 확장자가 표에 없거나 ② 이름과 확장자가 다른 언어를
    가리키면 UnsupportedLanguageError를 던진다 (2026-09-08, java 전용 게이트를
    언어 집합 게이트로 바꾸면서 기존 반려 규칙을 보존).
    """
    extension = (hint.file_extension or "").lower().strip()
    name = (hint.programming_language or "").lower().strip()
    by_extension = _EXTENSION_TO_LANGUAGE.get(extension) if extension else None
    by_name = _NAME_TO_LANGUAGE.get(name) if name else None
    if extension and by_extension is None:
        raise UnsupportedLanguageError(
            f"Unknown fileExtension {hint.file_extension!r} "
            f"(programmingLanguage={hint.programming_language!r})."
        )
    if by_extension and by_name and by_extension != by_name:
        raise UnsupportedLanguageError(
            f"fileExtension {hint.file_extension!r} ({by_extension}) conflicts with "
            f"programmingLanguage {hint.programming_language!r} ({by_name})."
        )
    return resolve_language(hint)


def build_source_chunks(
    code: str,
    hint: CodeLanguageHint,
    *,
    fallback_max_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> list[SourceChunk]:
    language = resolve_language(hint)
    try:
        builder = _ast_chunk_builder()(
            max_chunk_size=DEFAULT_MAX_CHUNK_CHARS,
            language=language,
            metadata_template="default",
        )
        # chunk_expansion=True prepends a triple-quote header carrying the
        # class/function ancestry; repo_level_metadata is emptied so only
        # ancestors (never a filepath) appear. Line-number metadata is computed
        # before expansion, so stripping the header keeps content aligned.
        windows = builder.chunkify(
            code,
            chunk_expansion=True,
            repo_level_metadata={},
        )
        chunks = _source_chunks_from_windows(windows)
        if chunks:
            return chunks
    except (
        AssertionError,
        ImportError,
        KeyError,
        RuntimeError,
        UnicodeError,
        ValueError,
    ):
        return fallback_line_chunks(code, max_chunk_chars=fallback_max_chars)
    return fallback_line_chunks(code, max_chunk_chars=fallback_max_chars)


def fallback_line_chunks(
    code: str,
    *,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> list[SourceChunk]:
    lines = code.splitlines()
    if not lines:
        return [SourceChunk(start_line=1, end_line=1, content="")]

    chunks: list[SourceChunk] = []
    current_lines: list[str] = []
    current_start = 1
    current_chars = 0

    for line_number, line in enumerate(lines, start=1):
        next_chars = len(line) if not current_lines else len(line) + 1
        if current_lines and current_chars + next_chars > max_chunk_chars:
            chunks.append(
                SourceChunk(
                    start_line=current_start,
                    end_line=line_number - 1,
                    content="\n".join(current_lines),
                )
            )
            current_lines = []
            current_start = line_number
            current_chars = 0

        current_lines.append(line)
        current_chars += len(line) if current_chars == 0 else len(line) + 1

    chunks.append(
        SourceChunk(
            start_line=current_start,
            end_line=current_start + len(current_lines) - 1,
            content="\n".join(current_lines),
        )
    )
    return chunks


def find_chunk_for_line(
    chunks: Sequence[SourceChunk],
    line: int,
) -> SourceChunk | None:
    """Return the chunk whose line range contains ``line`` (first match)."""
    for chunk in chunks:
        if chunk.start_line <= line <= chunk.end_line:
            return chunk
    return None


def _split_expansion_header(content: str) -> tuple[tuple[str, ...], str]:
    """Split an astchunk chunk-expansion header from ``content``.

    ``chunk_expansion=True`` prepends ``'''\\n<ancestor lines>'''\\n`` to every
    chunk (an empty ``'''\\n'''`` block when there is no ancestry). Returns the
    parsed ancestors and the header-free body. Content without a leading header
    (fallback path or non-expanded builders) is returned unchanged with no
    ancestors, so this is safe on every code path.
    """
    if not content.startswith("'''"):
        return (), content
    end = content.find("'''", 3)
    if end == -1:
        return (), content
    header = content[3:end]
    body = content[end + 3 :]
    if body.startswith("\n"):
        body = body[1:]
    ancestors = tuple(line.strip() for line in header.splitlines() if line.strip())
    return ancestors, body


def _source_chunks_from_windows(windows: Sequence[AstCodeWindow]) -> list[SourceChunk]:
    chunks: list[SourceChunk] = []
    for window in windows:
        metadata = window["metadata"]
        start_line = _start_line(metadata)
        end_line = _end_line(metadata, start_line)
        ancestors, content = _split_expansion_header(window["content"])
        chunks.append(
            SourceChunk(
                start_line=start_line,
                end_line=end_line,
                content=content,
                ancestors=ancestors,
            )
        )
    return chunks


def _start_line(metadata: AstChunkMetadata) -> int:
    astchunk_value = metadata.get("start_line_no")
    if astchunk_value is not None:
        return astchunk_value + 1
    return metadata.get("startLine", 1)


def _end_line(metadata: AstChunkMetadata, start_line: int) -> int:
    astchunk_value = metadata.get("end_line_no")
    if astchunk_value is not None:
        return astchunk_value + 1
    return metadata.get("endLine", start_line)


def _ast_chunk_builder() -> AstChunkBuilderFactory:
    global ASTChunkBuilder
    if ASTChunkBuilder is None:
        from astchunk import ASTChunkBuilder as ImportedASTChunkBuilder

        ASTChunkBuilder = ImportedASTChunkBuilder
    return ASTChunkBuilder
