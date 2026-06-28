"""Скачивание и парсинг вложений задач (PRI-196): board-агностичный helper.

Чистая часть (`extract_text`) — без I/O, диспатч по формату; тестируется на байтах.
I/O-часть (`download`/`fetch_attachment`) — скачивание с лимитами и fail-soft.
Текстовые форматы (.md/.txt) декодируются напрямую, .docx через python-docx,
.pdf через pypdf; непарсимое/битое → None (сохраняются только метаданные).
"""
from __future__ import annotations

import logging
from io import BytesIO

log = logging.getLogger(__name__)

_TEXT_EXT = {".md", ".markdown", ".txt", ".text"}


def _ext(name: str) -> str:
    """Расширение файла в нижнем регистре с точкой (``spec.MD`` → ``.md``); ``""`` если нет."""
    i = (name or "").rfind(".")
    return name[i:].lower() if i >= 0 else ""


def _docx_text(data: bytes) -> str | None:
    from docx import Document
    doc = Document(BytesIO(data))
    text = "\n".join(p.text for p in doc.paragraphs)
    return text.strip() or None


def _pdf_text(data: bytes) -> str | None:
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    return text.strip() or None


def extract_text(name: str, mime: str | None, data: bytes) -> str | None:
    """Bytes вложения → текст (или None, если формат не парсится / парсинг упал)."""
    ext = _ext(name)
    try:
        if ext in _TEXT_EXT:
            return data.decode("utf-8", errors="replace").strip() or None
        if ext == ".docx":
            return _docx_text(data)
        if ext == ".pdf":
            return _pdf_text(data)
    except Exception:
        log.warning("attachments: парсинг %s упал (fail-soft)", name, exc_info=True)
        return None
    return None
