"""edit_code 매칭 폴백 체인 — OpenCode edit 도구 replace()의 Python 포팅 (2026-08-20).

원 구현: opencode packages/.../tool/edit.ts (Apache-2.0; cline/gemini-cli 유래).
exact-match(simple)가 실패하면 관용적 매처들이 순서대로 후보 스팬을 생성하고,
파일에 실재하며 유일한 첫 후보가 채택된다. 채택 스팬은 항상 "실제 파일 원문"
이므로 diff 후처리 좌표계는 영향을 받지 않는다.

2026-08-20 사용자 결정: newString 들여쓰기 역보정은 미적용 — old 스팬만
원문으로 정확화하고 newString은 모델이 쓴 그대로 삽입한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator

SINGLE_CANDIDATE_SIMILARITY_THRESHOLD = 0.65
MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD = 0.65

Replacer = Callable[[str, str], Iterator[str]]


def _levenshtein(a: str, b: str) -> int:
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[len(b)]


def _line_span(original_lines: list[str], start: int, count: int, content: str) -> str:
    """원본 줄 인덱스 start부터 count줄에 해당하는 content 상의 원문 스팬."""
    begin = sum(len(original_lines[k]) + 1 for k in range(start))
    end = begin
    for k in range(count):
        end += len(original_lines[start + k])
        if k < count - 1:
            end += 1
    return content[begin:end]


def simple_replacer(content: str, find: str) -> Iterator[str]:
    yield find


def line_trimmed_replacer(content: str, find: str) -> Iterator[str]:
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    if not search_lines:
        return
    for i in range(len(original_lines) - len(search_lines) + 1):
        if all(
            original_lines[i + j].strip() == search_lines[j].strip()
            for j in range(len(search_lines))
        ):
            yield _line_span(original_lines, i, len(search_lines), content)


def block_anchor_replacer(content: str, find: str) -> Iterator[str]:
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if len(search_lines) < 3:
        return
    if search_lines[-1] == "":
        search_lines.pop()
    first = search_lines[0].strip()
    last = search_lines[-1].strip()
    size = len(search_lines)
    max_delta = max(1, size // 4)

    candidates: list[tuple[int, int]] = []
    for i in range(len(original_lines)):
        if original_lines[i].strip() != first:
            continue
        for j in range(i + 2, len(original_lines)):
            if original_lines[j].strip() == last:
                if abs((j - i + 1) - size) <= max_delta:
                    candidates.append((i, j))
                break  # 마지막 줄은 첫 등장만 본다
    if not candidates:
        return

    def _mid_similarity(start: int, end: int, average: bool) -> float:
        actual = end - start + 1
        to_check = min(size - 2, actual - 2)
        if to_check <= 0:
            return 1.0
        sim = 0.0
        j = 1
        while j < size - 1 and j < actual - 1:
            a = original_lines[start + j].strip()
            b = search_lines[j].strip()
            max_len = max(len(a), len(b))
            if max_len:
                part = 1 - _levenshtein(a, b) / max_len
                sim += part if average else part / to_check
                if not average and sim >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
                    break
            j += 1
        return sim / to_check if average else sim

    if len(candidates) == 1:
        start, end = candidates[0]
        if _mid_similarity(start, end, average=False) >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
            yield _line_span(original_lines, start, end - start + 1, content)
        return

    best, best_sim = None, -1.0
    for start, end in candidates:
        sim = _mid_similarity(start, end, average=True)
        if sim > best_sim:
            best_sim, best = sim, (start, end)
    if best is not None and best_sim >= MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD:
        start, end = best
        yield _line_span(original_lines, start, end - start + 1, content)


def whitespace_normalized_replacer(content: str, find: str) -> Iterator[str]:
    def norm(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    normalized_find = norm(find)
    lines = content.split("\n")
    for line in lines:
        if norm(line) == normalized_find:
            yield line
        elif normalized_find and normalized_find in norm(line):
            words = find.strip().split()
            if words:
                pattern = r"\s+".join(re.escape(w) for w in words)
                m = re.search(pattern, line)
                if m:
                    yield m.group(0)

    find_lines = find.split("\n")
    if len(find_lines) > 1:
        for i in range(len(lines) - len(find_lines) + 1):
            block = "\n".join(lines[i : i + len(find_lines)])
            if norm(block) == normalized_find:
                yield block


def indentation_flexible_replacer(content: str, find: str) -> Iterator[str]:
    def deindent(text: str) -> str:
        text_lines = text.split("\n")
        non_empty = [ln for ln in text_lines if ln.strip()]
        if not non_empty:
            return text
        min_indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
        return "\n".join(ln if not ln.strip() else ln[min_indent:] for ln in text_lines)

    normalized_find = deindent(find)
    content_lines = content.split("\n")
    n = len(find.split("\n"))
    for i in range(len(content_lines) - n + 1):
        block = "\n".join(content_lines[i : i + n])
        if deindent(block) == normalized_find:
            yield block


_ESCAPE_RE = re.compile(r"\\(n|t|r|'|\"|`|\\|\n|\$)")
_ESCAPE_MAP = {"n": "\n", "t": "\t", "r": "\r", "\n": "\n"}


def escape_normalized_replacer(content: str, find: str) -> Iterator[str]:
    def unescape(s: str) -> str:
        return _ESCAPE_RE.sub(lambda m: _ESCAPE_MAP.get(m.group(1), m.group(1)), s)

    unescaped = unescape(find)
    if unescaped in content:
        yield unescaped
    lines = content.split("\n")
    find_lines = unescaped.split("\n")
    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i : i + len(find_lines)])
        if unescape(block) == unescaped:
            yield block


def trimmed_boundary_replacer(content: str, find: str) -> Iterator[str]:
    trimmed = find.strip()
    if trimmed == find:
        return
    if trimmed in content:
        yield trimmed
    lines = content.split("\n")
    find_lines = find.split("\n")
    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i : i + len(find_lines)])
        if block.strip() == trimmed:
            yield block


def context_aware_replacer(content: str, find: str) -> Iterator[str]:
    find_lines = find.split("\n")
    if len(find_lines) < 3:
        return
    if find_lines[-1] == "":
        find_lines.pop()
    content_lines = content.split("\n")
    first = find_lines[0].strip()
    last = find_lines[-1].strip()
    for i in range(len(content_lines)):
        if content_lines[i].strip() != first:
            continue
        for j in range(i + 2, len(content_lines)):
            if content_lines[j].strip() == last:
                block_lines = content_lines[i : j + 1]
                if len(block_lines) == len(find_lines):
                    matching = total = 0
                    for k in range(1, len(block_lines) - 1):
                        a, b = block_lines[k].strip(), find_lines[k].strip()
                        if a or b:
                            total += 1
                            if a == b:
                                matching += 1
                    if total == 0 or matching / total >= 0.5:
                        yield "\n".join(block_lines)
                        return  # 첫 매칭만
                break


def multi_occurrence_replacer(content: str, find: str) -> Iterator[str]:
    start = 0
    while True:
        idx = content.find(find, start)
        if idx == -1:
            break
        yield find
        start = idx + len(find)


REPLACERS: list[tuple[str, Replacer]] = [
    ("simple", simple_replacer),
    ("line_trimmed", line_trimmed_replacer),
    ("block_anchor", block_anchor_replacer),
    ("whitespace_normalized", whitespace_normalized_replacer),
    ("indentation_flexible", indentation_flexible_replacer),
    ("escape_normalized", escape_normalized_replacer),
    ("trimmed_boundary", trimmed_boundary_replacer),
    ("context_aware", context_aware_replacer),
    ("multi_occurrence", multi_occurrence_replacer),
]


def _is_disproportionate(search: str, old_string: str) -> bool:
    old_lines = len(old_string.split("\n"))
    search_lines = len(search.split("\n"))
    if search_lines >= max(old_lines + 3, old_lines * 2):
        return True
    if old_lines == 1:
        return False
    return len(search.strip()) > max(len(old_string.strip()) + 500, len(old_string.strip()) * 4)


@dataclass
class MatchResult:
    span: str  # content에 실재하는 원문 스팬 (이 텍스트가 치환 대상)
    index: int
    replacer: str  # "simple"이 아니면 fuzzy 폴백 매칭


def find_unique_match(content: str, old_string: str) -> tuple[MatchResult | None, str | None]:
    """(match, error_kind) — error_kind: 'not_found' | 'ambiguous' | 'disproportionate'.

    OpenCode replace()와 동일한 순회: 후보가 content에 실재하고(0건이면 다음 후보),
    비대칭적으로 크지 않으며(즉시 중단), 유일할 때(복수면 다음 후보) 채택한다.
    """
    not_found = True
    for name, replacer in REPLACERS:
        for search in replacer(content, old_string):
            idx = content.find(search)
            if idx == -1:
                continue
            not_found = False
            if _is_disproportionate(search, old_string):
                return None, "disproportionate"
            if content.rfind(search) != idx:
                continue
            return MatchResult(span=search, index=idx, replacer=name), None
    return None, ("not_found" if not_found else "ambiguous")
