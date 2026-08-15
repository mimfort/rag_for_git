"""Общая агрегация транскрипта сессии для хуков плагина.

Исполняется системным python3 (только stdlib): импорт из пакета ``reviewer``
здесь запрещён — пакет в этом интерпретаторе не установлен.
"""
from __future__ import annotations

import glob
import json
import os

BASE_DIR_MARKER = "Base directory for this skill:"

# Веса бакетов относительно input-токена (спайк PRI-246). Единица результата —
# input-token equivalent: условные единицы, НЕ доллары.
WEIGHTS = {"fresh_in": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1}


def empty_bucket() -> dict:
    return {"fresh_in": 0, "output": 0, "cache_write": 0, "cache_read": 0}


def weigh(bucket: dict) -> float:
    """Взвешенная стоимость бакета в условных единицах."""
    return round(sum(WEIGHTS[k] * int(bucket.get(k) or 0) for k in WEIGHTS), 6)


def read_jsonl(path) -> list:
    """Прочитать JSONL; битые строки пропускаются, ошибка чтения → []."""
    rows: list = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def message_text(line: dict) -> str:
    """Текст сообщения: message.content как строка или список блоков {text}."""
    content = (line.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b["text"] for b in content
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def find_window_start(lines: list, skill_marker: str) -> int:
    """Индекс последнего user-сообщения с маркерами скилла; -1 если нет."""
    start = -1
    for i, line in enumerate(lines):
        if line.get("type") != "user":
            continue
        text = message_text(line)
        if BASE_DIR_MARKER in text and skill_marker in text:
            start = i
    return start


def aggregate_usage(lines: list, start_idx: int) -> tuple:
    """Сумма 4 бакетов токенов по model для assistant-ходов после start_idx.

    Returns:
        (main_by_model, sidechain_by_model); sidechain — ходы с isSidechain=True.
    """
    def _add(bucket: dict, usage: dict) -> None:
        bucket["fresh_in"] += int(usage.get("input_tokens") or 0)
        bucket["output"] += int(usage.get("output_tokens") or 0)
        bucket["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
        bucket["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)

    by_model: dict = {}
    sidechain: dict = {}
    for line in lines[start_idx + 1:]:
        if line.get("type") != "assistant":
            continue
        message = line.get("message") or {}
        usage = message.get("usage") or {}
        model = message.get("model") or "unknown"
        target = sidechain if line.get("isSidechain") else by_model
        _add(target.setdefault(model, empty_bucket()), usage)
    return (
        {m: b for m, b in by_model.items() if any(b.values())},
        {m: b for m, b in sidechain.items() if any(b.values())},
    )


def resolve_transcript(payload: dict) -> tuple:
    """Путь к транскрипту и способ его получения.

    Сначала payload['transcript_path'] (общее поле хуков), затем реконструкция
    по session_id в ~/.claude/projects/<slug>/<session_id>.jsonl. Второй путь
    оставлен намеренно: он снимает зависимость от того, отдаёт ли конкретное
    событие хука transcript_path.

    Returns:
        (путь, "payload"|"session_id") либо (None, None).
    """
    direct = payload.get("transcript_path")
    if direct and os.path.isfile(direct):
        return (direct, "payload")
    session_id = payload.get("session_id")
    if session_id:
        pattern = os.path.join(
            os.path.expanduser("~"), ".claude", "projects", "*", f"{session_id}.jsonl")
        matches = sorted(glob.glob(pattern))
        if matches:
            return (matches[0], "session_id")
    return (None, None)
