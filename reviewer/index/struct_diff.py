"""Структурный diff символов: сопоставление сигнатур/символов до и после (tree-sitter).

Подаёт агенту компактную сводку контрактных изменений рядом с сырым unified-diff.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from reviewer.index.chunker import chunk_python

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


@dataclass
class SymbolChange:
    """Одно структурное изменение символа между base и head."""

    kind: str               # "signature_changed" | "added" | "removed"
    fqn: str                # напр. "A.m" или "foo"
    symbol_kind: str        # class | method | function
    old_sig: str | None     # заголовок до (для removed/signature_changed)
    new_sig: str | None     # заголовок после (для added/signature_changed)
    line: int | None        # head-строка для added/changed; base-строка для removed


def _symbol_map(path: str, source: bytes | None) -> dict:
    """fqn -> Chunk по исходнику (tree-sitter). Fail-soft: {} при пустом/битом вводе."""
    if not source:
        return {}
    try:
        return {ch.symbol_fqn: ch for ch in chunk_python(path, source)}
    except Exception:
        return {}


def diff_symbols(
    path: str, base_source: bytes | None, head_source: bytes
) -> list[SymbolChange]:
    """Структурный diff символов файла: add / remove / signature-change.

    Сопоставляет символы base и head по fqn. Чисто телесные правки (сигнатура
    не менялась) НЕ репортятся — это и даёт компактность. base_source=None →
    все символы head как added (политику пропуска added-файлов реализует
    вызывающий). Не бросает исключений.

    Предположение: ``chunk_python`` возвращает чанки только для
    ``function_definition`` / ``class_definition``, поэтому ``extract_signature``
    ожидаемо возвращает non-None для реального чанка. Условие
    ``if old_sig and new_sig`` — защитное: если сигнатура всё же не
    извлеклась, signature-change для данной пары не репортится.
    """
    base = _symbol_map(path, base_source)
    head = _symbol_map(path, head_source)
    changes: list[SymbolChange] = []

    for fqn, old in base.items():
        new = head.get(fqn)
        if new is None:
            changes.append(SymbolChange(
                "removed", fqn, old.kind, extract_signature(old.text), None,
                old.start_line))
            continue
        old_sig, new_sig = extract_signature(old.text), extract_signature(new.text)
        if old_sig and new_sig and old_sig != new_sig:
            changes.append(SymbolChange(
                "signature_changed", fqn, new.kind, old_sig, new_sig,
                new.start_line))

    for fqn, new in head.items():
        if fqn not in base:
            changes.append(SymbolChange(
                "added", fqn, new.kind, None, extract_signature(new.text),
                new.start_line))

    return changes
