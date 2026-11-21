"""Чтение корпуса брифов: блок токенов, пути секций, ключ задачи."""
from __future__ import annotations

import logging
import pathlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from reviewer.metrics.brief_quality.config import BriefQualityConfig

log = logging.getLogger(__name__)

TOKENS_HEADER = "## Токены (этап solve-task)"
RELEVANT_HEADER = "## Relevant code"
TEST_HEADER = "## Test exemplars"
VERIFIED_CITATIONS_MARKER = "<!-- reviewer:verified-citations -->"
SIDECHAIN_MARK = "В т.ч. sidechain-сабагент:"

BUCKET_KEYS = ("fresh_in", "output", "cache_write", "cache_read")

_NUM_RE = re.compile(r"^(\d+(?:\.\d+)?)([KM]?)$")
_MODEL_RE = re.compile(r"^Модель:\s*(.+)$")
_BUCKETS_RE = re.compile(
    r"fresh-in\s+(\S+)\s*·\s*out\s+(\S+)\s*·\s*cache-write\s+(\S+)\s*·\s*cache-read\s+(\S+)"
)

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PATH_LINE_RE = re.compile(r"^([\w./\-]+\.\w+):([\d,\-\s]+)$")
_BARE_PATH_RE = re.compile(r"^[\w./\-]+\.\w+$")
_LINE_SUFFIX_RE = re.compile(r"^(.+?):(\d[\d,\-\s]*)$")
_ROLE_PREFIX_RE = re.compile(r"^(?:\*\*)?(?:Править|Прочитать)(?::?\*\*)?(?:\s*:\s*|\s+)")
_RENDERED_OPENING_RE = re.compile(r"^- (`+)")
_BACKTICK_RUN_RE = re.compile(r"`+")
_POSITIVE_LINE_RE = re.compile(r"[1-9][0-9]*")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_KNOWN_EXT_NO_DOT_NAMES = {"Dockerfile", "Makefile"}


def parse_human_tokens(text: str) -> float:
    """Обратное к human_tokens() хука brief_cost: '51.2K' -> 51200.0.

    Разбор лоссовый: исходное число уже округлено до одного знака после точки
    в K/M, поэтому восстановленное значение несёт погрешность округления.
    """
    match = _NUM_RE.match(text.strip())
    if not match:
        raise ValueError(f"не похоже на human_tokens число: {text!r}")
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "K":
        return value * 1_000
    if unit == "M":
        return value * 1_000_000
    return value


@dataclass
class TokenBlock:
    """Разобранный блок токенов одного брифа."""

    main_by_model: dict = field(default_factory=dict)
    sidechain_by_model: dict = field(default_factory=dict)

    def main_total(self) -> float:
        return sum(sum(b.values()) for b in self.main_by_model.values())

    def sidechain_total(self) -> float:
        return sum(sum(b.values()) for b in self.sidechain_by_model.values())


def _fenced_lines(text: str) -> Iterator[tuple[str, bool]]:
    """Отметить строки блоков кода, включая сами ограничители."""
    fence = ""
    for line in text.splitlines():
        candidate = _FENCE_RE.match(line)
        if fence:
            if candidate and candidate[1].startswith(fence) and not candidate[2].strip(" \t"):
                fence = ""
            yield line, True
            continue
        if candidate and (candidate[1][0] == "~" or "`" not in candidate[2]):
            fence = candidate[1]
        yield line, bool(fence)


def _section_bodies(text: str, header: str) -> Iterator[list[tuple[str, bool]]]:
    """Строки секций между заголовками вне блоков кода."""
    body = None
    for line, fenced in _fenced_lines(text):
        if not fenced and line.startswith("## ") and body is not None:
            yield body
            body = None
        if not fenced and line.strip() == header and body is None:
            body = []
        elif body is not None:
            body.append((line, fenced))
    if body is not None:
        yield body


def _section_body(text: str, header: str) -> list[str]:
    return [line for line, fenced in next(_section_bodies(text, header), []) if not fenced]


def parse_token_block(text: str) -> TokenBlock | None:
    """Найти и разобрать блок «## Токены (этап solve-task)»; None, если его нет."""
    body = _section_body(text, TOKENS_HEADER)
    if not body:
        return None
    block = TokenBlock()
    target = block.main_by_model
    current_model = None
    for raw in body:
        line = raw.strip()
        if not line:
            continue
        if line == SIDECHAIN_MARK:
            target = block.sidechain_by_model
            current_model = None
            continue
        model_match = _MODEL_RE.match(line)
        if model_match:
            current_model = model_match.group(1).strip()
            continue
        buckets_match = _BUCKETS_RE.search(line)
        if buckets_match and current_model is not None:
            values = [parse_human_tokens(g) for g in buckets_match.groups()]
            target[current_model] = dict(zip(BUCKET_KEYS, values))
        # Строки «Всего: …» не парсим: это производная от бакетов, считаем сами.
    if not block.main_by_model and not block.sidechain_by_model:
        return None
    return block


def _paths_from_backtick(fragment: str) -> list[str]:
    """Путь из backtick-фрагмента вида 'path.py:19,48'; [] для не-пути."""
    fragment = fragment.strip()
    match = _PATH_LINE_RE.match(fragment)
    if match:
        return [match.group(1)]
    if _BARE_PATH_RE.match(fragment):
        return [fragment]
    return []


