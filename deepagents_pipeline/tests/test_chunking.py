from itertools import pairwise

import pytest
from deepagents_pipeline.chunking import (
    CodeLanguageHint,
    UnsupportedLanguageError,
    build_source_chunks,
    fallback_line_chunks,
    find_chunk_for_line,
    resolve_language,
)

JAVA_CODE = """public class Demo {

    private static Connection connection;

    public void open() {
        connection = connect();
    }

    public void close() {
        connection.close();
    }
}"""


def test_vendored_astchunk_produces_chunk_covering_sink_line():
    chunks = build_source_chunks(JAVA_CODE, CodeLanguageHint("java", ".java"))

    sink = find_chunk_for_line(chunks, 3)

    assert sink is not None
    assert sink.start_line <= 3 <= sink.end_line
    body = JAVA_CODE.split("\n")[sink.start_line - 1 : sink.end_line]
    assert "private static Connection connection;" in "\n".join(body)


def test_chunk_content_is_header_free_and_line_aligned():
    chunks = build_source_chunks(JAVA_CODE, CodeLanguageHint("java", ".java"))

    for chunk in chunks:
        assert not chunk.content.startswith("'''")
        assert chunk.start_line >= 1
        assert chunk.end_line >= chunk.start_line


def test_file_extension_wins_over_programming_language():
    assert resolve_language(CodeLanguageHint("java", ".py")) == "python"
    assert resolve_language(CodeLanguageHint("Java", None)) == "java"


def test_unsupported_language_carries_e103():
    with pytest.raises(UnsupportedLanguageError) as excinfo:
        resolve_language(CodeLanguageHint("kotlin", ".kt"))

    assert excinfo.value.error == ("E103", "input")


def test_fallback_line_chunks_cover_every_line_contiguously():
    code = "\n".join(f"line {i}" for i in range(1, 51))

    chunks = fallback_line_chunks(code, max_chunk_chars=40)

    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 50
    for previous, current in pairwise(chunks):
        assert current.start_line == previous.end_line + 1
