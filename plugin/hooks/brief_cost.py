"""PostToolUse-хук: дописывает в бриф solve-task расход LLM-токенов на этап.

Запускается системным python3 (только stdlib). Считает детерминированно по
транскрипту сессии; долларов не показывает. Любая ошибка → no-op (exit 0).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    # noqa: F401 — BASE_DIR_MARKER/message_text ре-экспортируются как публичное API
    from _transcript import (  # noqa: E402,F401
        BASE_DIR_MARKER, aggregate_usage, message_text as _message_text,
        read_jsonl as _read_jsonl,
    )
    from _transcript import find_window_start as _find_window_start  # noqa: E402
except ImportError:
    # fail-open: недоступный/битый общий модуль не должен ронять хук с traceback
    # и ненулевым exit code — весь функционал ниже становится тихим no-op'ом.
    BASE_DIR_MARKER = None
    aggregate_usage = None
    _message_text = None
    _read_jsonl = None
    _find_window_start = None

HEADER = "## Токены (этап solve-task)"


def human_tokens(n: int) -> str:
    """Человекочитаемое число токенов: 9900→'9.9K', 164000→'164K', 14.2e6→'14.2M'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        value, unit = n / 1000, "K"
    else:
        value, unit = n / 1_000_000, "M"
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{text}{unit}"


def _format_bucket(model: str, b: dict) -> list[str]:
    """Строки описания одной модели из бакета токенов."""
    return [
        f"Модель: {model}",
        (
            f"fresh-in {human_tokens(b['fresh_in'])} · "
            f"out {human_tokens(b['output'])} · "
            f"cache-write {human_tokens(b['cache_write'])} · "
            f"cache-read {human_tokens(b['cache_read'])}"
        ),
    ]


def _bucket_total(b: dict) -> int:
    return b["fresh_in"] + b["output"] + b["cache_write"] + b["cache_read"]


def render_block(by_model: dict, sidechain: dict | None = None) -> str:
    """Текст блока «## Токены (этап solve-task)» (без хвостового перевода строки)."""
    lines = [HEADER]
    total = 0
    for model, b in by_model.items():
        lines.extend(_format_bucket(model, b))
        total += _bucket_total(b)
    # «Всего» — грандтотал этапа (главный агент + sidechain-сабагент), чтобы
    # подпись «В т.ч. sidechain-сабагент» ниже была корректной: sidechain
    # действительно входит в «Всего», а не идёт отдельной, не учтённой суммой.
    if sidechain:
        total += sum(_bucket_total(b) for b in sidechain.values())
    lines.append(f"Всего: {human_tokens(total)} токенов")
    if sidechain:
        side_total = 0
        lines.append("")
        lines.append("В т.ч. sidechain-сабагент:")
        for model, b in sidechain.items():
            lines.extend(_format_bucket(model, b))
            side_total += _bucket_total(b)
        lines.append(f"Sidechain всего: {human_tokens(side_total)} токенов")
    return "\n".join(lines)


def has_block(text: str) -> bool:
    """True, если бриф уже содержит блок токенов (замер запечатан)."""
    return any(line.strip() == HEADER for line in text.splitlines())


def upsert_block(text: str, block: str) -> str:
    """Заменить существующий блок по HEADER либо дописать в конец. Идемпотентно."""
    block = block.rstrip("\n")
    if not has_block(text):
        body = text.rstrip("\n")
        return f"{body}\n\n{block}\n"
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip() == HEADER:
            i += 1
            while i < n and not lines[i].startswith("## "):
                i += 1
            out.extend(block.splitlines())
            if i < n:
                out.append("")
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out).rstrip("\n") + "\n"


SKILL_MARKER = "skills/solve-task"


def find_window_start(lines: list) -> int:
    """Индекс последнего user-сообщения с маркерами solve-task; -1 если нет."""
    return _find_window_start(lines, SKILL_MARKER)


def read_flag(text) -> bool:
    """True только если solve_task.brief_token_cost == true.

    Хук исполняется системным python3, где PyYAML может отсутствовать → yaml,
    если доступен, иначе минимальный stdlib-разбор задокументированного формата.
    """
    if not text:
        return False
    try:
        import yaml
    except ImportError:
        return _read_flag_fallback(text)
    try:
        data = yaml.safe_load(text) or {}
    except Exception:
        return False
    block = data.get("solve_task") if isinstance(data, dict) else None
    return isinstance(block, dict) and block.get("brief_token_cost") is True


def _read_flag_fallback(text: str) -> bool:
    """Stdlib-разбор флага: inline `{...}` или block-style под `solve_task:`."""
    if re.search(r"solve_task:\s*\{[^}]*brief_token_cost:\s*true\b", text):
        return True
    in_block = False
    for line in text.splitlines():
        if re.match(r"^solve_task:\s*(#.*)?$", line):
            in_block = True
            continue
        if in_block:
            if line and not line[0].isspace():
                in_block = False
                continue
            if re.match(r"^\s+brief_token_cost:\s*true\b", line):
                return True
    return False


def _under_briefs(path: str) -> bool:
    norm = os.path.normpath(path).replace(os.sep, "/")
    return "/docs/superpowers/briefs/" in norm or norm.startswith("docs/superpowers/briefs/")


def _find_review_yml(cwd: str):
    current = os.path.abspath(cwd)
    while True:
        candidate = os.path.join(current, ".review.yml")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _write_text(path, text) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run(payload: dict) -> int:
    """Оркестрация хука. Всегда возвращает 0 (fail-open)."""
    try:
        if _read_jsonl is None:
            # _transcript недоступен/битый (см. импорт выше) — тихий no-op.
            return 0
        if os.environ.get("BRIEF_COST_DEBUG"):
            sys.stderr.write("brief_cost payload keys: " + ",".join(sorted(payload)) + "\n")
        file_path = (payload.get("tool_input") or {}).get("file_path") or ""
        if not _under_briefs(file_path):
            return 0
        # .review.yml ищем вверх от каталога брифа (file_path гарантированно
        # непуст после path-guard выше).
        start_dir = os.path.dirname(os.path.abspath(file_path))
        yml_path = _find_review_yml(start_dir)
        if not read_flag(_read_text(yml_path) if yml_path else None):
            return 0
        lines = _read_jsonl(payload.get("transcript_path") or "")
        if not lines:
            return 0
        brief = _read_text(file_path)
        if brief is None:
            return 0
        # Печать: блок описывает завершённый этап сборки контекста, то есть окно
        # ДО первой записи брифа. Пересчёт при повторной правке файла втянул бы
        # в число всё, что произошло после (брейншторм, план, реализация).
        if has_block(brief):
            return 0
        start = find_window_start(lines)
        if start < 0:
            return 0
        by_model, sidechain = aggregate_usage(lines, start)
        if not by_model and not sidechain:
            return 0
        _write_text(file_path, upsert_block(brief, render_block(by_model, sidechain)))
    except Exception:
        return 0
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    return run(payload)


if __name__ == "__main__":
    sys.exit(main())
