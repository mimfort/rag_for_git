"""Соглашение об именах ref для base-индекса по ветке.

ref в Postgres — дискриминатор вида `вид:значение`: 'base' / 'base:<branch>' / 'pr:<n>'.
Git запрещает ':' в именах веток, поэтому парсинг 'base:release/v1' однозначен.
Пустая ветка → legacy 'base' (обратная совместимость до миграции).
"""
from __future__ import annotations


def base_ref(branch: str) -> str:
    """Ключ base-индекса ветки: '' → 'base'; 'main' → 'base:main'."""
    return f"base:{branch}" if branch else "base"
