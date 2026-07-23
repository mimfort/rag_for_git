"""Каноническая структура задачи: TaskDoc → markdown (PRI-213).

Board-agnostic ядро: структура описания задаётся здесь ОДИН раз для всех досок.
Провайдер получает готовый markdown и лишь конвертирует его в формат своего
транспорта (см. reviewer/tasks/boards/markup.py). Модуль чистый: без I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TaskDoc:
    """Поля новой задачи. title в тело описания НЕ входит — это отдельное поле доски."""

    title: str
    problem: str = ""
    steps: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    context: str | None = None


def _numbered(items: list[str] | None) -> str:
    """Нумерованный список; пустые и пробельные элементы отбрасываются."""
    cleaned = [s.strip() for s in (items or []) if s and s.strip()]
    return "\n".join(f"{i}. {s}" for i, s in enumerate(cleaned, 1))


def render_markdown(doc: TaskDoc) -> str:
    """Канонический markdown описания: фиксированный порядок секций, пустые опускаются.

    Секции: Проблема → Что сделать → Критерии приёмки → Контекст. Без эмодзи и
    декоративных разделителей — текст читают и человек в UI доски, и LLM из стора.
    """
    blocks: list[str] = []
    if (doc.problem or "").strip():
        blocks.append("## Проблема\n\n" + doc.problem.strip())
    steps = _numbered(doc.steps)
    if steps:
        blocks.append("## Что сделать\n\n" + steps)
    criteria = _numbered(doc.criteria)
    if criteria:
        blocks.append("## Критерии приёмки\n\n" + criteria)
    if (doc.context or "").strip():
        blocks.append("## Контекст\n\n" + doc.context.strip())
    return "\n\n".join(blocks)
