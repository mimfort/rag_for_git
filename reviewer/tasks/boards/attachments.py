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

# MIME-типы для фолбэка, когда расширение не идентифицирует формат
_TEXT_MIME = {"text/markdown", "text/x-markdown", "text/plain"}
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MIME = "application/pdf"


def _ext(name: str) -> str:
    """Расширение файла в нижнем регистре с точкой (``spec.MD`` → ``.md``); ``""`` если нет."""
    i = (name or "").rfind(".")
    return name[i:].lower() if i >= 0 else ""


def _kind(ext: str, mime: str | None) -> str | None:
    """Определяет вид формата: ``"text"`` / ``"docx"`` / ``"pdf"`` / ``None``.

    Приоритет: расширение файла (первичное) → mime-тип (фолбэк при неизвестном расширении).
    """
    # --- диспатч по расширению (первичный) ---
    if ext in _TEXT_EXT:
        return "text"
    if ext == ".docx":
        return "docx"
    if ext == ".pdf":
        return "pdf"
    # --- фолбэк по mime (игнорируем параметры вида "; charset=utf-8") ---
    if mime:
        base_mime = mime.split(";")[0].strip().lower()
        if base_mime in _TEXT_MIME:
            return "text"
        if base_mime == _DOCX_MIME:
            return "docx"
        if base_mime == _PDF_MIME:
            return "pdf"
    return None


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
    """Bytes вложения → текст (или None, если формат не парсится / парсинг упал).

    Диспатч: расширение файла (первичное) → mime-тип (фолбэк при неизвестном расширении).
    """
    kind = _kind(_ext(name), mime)
    try:
        if kind == "text":
            return data.decode("utf-8", errors="replace").strip() or None
        if kind == "docx":
            return _docx_text(data)
        if kind == "pdf":
            return _pdf_text(data)
    except Exception:
        log.warning("attachments: парсинг %s упал (fail-soft)", name, exc_info=True)
        return None
    return None


def download(client, url: str, *, timeout: float, max_bytes: int) -> bytes | None:
    """Скачать файл по url через httpx-совместимый client. None при skip/сбое (fail-soft)."""
    try:
        resp = client.get(url, timeout=timeout)
        resp.raise_for_status()
        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) > max_bytes:
            log.warning("attachments: %s > max_bytes (%s) — skip", url, cl)
            return None
        data = resp.content
        if len(data) > max_bytes:
            log.warning("attachments: %s превысил max_bytes при чтении — skip", url)
            return None
        return data
    except Exception:
        log.warning("attachments: скачивание %s упало (fail-soft)", url, exc_info=True)
        return None


def fetch_attachment(client, *, name: str, mime: str | None, size, url: str,
                     timeout: float, max_bytes: int, store_chars: int) -> dict:
    """Скачать+распарсить одно вложение → {name, mime_type, size, content_text|None}."""
    content_text: str | None = None
    data = download(client, url, timeout=timeout, max_bytes=max_bytes)
    if data is not None:
        text = extract_text(name, mime, data)
        if text:
            content_text = text[:store_chars]
    return {"name": name, "mime_type": mime, "size": size, "content_text": content_text}
