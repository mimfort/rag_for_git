"""Подбор отрывка по идентификаторам постановки при прежнем файловом бюджете."""

import re
from collections import Counter
from dataclasses import replace
from math import log2

from reviewer.mcp.brief_evidence import _HEADER
from reviewer.retrieval.multiquery import cap_block
from reviewer.retrieval.retriever import _render_block

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_NAME_PART = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_STOP_WORDS = {
    "board",
    "cases",
    "class",
    "classes",
    "code",
    "data",
    "def",
    "default",
    "false",
    "file",
    "files",
    "from",
    "given",
    "import",
    "imports",
    "line",
    "main",
    "name",
    "names",
    "none",
    "only",
    "output",
    "python",
    "result",
    "results",
    "support",
    "supported",
    "task",
    "tasks",
    "test",
    "tests",
    "that",
    "this",
    "tool",
    "tools",
    "true",
    "user",
    "when",
}
_MIN_GAIN = 0.43


def _terms(text: str) -> set[str]:
    words = set()
    for identifier in _IDENTIFIER.findall(text):
        words.add(identifier.casefold())
        words.update(part.casefold() for part in _NAME_PART.findall(identifier))
    return {word for word in words if len(word) >= 3 and word not in _STOP_WORDS}


def _scores(query: str, paths: list[str], sources: dict[str, str]) -> dict[str, float]:
    frequencies = Counter(word for path in paths for word in _terms(sources[path]))
    return {
        word: 1 / log2(2 + frequencies[word]) for word in sorted(_terms(query)) if frequencies[word]
    }


def _relevance(text: str, identifier: str, weights: dict[str, float]) -> float:
    words = _terms(text)
    names = _terms(identifier)
    return sum(
        weight * (2 if word in names else int(word in words)) for word, weight in weights.items()
    )


def _fit(item, *, raw_size: int, rendered_size: int):
    """Обрезать целые строки, пока блок входит в исходный размер после рендера."""
    candidate = cap_block(item, raw_size)
    rendered = _render_block(candidate, line_numbers=True)
    lines = candidate.text.split("\n")
    while len(lines) > 1 and len(rendered) > rendered_size:
        lines.pop()
        candidate = replace(
            candidate, text="\n".join(lines), end_line=candidate.start_line + len(lines) - 1
        )
        rendered = _render_block(candidate, line_numbers=True)
    return candidate, rendered


def _choose(block: str, items: list, weights: dict[str, float]) -> str:
    header, _, numbered = block.partition("\n")
    symbol = header.split(" (", 1)[0].split("#", 1)[-1]
    text = "\n".join(row.partition(" | ")[2] for row in numbered.splitlines())
    current_score = _relevance(text, symbol, weights)
    best_score, best_block = current_score, block
    for item in items:
        candidate, rendered = _fit(item, raw_size=len(text), rendered_size=len(block))
        if len(rendered) > len(block):
            continue
        score = _relevance(candidate.text, candidate.symbol_fqn, weights)
        if score > best_score + 1e-5:
            best_score, best_block = score, rendered
    return best_block if best_score - current_score >= _MIN_GAIN else block


def choose_windows(
    context: str, query: str, sources: dict[str, str], chunks: dict[str, list]
) -> str:
    """Сохранить пути, порядок и верхнюю границу длины каждого блока."""
    blocks = context.split("\n\n")
    headers = [_HEADER.fullmatch(block.partition("\n")[0]) for block in blocks]
    if any(header is None for header in headers):
        raise ValueError("Replay ожидает секцию целых кодовых фрагментов")
    paths = [header["path"] for header in headers]
    if len(set(paths)) != len(paths):
        raise ValueError("Replay ожидает один фрагмент на файл")
    weights = _scores(query, paths, sources)
    if not weights:
        return context
    return "\n\n".join(
        _choose(block, chunks[path], weights) for path, block in zip(paths, blocks, strict=True)
    )
