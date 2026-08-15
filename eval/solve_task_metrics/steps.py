"""Атрибуция взвешенной цены solve-task по под-шагам (офлайн, PRI-248).

Спайк PRI-246 измерил этап целиком, но не разбил его на шаги, поэтому не мог
доказать, где именно сидит расход. Атрибуция — чистая функция от транскрипта:
assistant-ход относится к под-шагу по тому, какой инструмент он вызвал.
Считается ретроспективно по уже накопленным транскриптам — ждать новых
прогонов не нужно.
"""
from __future__ import annotations

import pathlib

from . import endtoend
from .briefs import BUCKET_KEYS
from .cost import weighted

# Тул → под-шаг. Ключ — имя как оно приходит в транскрипте; MCP-тулы приходят
# с префиксом сервера, поэтому сопоставление идёт по суффиксу после '__'.
STEP_TOOLS = {
    "sync_board": "preflight",
    "get_board_config": "preflight",
    "get_board_targets": "preflight",
    "get_task": "gather",
    "get_task_context": "gather",
    "search_tasks": "gather",
    "search_codebase": "gather",
    "get_subsystem_summaries": "gather",
    "related_symbols": "gather",
    "callers": "gather",
    "definition": "gather",
    "implementations": "gather",
    "get_pr_diff": "gather",
}

BRIEFS_MARKER = "docs/superpowers/briefs/"
STATUS_MARKER = "reviewer status"
STEPS = ("preflight", "gather", "brief", "other")


def _tool_calls(line: dict) -> list:
    content = (line.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _short_name(name: str) -> str:
    return name.rsplit("__", 1)[-1] if name else ""


def classify_turn(line: dict) -> str:
    """Под-шаг assistant-хода по инструментам, которые он вызвал.

    Ход без tool_use относится к "other": текстовый ход не принадлежит
    ни одному под-шагу однозначно. Первый распознанный вызов решает.
    """
    for block in _tool_calls(line):
        name = block.get("name") or ""
        payload = block.get("input") or {}
        short = _short_name(name)
        if short in STEP_TOOLS:
            return STEP_TOOLS[short]
        if name == "Bash" and STATUS_MARKER in str(payload.get("command") or ""):
            return "preflight"
        if name in ("Write", "Edit") and BRIEFS_MARKER in str(payload.get("file_path") or ""):
            return "brief"
    return "other"


def brief_window_end(lines: list, start: int, end: int) -> int:
    """Граница фазы сборки брифа: сразу после первой записи под BRIEFS_MARKER.

    `find_windows` закрывает окно только следующим вызовом solve-task (или
    концом транскрипта), поэтому «сырое» окно включает весь брейншторм,
    спеку/план и реализацию, случившиеся ПОСЛЕ того, как бриф уже записан —
    именно эту границу уже проводит `plugin/hooks/brief_cost.py` (комментарий
    в его `run`: «окно ДО первой записи брифа»). Если запись брифа в окне не
    найдена — граница не сужается (fail-open: фаза не завершилась записью).
    """
    for index in range(start + 1, end):
        line = lines[index]
        if line.get("type") != "assistant":
            continue
        for block in _tool_calls(line):
            name = block.get("name") or ""
            payload = block.get("input") or {}
            if name in ("Write", "Edit") and BRIEFS_MARKER in str(payload.get("file_path") or ""):
                return index + 1
    return end


def attribute_window(lines: list, start: int, end: int) -> dict:
    """Бакеты токенов по под-шагам для assistant-ходов окна (start, end)."""
    by_step = {step: {key: 0.0 for key in BUCKET_KEYS} for step in STEPS}
    for line in lines[start + 1 : end]:
        if line.get("type") != "assistant":
            continue
        usage = (line.get("message") or {}).get("usage") or {}
        bucket = by_step[classify_turn(line)]
        bucket["fresh_in"] += float(usage.get("input_tokens") or 0)
        bucket["output"] += float(usage.get("output_tokens") or 0)
        bucket["cache_write"] += float(usage.get("cache_creation_input_tokens") or 0)
        bucket["cache_read"] += float(usage.get("cache_read_input_tokens") or 0)
    return by_step


def weighted_shares(by_step: dict) -> dict:
    """Доли взвешенной цены по под-шагам. Нулевая цена → нулевые доли."""
    values = {step: weighted(buckets) for step, buckets in by_step.items()}
    total = sum(values.values())
    if not total:
        return {step: 0.0 for step in by_step}
    return {step: value / total for step, value in values.items()}


SCOPES = ("phase", "session")


def _empty_scoped_slot() -> dict:
    return {scope: {step: {b: 0.0 for b in BUCKET_KEYS} for step in STEPS} for scope in SCOPES}


def scan_steps(root: pathlib.Path) -> dict:
    """Разбивка по под-шагам для каждой задачи из транскриптов под root.

    Каждая задача даёт два скоупа: "phase" — только фаза сборки брифа
    (окно сужено `brief_window_end`, это основная метрика PRI-248) и
    "session" — вся сессия до следующего вызова solve-task/конца транскрипта
    (старая метрика, сохранена для сопоставимости с PRI-246).
    """
    result: dict = {}
    if not root.exists():
        return result
    for path in sorted(root.glob("*/*.jsonl")):
        lines = endtoend._read_jsonl(path)
        if not lines:
            continue
        for key, start, end in endtoend.find_windows(lines):
            if not key:
                continue
            slot = result.setdefault(key, _empty_scoped_slot())
            phase_end = brief_window_end(lines, start, end)
            for scope, scope_end in (("phase", phase_end), ("session", end)):
                for step, buckets in attribute_window(lines, start, scope_end).items():
                    for bucket_key in BUCKET_KEYS:
                        slot[scope][step][bucket_key] += buckets[bucket_key]
    return result
