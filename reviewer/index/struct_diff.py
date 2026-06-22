"""Структурный diff символов: сопоставление сигнатур/символов до и после (tree-sitter).

Подаёт агенту компактную сводку контрактных изменений рядом с сырым unified-diff.
"""
from __future__ import annotations

import re

_DEF_RE = re.compile(r"^\s*(async\s+def|def|class)\s")
_WS_RE = re.compile(r"\s+")


def extract_signature(node_text: str) -> str | None:
    """Заголовок объявления (def/async def/class) из исходника символа.

    Сканирует до первой `:` на нулевой глубине скобок — корректно для
    многострочных сигнатур и аннотаций (`x: int` внутри скобок не считается
    концом заголовка). Декораторы и докстринги до `def`/`class` пропускаются.
    Возвращает строку с нормализованными пробелами или None, если заголовка нет.
    """
    lines = node_text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _DEF_RE.match(ln)), None)
    if start is None:
        return None
    rest = "\n".join(lines[start:])
    depth = 0
    end = None
    for j, ch in enumerate(rest):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            end = j
            break
    header = rest[: end + 1] if end is not None else rest
    return _WS_RE.sub(" ", header).strip()