def _leading_path(token: str) -> str | None:
    """Путь из первого токена bullet'а: не все пути в брифах обёрнуты в backticks."""
    token = token.strip("`,;()")
    match = _LINE_SUFFIX_RE.match(token)
    if match:
        token = match.group(1)
    if "/" in token or re.search(r"\.\w+$", token):
        return token
    if token in _KNOWN_EXT_NO_DOT_NAMES:
        return token
    return None


def _parse_rendered_citation(row: str) -> tuple[str, int]:
    if not (opening := _RENDERED_OPENING_RE.match(row)):
        raise ValueError("Expected a citation at the start of a marked bullet")
    # In CommonMark the first matching run closes the span, including on malformed rows.
    closing = next(
        (run for run in _BACKTICK_RUN_RE.finditer(row, opening.end()) if run[0] == opening[1]),
        None,
    )
    if closing is None:
        raise ValueError("Unclosed citation in a marked bullet")
    suffix = row[closing.end():]
    reason = suffix.removeprefix(" — ")
    if reason == suffix or not reason.strip() or "\x00" in reason:
        raise ValueError("Expected an explanation after the citation")
    label = row[opening.end():closing.start()]
    if label.startswith(" ") and label.endswith(" "):
        label = label[1:-1]
    path, _, line = label.rpartition(":")
    if not path or "\x00" in path or not _POSITIVE_LINE_RE.fullmatch(line):
        raise ValueError("Expected path:line in a marked citation")
    return path, int(line)


def parse_rendered_citations(text: str, header: str) -> list[tuple[str, int]] | None:
    """Read ordered path/line pairs from a marked section, or None for historical Markdown."""
    bodies = list(_section_bodies(text, header))
    marked = next(
        (
            body for body in bodies
            if any(not fenced and line.strip() == VERIFIED_CITATIONS_MARKER for line, fenced in body)
        ),
        None,
    )
    if marked is None:
        return None
    rows = [line for line, _ in marked if line.strip()]
    if len(bodies) != 1 or rows[0].strip() != VERIFIED_CITATIONS_MARKER:
        raise ValueError(f"Misplaced or ambiguous citation marker under {header}")
    return [_parse_rendered_citation(row) for row in rows[1:]]


def extract_section_paths(text: str, header: str) -> set[str]:
    """Множество путей из bullet-секции брифа; строки '(dropped N: …)' пропускаются."""
    if (rendered := parse_rendered_citations(text, header)) is not None:
        return {path for path, _ in rendered}

    paths: set[str] = set()
    for raw in _section_body(text, header):
        line = raw.strip()
        if not line.startswith("-"):
            continue
        if re.match(r"^-\s*\(dropped\b", line):
            continue
        for fragment in _BACKTICK_RE.findall(line):
            paths.update(_paths_from_backtick(fragment))
        body = _ROLE_PREFIX_RE.sub("", line[1:].strip(), count=1)
        tokens = body.split(maxsplit=1)
        if tokens and (leading := _leading_path(tokens[0])):
            paths.add(leading)
    return paths


def has_section(text: str, header: str) -> bool:
    """Есть ли в тексте секция с таким заголовком.

    Нужна, чтобы отличить «бриф без секции ## Relevant code» (бриф непригоден
    как источник предсказаний) от «секция есть, но пуста» (валидное измерение
    с predicted=0). extract_section_paths оба случая отдаёт пустым множеством.
    """
    return any(not fenced and line.strip() == header for line, fenced in _fenced_lines(text))


def extract_task_key(filename: str, config: BriefQualityConfig) -> str | None:
    """Ключ задачи из имени файла брифа ('…-PRI-250-…' -> 'PRI-250').

    Паттерн — из конфига репозитория (`task_board.key_pattern`), иначе общий
    `[A-Z]+-\\d+`. Невалидный паттерн, как и в `task_keys.extract_task_keys`,
    даёт предупреждение и пустой результат, а не падение.
    """
    try:
        rx = re.compile(f"({config.key_pattern})", re.IGNORECASE)
    except re.error:
        log.warning("Невалидный key_pattern %r — ключ брифа не извлекается", config.key_pattern)
        return None
    match = rx.search(filename)
    return match.group(1).upper() if match else None


@dataclass
class BriefRecord:
    """Один бриф корпуса: ключ, токены и предсказанные пути."""

    filename: str
    task_key: str | None
    token_block: TokenBlock | None
    relevant_paths: set[str]
    test_paths: set[str]
    citation_error: str | None = None


def load_briefs(briefs_dir: pathlib.Path, config: BriefQualityConfig) -> list[BriefRecord]:
    """Прочитать весь корпус брифов каталога (по возрастанию имени файла)."""
    records: list[BriefRecord] = []
    for path in sorted(briefs_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        citation_error = None
        try:
            relevant_paths = extract_section_paths(text, RELEVANT_HEADER)
            test_paths = extract_section_paths(text, TEST_HEADER)
        except ValueError as error:
            citation_error = str(error)
            log.warning("Не удалось прочитать ссылки брифа %s (%s)", path, citation_error)
            relevant_paths, test_paths = set(), set()
        records.append(
            BriefRecord(
                filename=path.name,
                task_key=extract_task_key(path.name, config),
                token_block=parse_token_block(text),
                relevant_paths=relevant_paths,
                test_paths=test_paths,
                citation_error=citation_error,
            )
        )
    return records
