"""Сверка показанных строк брифа с Git-снимком индекса."""
from collections.abc import Callable, Mapping

from reviewer.mcp.brief_evidence import BriefAliases, BriefSections, _visible_excerpts


def check_brief_anchors(
    payload: Mapping[str, object],
    read_sources: Callable[[list[str]], dict[str, str]],
) -> tuple[BriefSections, dict[str, dict[str, int]]]:
    eligible, diagnostics, _ = collect_brief_evidence(payload, read_sources)
    return eligible, diagnostics


def collect_brief_evidence(
    payload: Mapping[str, object],
    read_sources: Callable[[list[str]], dict[str, str]],
    read_existing_paths: Callable[[list[str]], set[str] | None] | None = None,
) -> tuple[BriefSections, dict[str, dict[str, int]], BriefAliases]:
    blocks_by_section = {}
    for section in ("code", "test_exemplars"):
        value = payload.get(section)
        if isinstance(value, str):
            blocks_by_section[section] = _visible_excerpts(value)
    paths = list(dict.fromkeys(block.path for blocks in blocks_by_section.values()
                               for block in blocks))
    if not paths:
        return {}, {}, {}
    source = {path: text.split("\n") for path, text in read_sources(paths).items()}

    eligible = {}
    diagnostics = {}
    aliases = {}
    for section, blocks in blocks_by_section.items():
        if not blocks:
            continue
        confirmed = {}
        rejected = {}
        claims: dict[str, dict[str, set[int]]] = {}
        counts = {"checked": 0, "mismatched": 0, "unavailable": 0}
        for node_id, path, first, rows in blocks:
            alias_lines = None
            prefix = path + "#"
            if read_existing_paths is not None and node_id.startswith(prefix) and node_id != prefix:
                alias_lines = claims.setdefault(node_id, {}).setdefault(path, set())
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
            if alias_lines is not None and not mismatched:
                alias_lines.update(number for number, _ in rows)
        # Пересекающиеся фрагменты с конфликтом тоже не дают однозначной ссылки.
        validated = {path: numbers - rejected.get(path, set())
                     for path, numbers in confirmed.items()}
        validated = {path: numbers for path, numbers in validated.items() if numbers}
        if validated:
            eligible[section] = validated
        diagnostics[section] = counts
        section_aliases = {}
        for node_id, candidates in claims.items():
            if len(candidates) == 1:
                path, shown = next(iter(candidates.items()))
                if lines := shown & validated.get(path, set()):
                    section_aliases[node_id] = path, lines
        if section_aliases:
            aliases[section] = section_aliases
    if aliases and read_existing_paths is not None:
        candidates = list(dict.fromkeys(node_id for group in aliases.values() for node_id in group))
        # Буквальный Git-путь имеет приоритет, даже если его нет среди отрывков.
        literal_paths = read_existing_paths(candidates)
        if literal_paths is None:
            aliases = {}
        else:
            aliases = {
                section: kept for section, group in aliases.items()
                if (kept := {node_id: claim for node_id, claim in group.items()
                             if node_id not in literal_paths})
            }
    return eligible, diagnostics, aliases


def _matches(shown: str, source: str, first: bool) -> bool:
    # Tree-sitter начинает внутренний символ с первого токена, без отступа.
    if first:
        source = source.lstrip(" \t")
        shown = shown.lstrip(" \t")
    return shown.rstrip(" \t\r") == source.rstrip(" \t\r")
