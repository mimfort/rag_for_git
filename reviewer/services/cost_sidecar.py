"""Чтение sidecar-файла с расходом окна ревью (PRI-247).

Расход снимает клиентский PreToolUse-хук плагина (plugin/hooks/review_cost.py)
и кладёт JSON по детерминированному пути от repo и номера PR; publish_review
читает его здесь. Канал файловый, поэтому работает, когда хук и reviewer-mcp
делят файловую систему (stdio-запуск). При удалённом MCP файла не будет —
это штатный случай «sidecar отсутствует», ревью публикуется без метаданных.

Путь ДУБЛИРУЕТ формулу хука: хук исполняется системным python3 и не может
импортировать пакет reviewer. Совпадение формул закреплено guard-тестом.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

SIDECAR_DIR = "reviewer-review-cost"
SIDECAR_VERSION = 1
# Замер старше суток относится к другому ревью того же PR — применять его нельзя.
MAX_AGE = timedelta(hours=24)


def sidecar_path(repo: str, pr: int) -> str:
    """Детерминированный путь sidecar от repo и номера PR."""
    safe = str(repo).replace("/", "_").replace("..", "_")
    return os.path.join(tempfile.gettempdir(), SIDECAR_DIR, f"{safe}-{pr}.json")


def _drop(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def read_cost_sidecar(repo: str, pr: int) -> dict | None:
    """Прочитать и удалить sidecar. Любая негодность → None (fail-open).

    Returns:
        {"model", "usage", "total_cost"} либо None.
    """
    path = sidecar_path(repo, pr)
    try:
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        _drop(path)
        if not isinstance(data, dict) or data.get("version") != SIDECAR_VERSION:
            return None
        if str(data.get("repo") or "") != str(repo) or int(data.get("pr") or -1) != int(pr):
            return None
        written = data.get("written_at")
        if written:
            try:
                stamp = datetime.fromisoformat(written)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - stamp > MAX_AGE:
                    return None
            except ValueError:
                return None
        return {
            "model": data.get("model"),
            "usage": data.get("usage"),
            "total_cost": data.get("total_cost"),
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("Sidecar расхода непригоден (%s): %s", path, exc)
        _drop(path)
        return None


def merge_metadata(explicit: dict, sidecar: dict | None) -> dict:
    """Слияние метаданных: явные аргументы клиента приоритетнее — по полям.

    Слияние именно пофайловое, а не «всё или ничего»: CLI, умеющий отдать
    model, но не расход, не должен терять расход из sidecar.
    """
    merged = dict(explicit)
    if not sidecar:
        return merged
    for key in ("model", "usage", "total_cost"):
        if merged.get(key) is None and sidecar.get(key) is not None:
            merged[key] = sidecar[key]
    return merged
