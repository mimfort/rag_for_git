"""JSONL-лог решений ревью (вердикты verify и публикации) для анализа ложных срабатываний."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reviewer.vcs.base import Finding

logger = logging.getLogger(__name__)

_MSG_MAX = 500  # максимальная длина поля message в записи лога


class VerdictLog:
    """JSONL-лог решений ревью (вердикты verify и публикации) для анализа ложных срабатываний.

    Каждая запись — одна строка JSON (ensure_ascii=False) со следующими полями:
      - ts          — ISO-8601 UTC
      - pr          — номер PR
      - event       — "verdict" | "published"
      - fingerprint — Finding.fingerprint()
      - file        — путь к файлу
      - line        — номер строки (int или null)
      - category    — категория находки
      - severity    — low/medium/high/critical
      - confidence  — 0..1
      - message     — текст (обрезан до 500 символов)
    Для "verdict":
      - is_real     — прошла ли находка проверку
      - source      — источник ("agentic" и т.д.)
    Для "published":
      - inline      — true = inline-комментарий, false = ушло в сводку
    """

    def __init__(self, path: str, pr: int = 0) -> None:
        """
        Args:
            path: путь к JSONL-файлу (создаётся при необходимости).
            pr:   номер PR; 0 = неизвестен.
        """
        self._path = path
        self._pr = pr
        self._lock = threading.Lock()

        # создать директорию, если нужно
        try:
            dirname = os.path.dirname(path)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("VerdictLog: не удалось создать директорию для %r: %s", path, exc)

    # ------------------------------------------------------------------
    # публичный API
    # ------------------------------------------------------------------

    def log_verdict(self, finding: Finding, is_real: bool, source: str = "agentic") -> None:
        """Записать результат верификации находки.

        Args:
            finding: находка из reviewer/vcs/base.py.
            is_real: True — находка подтверждена, False — отклонена.
            source:  источник вердикта (по умолчанию "agentic").
        """
        record = self._base_record(finding, "verdict")
        record["is_real"] = is_real
        record["source"] = source
        self._write(record)

    def log_published(self, finding: Finding, inline: bool) -> None:
        """Записать факт публикации находки.

        Args:
            finding: находка из reviewer/vcs/base.py.
            inline:  True = inline-комментарий на строку диффа, False = ушло в сводку.
        """
        record = self._base_record(finding, "published")
        record["inline"] = inline
        self._write(record)

    # ------------------------------------------------------------------
    # внутренние методы
    # ------------------------------------------------------------------

    def _base_record(self, finding: Finding, event: str) -> dict:
        """Сформировать базовый словарь записи из полей Finding."""
        return {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "pr": self._pr,
            "event": event,
            "fingerprint": finding.fingerprint(),
            "file": finding.file,
            "line": finding.line,
            "category": finding.category,
            "severity": finding.severity,
            "confidence": finding.confidence,
            "message": (finding.message[:_MSG_MAX] if finding.message else ""),
        }

    def _write(self, record: dict) -> None:
        """Сериализовать запись в JSONL и дозаписать в файл (потокобезопасно)."""
        try:
            line = json.dumps(record, ensure_ascii=False)
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("VerdictLog: не удалось записать в %r: %s", self._path, exc)
