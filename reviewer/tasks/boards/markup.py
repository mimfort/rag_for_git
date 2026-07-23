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
from html.parser import HTMLParser

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


_BLOCK_TAGS = {"p", "div", "section", "article", "blockquote", "tr"}
_HEADINGS = {"h1": 2, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class _MarkdownWriter(HTMLParser):
    """HTML → markdown: узкое подмножество; неизвестные теги прозрачны (текст цел).

    Пробелы и переносы ВНУТРИ текста сохраняются как есть: почти все описания на
    досках — markdown, лежащий в HTML-поле обычным текстом, и схлопывание пробелов
    склеило бы его в один абзац.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._lists: list[dict] = []
        self._pre = False
        self._href: str | None = None
        self._link_at: int | None = None

    def _newblock(self) -> None:
        text = "".join(self._parts)
        if not text:
            return
        if text.endswith("\n\n"):
            return
        self._parts.append("\n" if text.endswith("\n") else "\n\n")

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _HEADINGS:
            self._newblock()
            self._parts.append("#" * _HEADINGS[tag] + " ")
        elif tag == "br":
            self._parts.append("\n")
        elif tag in ("ul", "ol"):
            self._newblock()
            self._lists.append({"ordered": tag == "ol", "n": 0})
        elif tag == "li":
            lst = self._lists[-1] if self._lists else {"ordered": False, "n": 0}
            lst["n"] += 1
            text = "".join(self._parts)
            if text and not text.endswith("\n"):
                self._parts.append("\n")
            self._parts.append(f"{lst['n']}. " if lst["ordered"] else "- ")
        elif tag == "pre":
            self._newblock()
            self._parts.append("```\n")
            self._pre = True
        elif tag == "code" and not self._pre:
            self._parts.append("`")
        elif tag in ("strong", "b"):
            self._parts.append("**")
        elif tag in ("em", "i"):
            self._parts.append("*")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_at = len(self._parts)
        elif tag in _BLOCK_TAGS:
            self._newblock()

    def handle_endtag(self, tag: str) -> None:
        if tag in _HEADINGS or tag in _BLOCK_TAGS:
            self._newblock()
        elif tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            self._newblock()
        elif tag == "pre":
            self._pre = False
            if not "".join(self._parts).endswith("\n"):
                self._parts.append("\n")
            self._parts.append("```")
            self._newblock()
        elif tag == "code" and not self._pre:
            self._parts.append("`")
        elif tag in ("strong", "b"):
            self._parts.append("**")
        elif tag in ("em", "i"):
            self._parts.append("*")
        elif tag == "a":
            at = self._link_at if self._link_at is not None else len(self._parts)
            text = "".join(self._parts[at:]).strip()
            del self._parts[at:]
            href = self._href
            if href and text and text != href:
                self._parts.append(f"[{text}]({href})")
            else:
                self._parts.append(href or text)
            self._href = None
            self._link_at = None

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def result(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_md(html_text: str) -> str:
    """HTML-описание доски → markdown. Терпима к чужому дереву; НИКОГДА не бросает.

    Вход без тегов (markdown, лежащий в HTML-поле как текст) возвращается как есть,
    только с разэкранированными сущностями. Ограничение: HTML-теги, написанные
    человеком внутри инлайн-кода прямо в UI доски, неотличимы от настоящей разметки
    и будут съедены. Для текста, записанного через create_task, этого не случается —
    md_to_html экранирует содержимое кода.
    """
    if not html_text:
        return ""
    if "<" not in html_text:
        return html.unescape(html_text)
    writer = _MarkdownWriter()
    try:
        writer.feed(html_text)
        writer.close()
        return writer.result()
    except Exception:
        log.warning("markup: HTML не разобран — отдаём исходный текст", exc_info=True)
        return html_text
