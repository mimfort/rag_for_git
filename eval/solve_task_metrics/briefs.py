"""Чтение корпуса брифов: блок токенов, пути секций, ключ задачи."""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field

TOKENS_HEADER = "## Токены (этап solve-task)"
RELEVANT_HEADER = "## Relevant code"
TEST_HEADER = "## Test exemplars"
SIDECHAIN_MARK = "В т.ч. sidechain-сабагент:"

BUCKET_KEYS = ("fresh_in", "output", "cache_write", "cache_read")

_NUM_RE = re.compile(r"^(\d+(?:\.\d+)?)([KM]?)$")
_MODEL_RE = re.compile(r"^Модель:\s*(.+)$")
_BUCKETS_RE = re.compile(
    r"fresh-in\s+(\S+)\s*·\s*out\s+(\S+)\s*·\s*cache-write\s+(\S+)\s*·\s*cache-read\s+(\S+)"
)
_KEY_RE = re.compile(r"(PRI-\d+)", re.IGNORECASE)

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PATH_LINE_RE = re.compile(r"^([\w./\-]+\.\w+):([\d,\-\s]+)$")
_BARE_PATH_RE = re.compile(r"^[\w./\-]+\.\w+$")
_LINE_SUFFIX_RE = re.compile(r"^(.+?):(\d[\d,\-\s]*)$")
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


def _section_body(text: str, header: str) -> list[str]:
    """Строки секции от её заголовка до следующего '## ' либо конца текста."""
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == header)
    except StopIteration:
        return []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return lines[start + 1 : end]


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


def extract_section_paths(text: str, header: str) -> set[str]:
    """Множество путей из bullet-секции брифа; строки '(dropped N: …)' пропускаются."""
    paths: set[str] = set()
    for raw in _section_body(text, header):
        line = raw.strip()
        if not line.startswith("-"):
            continue
        if re.match(r"^-\s*\(dropped\b", line):
            continue
        for fragment in _BACKTICK_RE.findall(line):
            paths.update(_paths_from_backtick(fragment))
        body = line[1:].strip()
        first_token = body.split()[0] if body.split() else ""
        leading = _leading_path(first_token)
        if leading:
            paths.add(leading)
    return paths


def extract_task_key(filename: str) -> str | None:
    """Ключ задачи из имени файла брифа ('…-PRI-250-…' -> 'PRI-250')."""
    match = _KEY_RE.search(filename)
    return match.group(1).upper() if match else None


@dataclass
class BriefRecord:
    """Один бриф корпуса: ключ, токены и предсказанные пути."""

    filename: str
    task_key: str | None
    token_block: TokenBlock | None
    relevant_paths: set[str]
    test_paths: set[str]


def load_briefs(briefs_dir: pathlib.Path) -> list[BriefRecord]:
    """Прочитать весь корпус брифов каталога (по возрастанию имени файла)."""
    records: list[BriefRecord] = []
    for path in sorted(briefs_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        records.append(
            BriefRecord(
                filename=path.name,
                task_key=extract_task_key(path.name),
                token_block=parse_token_block(text),
                relevant_paths=extract_section_paths(text, RELEVANT_HEADER),
                test_paths=extract_section_paths(text, TEST_HEADER),
            )
        )
    return records
