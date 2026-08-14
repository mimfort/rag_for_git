"""Семейство однотипных символов: «кто ещё такой же» для узла графа.

Два независимых сигнала:

- **inheritance** — подклассы и сиблинги по рёбрам ``IMPLEMENTS``. Надёжен
  после того, как наследование стало приходить из синтаксиса
  (:mod:`reviewer.graph.inherit`).
- **structural** — покрытие полного набора методов контракта с учётом
  унаследованных. Нужен потому, что ``typing.Protocol`` рёбер наследования
  не даёт ни при каком бэкенде: структурная типизация не выражается рёбрами.
  Не применяется на тонких контрактах (``contract_too_thin``) — иначе класс
  вроде ``CodexInstallError`` с единственным ``__init__`` собирает в
  «семейство» десятки посторонних классов.

Молчаливая пустота хуже ошибки: она неотличима от «семейства нет». Поэтому
результат всегда несёт список сработавших сигналов и признак полноты.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FamilyResult:
    """Семейство узла: члены, сработавшие сигналы, полнота ответа.

    ``complete=False`` значит не только «членов нет» — но и «часть сигналов
    сознательно не применялась» (см. ``contract_too_thin``); в обоих случаях
    ``note`` объясняет, что именно недосчитано, и шапка ответа обязана его
    показать (см. ``MCPReviewService._family_header``).
    """

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


MIN_CONTRACT_METHODS = 3


def significant_methods(methods: set[str]) -> set[str]:
    """Методы контракта без dunder-псевдонимов (``__init__`` и т.п.).

    Dunder-и почти не образуют содержательный контракт — ``__init__`` есть у
    любого класса, поэтому калибровка размера контракта их не считает.
    """
    return {m for m in methods if not (m.startswith("__") and m.endswith("__"))}


def contract_too_thin(contract_methods: set[str],
                      min_methods: int = MIN_CONTRACT_METHODS) -> bool:
    """Контракт слишком тонок, чтобы структурный сигнал не давал шума.

    Порог считается по не-dunder методам: узел с единственным ``__init__``
    или единственным ``close()`` структурно «находит» десятки посторонних
    классов (замер на этом репозитории: 49 и 28 «членов» соответственно, при
    6 из 105 классов с ≥10 членами вообще). Реальные контракты порог не
    задевает: у ``TaskBoardProvider`` 9 методов, у ``VCSProvider`` — 8.
    """
    return len(significant_methods(contract_methods)) < min_methods


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
                  structural: list[str], *,
                  structural_skipped: bool = False) -> FamilyResult:
    """Слить сигналы в один ответ, сохранив порядок и назвав источники.

    ``structural_skipped`` — структурный сигнал не считался вовсе (тонкий
    контракт, см. ``contract_too_thin``), а не «посчитан и не нашёл
    совпадений»; это обязано быть видно в ответе, а не молча выглядеть как
    отсутствующее совпадение.
    """
    members: list[str] = []
    for item in list(inheritance) + list(structural):
        if item != node_id and item not in members:
            members.append(item)
    signals: list[str] = []
    if inheritance:
        signals.append("inheritance")
    if structural:
        signals.append("structural")
    skip_note = (
        f"структурный сигнал не применялся: контракт тоньше "
        f"{MIN_CONTRACT_METHODS} значимых методов"
        if structural_skipped else ""
    )
    if not members:
        note = ("семейство не найдено ни по наследованию, ни по структуре "
                 "контракта — это может значить и что его нет, и что сигналы слепы")
        if skip_note:
            note = f"{note}; {skip_note}"
        return FamilyResult(members=[], signals=signals, complete=False, note=note)
    if skip_note:
        return FamilyResult(members=members, signals=signals, complete=False,
                            note=skip_note)
    return FamilyResult(members=members, signals=signals, complete=True)
