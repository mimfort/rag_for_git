"""Сверка показанных строк брифа с Git-снимком индекса."""
from collections.abc import Callable, Mapping

from reviewer.mcp.brief_evidence import _visible_blocks


def check_brief_anchors(
    payload: Mapping[str, object],
    read_sources: Callable[[list[str]], dict[str, str]],
) -> tuple[dict[str, dict[str, set[int]]], dict[str, dict[str, int]]]:
    blocks_by_section = {}
    for section in ("code", "test_exemplars"):
        value = payload.get(section)
        if isinstance(value, str):
            blocks_by_section[section] = _visible_blocks(value)
    paths = list(dict.fromkeys(path for blocks in blocks_by_section.values()
                               for path, _, _ in blocks))
    if not paths:
        return {}, {}
    source = {path: text.split("\n") for path, text in read_sources(paths).items()}

    eligible = {}
    diagnostics = {}
    for section, blocks in blocks_by_section.items():
        if not blocks:
            continue
        confirmed = {}
        rejected = {}
        counts = {"checked": 0, "mismatched": 0, "unavailable": 0}
        for path, first, rows in blocks:
            lines = source.get(path)
            if lines is None:
                counts["unavailable"] += len(rows)
                continue
            counts["checked"] += len(rows)
            mismatched = sum(
                not (1 <= number <= len(lines) and _matches(text, lines[number - 1], number == first))
                for number, text in rows
            )
            counts["mismatched"] += mismatched
            # Даже совпавшая строка смещённого фрагмента может быть из другого символа.
            target = rejected if mismatched else confirmed
            target.setdefault(path, set()).update(number for number, _ in rows)
        # Пересекающиеся фрагменты с конфликтом тоже не дают однозначной ссылки.
        validated = {path: numbers - rejected.get(path, set())
                     for path, numbers in confirmed.items()}
        validated = {path: numbers for path, numbers in validated.items() if numbers}
        if validated:
            eligible[section] = validated
        diagnostics[section] = counts
    return eligible, diagnostics


def _matches(shown: str, source: str, first: bool) -> bool:
    # Tree-sitter начинает внутренний символ с первого токена, без отступа.
    if first:
        source = source.lstrip(" \t")
        shown = shown.lstrip(" \t")
    return shown.rstrip(" \t\r") == source.rstrip(" \t\r")
