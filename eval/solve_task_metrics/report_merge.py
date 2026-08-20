"""Слияние генерируемой и ручной частей markdown-отчёта (PRI-265).

Прогон харнесса писал отчёт целиком и уничтожал накопленный ручной разбор
приёмок — единственное место, где живут числа и оговорки прошлых замеров.
Граница проводится ЯВНЫМ маркером: догадка о том, где кончается генерируемое,
— тот же класс молчаливой потери, который этот модуль чинит.
"""
from __future__ import annotations

import pathlib

MARKER = "<!-- generated:end — ниже ручные разделы, прогон их не трогает -->"


class MarkerMissing(Exception):
    """В существующем отчёте нет маркера границы: сливать не с чем."""


def manual_tail(existing: str) -> str:
    """Ручной хвост отчёта — срезом от маркера включительно.

    Побайтовость обеспечена конструкцией (срез исходной строки), а не
    аккуратностью вызывающего: пересобранный текст рано или поздно разойдётся
    с исходным на пустой строке или переносе.
    """
    if not existing:
        return ""
    index = existing.find(MARKER)
    return "" if index < 0 else existing[index:]


def merge(generated: str, existing: str) -> str:
    """Генерируемая часть + маркер + ручной хвост дословно."""
    tail = manual_tail(existing)
    head = generated.rstrip("\n")
    if not tail:
        return f"{head}\n\n{MARKER}\n"
    return f"{head}\n\n{tail}"


def ensure_mergeable(path: pathlib.Path) -> None:
    """Проверить, что отчёт можно перезаписать без потери ручной части.

    Вызывать ДО прогона, а не при записи: запись стоит после дорогого прогона
    (Voyage, обход графа), и отказ на записи означал бы «прогон отработал,
    результат записать нельзя» — потеря того же класса.
    """
    if not path.exists():
        return
    if MARKER in path.read_text(encoding="utf-8"):
        return
    raise MarkerMissing(
        f"В отчёте {path} нет маркера границы, прогон бы стёр ручные разделы.\n"
        f"Вставьте строку-маркер между генерируемой частью и первым ручным\n"
        f"разделом и повторите прогон:\n{MARKER}"
    )


def merge_file(path: pathlib.Path, generated: str) -> str:
    """Итоговый текст отчёта с сохранённым хвостом. Ничего не пишет."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    return merge(generated, existing)
