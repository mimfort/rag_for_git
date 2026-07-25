"""Узкая конвертация YFM-разметки Yandex Tracker в markdown.

Покрывает конструкции, которые ломают markdown-инвариант normalize: блоки кода ``%%``,
ссылки ``((url текст))`` и cut ``<{Заголовок ... }>``. Всё остальное (макросы, цветной
текст) остаётся как есть — читаемо и не теряется. Функция никогда не бросает.
"""
from __future__ import annotations

import re

_CODE_RE = re.compile(r"%%(?:\((?P<lang>[^)\n]*)\))?\n(?P<body>.*?)\n%%", re.DOTALL)
_LINK_RE = re.compile(r"\(\((?P<url>\S+?)(?:\s+(?P<text>[^)]+?))?\)\)")
_CUT_RE = re.compile(r"<\{(?P<title>[^\n]*)\n(?P<body>.*?)\n?\}>", re.DOTALL)


def _code(match: re.Match[str]) -> str:
    lang = (match.group("lang") or "").strip()
    return f"```{lang}\n{match.group('body')}\n```"


def _link(match: re.Match[str]) -> str:
    url = match.group("url")
    text = (match.group("text") or "").strip()
    return f"[{text}]({url})" if text else f"<{url}>"


def _cut(match: re.Match[str]) -> str:
    title = match.group("title").strip()
    body = match.group("body").strip()
    return f"**{title}**\n\n{body}" if title else body


def yfm_to_md(text: str) -> str:
    """YFM → markdown; при любой ошибке возвращает исходный текст."""
    if not text:
        return ""
    try:
        out = _CODE_RE.sub(_code, text)
        out = _CUT_RE.sub(_cut, out)
        return _LINK_RE.sub(_link, out)
    except Exception:
        return text
