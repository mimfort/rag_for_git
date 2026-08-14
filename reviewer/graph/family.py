"""Семейство однотипных символов: «кто ещё такой же» для узла графа.

Два независимых сигнала:

- **inheritance** — подклассы и сиблинги по рёбрам ``IMPLEMENTS``. Надёжен
  после того, как наследование стало приходить из синтаксиса
  (:mod:`reviewer.graph.inherit`).
- **structural** — покрытие полного набора методов контракта с учётом
  унаследованных. Нужен потому, что ``typing.Protocol`` рёбер наследования
  не даёт ни при каком бэкенде: структурная типизация не выражается рёбрами.

Молчаливая пустота хуже ошибки: она неотличима от «семейства нет». Поэтому
результат всегда несёт список сработавших сигналов и признак полноты.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FamilyResult:
    """Семейство узла: члены, сработавшие сигналы, полнота ответа."""

    members: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    complete: bool = True
    note: str = ""


def effective_methods(node_id: str, own_by_node: dict[str, set[str]],
                      bases_map: dict[str, list[str]]) -> set[str]:
    """Простые имена методов класса: собственные ∪ унаследованные.

    Обход по цепочке ``IMPLEMENTS`` защищён от циклов: рёбра строятся
    эвристиками резолвинга имён, и цикл там возможен.
    """
    seen: set[str] = set()
    methods: set[str] = set()
    stack = [node_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        methods |= own_by_node.get(current, set())
        stack.extend(bases_map.get(current, []))
    return methods


def structural_matches(contract: str, contract_methods: set[str],
                       candidates: dict[str, set[str]]) -> list[str]:
    """Классы, покрывающие ВЕСЬ набор методов контракта (сам контракт исключён).

    Полное покрытие, а не частичное: девяти методов ``TaskBoardProvider``
    достаточно, чтобы совпадение не давало шума, а частичное покрытие
    притянуло бы любой класс с ``close()``.
    """
    if not contract_methods:
        return []
    return sorted(
        node for node, methods in candidates.items()
        if node != contract and contract_methods <= methods
    )


def merge_signals(node_id: str, inheritance: list[str],
                  structural: list[str]) -> FamilyResult:
    """Слить сигналы в один ответ, сохранив порядок и назвав источники."""
    members: list[str] = []
    for item in list(inheritance) + list(structural):
        if item != node_id and item not in members:
            members.append(item)
    signals: list[str] = []
    if inheritance:
        signals.append("inheritance")
    if structural:
        signals.append("structural")
    if not members:
        return FamilyResult(
            members=[], signals=signals, complete=False,
            note="семейство не найдено ни по наследованию, ни по структуре "
                 "контракта — это может значить и что его нет, и что сигналы слепы",
        )
    return FamilyResult(members=members, signals=signals, complete=True)
