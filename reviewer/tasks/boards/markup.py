"""Конвертеры разметки описания задачи (PRI-213).

Общая валюта между ядром (reviewer/tasks/taskdoc.py) и досками — канонический
markdown. Доски, хранящие описание в HTML (YouGile), конвертируют его на запись
(md_to_html) и обратно на чтение (html_to_md). Доски с нативным markdown
(YouTrack) этот модуль не используют.

Подмножество узкое и намеренно неполное: заголовки, абзацы, списки, код,
ссылки, жирный. Всё остальное деградирует в текст, а не ломает конвертацию.
Только stdlib — в ядре нет и не заводится markdown-зависимостей.
"""
from __future__ import annotations

import html
import logging
import re

log = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\d+\.\s+(.*)$")
_FENCE_RE = re.compile(r"^```")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_SAFE_URL_SCHEME_RE = re.compile(r"^(https?://|mailto:)", re.IGNORECASE)
_STASH = "\x00%d\x00"


def _link(m: re.Match) -> str:
    """Собрать `<a href>` только для безопасной схемы URL.

    `javascript:`/`data:`-схемы — известный XSS-вектор в href (браузер
    доски может исполнить их по клику); текст, экранированный html.escape,
    от этого не защищает, т.к. схема не содержит HTML-метасимволов. Ссылка
    вне белого списка деградирует в исходный текст, а не ломает конвертацию.
    """
    text, url = m.group(1), m.group(2)
    if _SAFE_URL_SCHEME_RE.match(url):
        return f'<a href="{url}">{text}</a>'
    return m.group(0)


def _inline(text: str) -> str:
    """Экранировать текст и разложить инлайн-разметку в HTML.

    Инлайн-код вынимается ПЕРВЫМ в плейсхолдеры, поэтому его содержимое
    экранируется целиком и не участвует в остальных заменах.
    """
    stash: list[str] = []

    def _keep(m: re.Match) -> str:
        stash.append(f"<code>{html.escape(m.group(1))}</code>")
        return _STASH % (len(stash) - 1)

    out = html.escape(_INLINE_CODE_RE.sub(_keep, text))
    out = _LINK_RE.sub(_link, out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    for i, chunk in enumerate(stash):
        out = out.replace(_STASH % i, chunk)
    return out


def md_to_html(md: str) -> str:
    """Канонический markdown → узкий HTML для доски, хранящей описание в HTML."""
    if not md:
        return ""
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    list_tag = ""
    code: list[str] | None = None

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if items:
            body = "".join(f"<li>{_inline(i)}</li>" for i in items)
            out.append(f"<{list_tag}>{body}</{list_tag}>")
            items.clear()
            list_tag = ""

    for line in md.splitlines():
        if _FENCE_RE.match(line.strip()):
            if code is None:
                flush_para()
                flush_list()
                code = []
            else:
                out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        if not line.strip():
            flush_para()
            flush_list()
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush_para()
            flush_list()
            level = 2 if len(heading.group(1)) <= 2 else 3
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            continue
        bullet = _BULLET_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if bullet or ordered:
            flush_para()
            tag = "ul" if bullet else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            items.append((bullet or ordered).group(1).strip())
            continue
        flush_list()
        para.append(line.strip())

    if code is not None:                      # незакрытый fence — не теряем текст
        out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
    flush_para()
    flush_list()
    return "".join(out)
