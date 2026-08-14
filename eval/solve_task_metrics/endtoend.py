"""Полная цена задачи «под ключ»: ретроспективно по транскриптам сессий.

Осознанное решение: без нового рантайм-хука. Харнесс остаётся офлайн-инструментом,
не имеет рантайм-риска и работает на уже закрытых задачах.

Окно замера — от user-сообщения с маркерами вызова скилла solve-task до конца
транскрипта. Запись файла брифа в определении окна НЕ участвует вовсе: именно
эта связь и была дефектом старого замера.
"""
from __future__ import annotations

import json
import pathlib
import re

from . import cost
from .briefs import BUCKET_KEYS

SKILL_MARKER = "skills/solve-task"
BASE_DIR_MARKER = "Base directory for this skill:"

_KEY_RE = re.compile(r"(PRI-\d+)", re.IGNORECASE)


def _message_text(line: dict) -> str:
    """Текст сообщения: content строкой или списком блоков {text}."""
    content = (line.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def window_start(lines: list) -> tuple:
    """Первый вызов скилла solve-task с распознанным ключом задачи.

    Returns:
        (ключ задачи или None, индекс начала окна или -1).
    """
    for index, line in enumerate(lines):
        if line.get("type") != "user":
            continue
        text = _message_text(line)
        if BASE_DIR_MARKER not in text or SKILL_MARKER not in text:
            continue
        match = _KEY_RE.search(text)
        return (match.group(1).upper() if match else None, index)
    return (None, -1)


def aggregate_after(lines: list, start_idx: int) -> dict:
    """Сумма бакетов токенов по assistant-ходам после начала окна."""
    buckets = {key: 0.0 for key in BUCKET_KEYS}
    if start_idx < 0:
        return buckets
    for line in lines[start_idx + 1 :]:
        if line.get("type") != "assistant":
            continue
        usage = (line.get("message") or {}).get("usage") or {}
        buckets["fresh_in"] += float(usage.get("input_tokens") or 0)
        buckets["output"] += float(usage.get("output_tokens") or 0)
        buckets["cache_write"] += float(usage.get("cache_creation_input_tokens") or 0)
        buckets["cache_read"] += float(usage.get("cache_read_input_tokens") or 0)
    return buckets


def _read_jsonl(path: pathlib.Path) -> list:
    rows: list = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def scan_transcripts(root: pathlib.Path) -> dict:
    """Полная цена по задачам из транскриптов под root.

    Сессия без распознанного ключа задачи в измерение не входит. Несколько
    сессий одной задачи суммируются, их число сохраняется.
    """
    result: dict = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*.jsonl")):
        lines = _read_jsonl(path)
        if not lines:
            continue
        key, start = window_start(lines)
        if not key or start < 0:
            continue
        buckets = aggregate_after(lines, start)
        entry = result.setdefault(
            key,
            {"buckets": {b: 0.0 for b in BUCKET_KEYS}, "sessions": 0, "weighted": 0.0},
        )
        entry["buckets"] = cost.sum_buckets([entry["buckets"], buckets])
        entry["sessions"] += 1
        entry["weighted"] = cost.weighted(entry["buckets"])
    return result
