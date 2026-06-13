"""Хранилище задач доски в Postgres: эмбеддинги (pgvector) + BM25 (pg_search), RRF.

Отдельная таблица ``tasks`` (не code-``chunks``): у задач нет path/symbol/lines и
base/overlay-freshness. Зеркалит паттерн :class:`ChunkStore` — ленивый пул,
``register_vector`` на каждое соединение.
"""
from __future__ import annotations

import hashlib
import re

_BM25_STRIP = re.compile(r"[^\w\s]")


def _bm25_query(text: str) -> str:
    cleaned = _BM25_STRIP.sub(" ", text).strip()
    return cleaned or "____nomatch____"


def build_task_text(title: str | None, description: str | None, criteria: list[str] | None) -> str:
    """Текст задачи для эмбеддинга и BM25: заголовок + описание + критерии."""
    parts = [title or "", description or ""]
    if criteria:
        parts.append("\n".join(c for c in criteria if c))
    return "\n\n".join(p for p in parts if p).strip()


def task_content_hash(text: str) -> str:
    """Хэш нормализованного текста задачи (как Chunk.content_hash) — дедуп переэмбеда."""
    norm = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
