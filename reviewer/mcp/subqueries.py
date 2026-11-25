"""Детерминированные подзапросы ретрива из требований и критериев задачи."""
from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

MAX_SUBQUERIES = 20
"""Предохранитель против вырожденного текста и лишних обращений к хранилищу."""

_MAX_FREE_TEXT_SUBQUERIES = 7
MAX_IDENTIFIERS = 12
"""Сколько идентификаторов попадает в пул. Дальше запрос теряет фокус."""

MIN_IDENTIFIER_LEN = 4
_MAX_GROUP_CHARS = 1_000
_MIN_TOPIC_OVERLAP = 0.2

_CRITERIA_RE = re.compile(r"критери|при[её]мк|acceptance", re.IGNORECASE)
_REQUIREMENTS_RE = re.compile(r"что сделать|требовани|requirements", re.IGNORECASE)
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+")
_ITEM_RE = re.compile(r"^([ \t]*)(?:[-*+]|\d+[.)])[ \t]+(.*\S)\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_TOKEN_RE = re.compile(r"[\w./-]+")
_LINE_SUFFIX_RE = re.compile(r":([1-9][0-9]{0,8})(?![\w:-]|\.\w)")
_URL_RE = re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.)[^\s`<>()]+", re.IGNORECASE)
_FILE_SUFFIXES = frozenset(
    "cfg css csv html ini js json jsonl jsx lock md proto py pyi rst sql svg "
    "toml ts tsx txt xml yaml yml".split()
)
_FILE_NAMES = frozenset({
    ".codexignore", ".dockerignore", ".env", ".env.example", ".gitattributes",
    ".gitignore", "Dockerfile", "Makefile",
})
_REFERENCE_HEADING_RE = re.compile(
    r"^(?:контекст|справочно|ссылки|источники|context|references|background)\b",
    re.IGNORECASE,
)
_TOPIC_RE = re.compile(r"[^\W\d]\w*(?:[./-]\w+)*")
_TOPIC_STOPWORDS = {
    "после", "перед", "когда", "если", "также", "только", "можно", "через",
    "должен", "должна", "должны", "который", "которые",
    "after", "before", "when", "with", "only", "must", "should",
    "from", "into", "than", "that", "this", "these", "those",
}


def _is_file_name(token: str) -> bool:
    name = token.rsplit("/", 1)[-1]
    stem, _, suffix = name.rpartition(".")
    return name in _FILE_NAMES or bool(stem.strip(".") and suffix.lower() in _FILE_SUFFIXES)


def _is_identifier(token: str) -> bool:
    """Технический идентификатор: snake_case, CamelCase, путь или имя файла."""
    if _is_file_name(token):
        return True
    if len(token) < MIN_IDENTIFIER_LEN or not token.strip("./-_"):
        return False
    if "/" in token:
        return True
    if not token.isascii():
        return False
    if "_" in token:
        return True
    return token[:1].isupper() and any(c.isupper() for c in token[1:])


def _section_items(text: str) -> tuple[list[tuple[bool, str]], list[str]]:
    """Вернуть пункты с признаком приёмки и строки для поиска идентификаторов."""
    items: list[tuple[bool, str]] = []
    prose: list[str] = []
    in_section = False
    acceptance_depth: int | None = None
    item_content_column: int | None = None
    lines: list[str] = []
    after_blank = True
    fence = ""

    def flush() -> None:
        nonlocal item_content_column
        if lines:
            items.append((acceptance_depth is not None, " ".join(lines)))
            lines.clear()
        item_content_column = None

    for line in text.splitlines():
        stripped = line.strip()
        marker = _FENCE_RE.match(line)
        if fence:
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = ""
            elif stripped:
                (lines if lines else prose).append(stripped)
            continue
        if marker:
            fence = marker[1]
            if after_blank and not line[0].isspace() and acceptance_depth is None:
                flush()
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            depth = len(heading[1])
            if acceptance_depth is not None and depth <= acceptance_depth:
                acceptance_depth = None
            if acceptance_depth is None and _CRITERIA_RE.search(stripped):
                acceptance_depth = depth
            in_section = acceptance_depth is not None or bool(_REQUIREMENTS_RE.search(stripped))
            prose.append(line)
            after_blank = True
        elif not stripped:
            after_blank = True
        elif not in_section:
            prose.append(line)
        else:
            item = _ITEM_RE.match(line)
            if item:
                indent = len(item[1].expandtabs(4))
                nested_criterion = (acceptance_depth is not None
                                    and item_content_column is not None
                                    and indent >= item_content_column)
                if not nested_criterion:
                    flush()
                    item_content_column = len(line[:item.start(2)].expandtabs(4))
                lines.append(item[2])
            elif lines and (not after_blank
                            or (item_content_column is not None and line[0].isspace())):
                lines.append(stripped)
            else:
                flush()
                (lines if acceptance_depth is not None else prose).append(stripped)
            after_blank = False
    flush()
    return items, prose


def _tokens(lines: Iterable[str]) -> Iterator[str]:
    for line in lines:
        for token in _TOKEN_RE.findall(_URL_RE.sub(" ", line)):
            yield token.rstrip(".")


def _without_reference_sections(description: str, *, respect_fences: bool = False) -> Iterator[str]:
    skip_depth = None
    fence = ""
    for line in description.splitlines():
        marker = _FENCE_RE.match(line) if respect_fences else None
        if marker and not fence:
            fence = marker[1]
        elif marker and marker[1].startswith(fence) and not marker[2].strip():
            fence = ""
        elif not fence and (heading := _HEADING_RE.match(line)):
            depth = len(heading[1])
            if skip_depth is None or depth <= skip_depth:
                skip_depth = (
                    depth if _REFERENCE_HEADING_RE.match(line[heading.end():]) else None
                )
        if skip_depth is None:
            yield line


def explicit_python_locations(task: dict | None, free_text: str) -> dict[str, int | None]:
    """Относительные Git-пути, для каждого сохраняется первая явная координата."""
    if task:
        lines = [
            str(task.get("title") or ""),
            *_without_reference_sections(str(task.get("description") or "")),
            *(str(criterion) for criterion in task.get("criteria") or []),
        ]
    else:
        lines = [free_text]
    locations: dict[str, int | None] = {}
    for raw in lines:
        line = _URL_RE.sub(" ", raw)
        for match in _TOKEN_RE.finditer(line):
            token = match[0].rstrip(".")
            path = token.removeprefix("./")
            if ("/" not in token or not path.endswith(".py")
                    or any(part in ("", ".", "..") for part in path.split("/"))):
                continue
            if path not in locations and len(locations) == MAX_IDENTIFIERS:
                continue
            if locations.setdefault(path, None) is None:
                number = _LINE_SUFFIX_RE.match(line, match.end())
                if number and match[0] == token:
                    locations[path] = int(number[1])
    return locations


def explicit_python_paths(task: dict | None, free_text: str) -> list[str]:
    """Относительные Git-пути из постановки и критериев, в порядке упоминания."""
    return list(explicit_python_locations(task, free_text))


def _identifiers(lines: list[str], queries: list[str]) -> list[str]:
    """Идентификаторы текста, который не вошёл в отдельные подзапросы."""
    covered = set(_tokens(queries))
    return list(dict.fromkeys(
        token for token in _tokens(lines)
        if _is_identifier(token) and (token not in covered or not _is_file_name(token))
    ))


def _keep_file_names(queries: list[str], task_text: Iterable[str], target: int) -> list[str]:
    """Вписать пропущенные имена в выбранный запрос, сохраняя его текст и число запросов."""
    names = dict.fromkeys(token for token in _tokens(task_text) if _is_file_name(token))
    if names and queries:
        emitted = set(_tokens(queries))
        missing = [name for name in names if name not in emitted][:MAX_IDENTIFIERS]
        if missing:
            queries[target] += "\n" + " ".join(missing)
    return queries


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _dedup(queries: list[str]) -> list[str]:
    """Убрать повторы, сохранив первое написание и порядок."""
    by_key: dict[str, str] = {}
    for query in queries:
        key = _normalized(query).casefold()
        if key:
            by_key.setdefault(key, query)
    return list(by_key.values())


def build_free_text_subqueries(text: str) -> list[str]:
    """Добавить ограниченное число явно оформленных требований к исходному запросу."""
    items, _ = _section_items("\n".join(_without_reference_sections(text, respect_fences=True)))
    candidates = [_normalized(item) for _, item in items]
    queries = _dedup([text, *(item for item in candidates if len(item) <= _MAX_GROUP_CHARS)])
    return queries[:_MAX_FREE_TEXT_SUBQUERIES]


def _topic_overlap(left: set[str], right: set[str]) -> float:
    common = len(left & right)
    # Длинный критерий может разворачивать ту же тему и добавлять детали.
    return common / min(len(left), len(right)) if common >= 2 else 0.0


def _group_criteria(criteria: list[str], slots: int, merges_needed: int,
                    excluded: list[str]) -> list[str]:
    """При нехватке слотов объединять пары с близкой лексикой."""
    groups: list[str] = []
    unpaired: dict[int, set[str]] = {}
    for criterion in criteria:
        terms = {term for term in _TOPIC_RE.findall(criterion.casefold()) if len(term) >= 4}
        terms.difference_update(_TOPIC_STOPWORDS)
        best_index = None
        best_score = _MIN_TOPIC_OVERLAP
        if merges_needed:
            for index, other_terms in unpaired.items():
                if len(groups[index]) + 1 + len(criterion) > _MAX_GROUP_CHARS:
                    continue
                score = _topic_overlap(terms, other_terms)
                if score >= _MIN_TOPIC_OVERLAP and (best_index is None or score > best_score):
                    best_index, best_score = index, score

        if best_index is not None:
            groups[best_index] += "\n" + criterion
            del unpaired[best_index]
            merges_needed -= 1
        elif len(groups) < slots:
            unpaired[len(groups)] = terms
            groups.append(criterion)
        else:
            excluded.append(criterion)
    return groups


def build_subqueries(task: dict | None, base_query: str, *,
                     excluded_criteria: list[str] | None = None) -> list[str]:
    """Сохранить базовый запрос и отдать приёмке приоритет при нехватке слотов.

    При наличии `excluded_criteria` дописать туда критерии, которым не хватило
    места ни в собственном запросе, ни в паре с другим критерием.
    """
    base = _dedup([base_query])
    if not task:
        return base

    text = "\n".join([str(task.get("title") or ""), str(task.get("description") or "")])
    items, prose = _section_items(text)
    criteria_text = [str(criterion) for criterion in (task.get("criteria") or [])]
    items.extend((True, criterion) for criterion in criteria_text)
    normalized = [(is_criterion, _normalized(item)) for is_criterion, item in items]
    queries = _dedup([*base, *(item for _, item in normalized)])
    bundle = " ".join(_identifiers(prose, queries)[:MAX_IDENTIFIERS])
    queries = _dedup([*queries, bundle])
    target = -1
    if len(queries) > MAX_SUBQUERIES:
        criterion_keys = {item.casefold() for is_criterion, item in normalized if is_criterion}
        criteria = [query for query in queries[len(base):] if query.casefold() in criterion_keys]
        other = [query for query in queries[len(base):] if query.casefold() not in criterion_keys]
        slots = MAX_SUBQUERIES - len(base)
        groups = _group_criteria(criteria, slots, len(queries) - MAX_SUBQUERIES,
                                 excluded_criteria if excluded_criteria is not None else [])
        selected_other = other[:slots - len(groups)]
        queries = [*base, *selected_other, *groups]
        if selected_other:
            target = len(base) + len(selected_other) - 1
    return _keep_file_names(queries, [text, *criteria_text], target)
