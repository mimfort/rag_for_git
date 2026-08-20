"""Контекстное ядро задачи: файлы, которые надо ПРОЧИТАТЬ, а не изменить.

Знаменатель core-recall — только изменённые файлы, поэтому контракт, соседний
адаптер или образец для нового кода в него не входят: recall их не штрафует,
а precision — штрафует. Контекстное ядро выводится из графа: соседи символов,
которых коснулся дифф задачи.

Модуль ЧИСТЫЙ. Обход графа приходит инъекцией (`Traversal`), а не импортом
GraphStore: чистота brief_quality есть условие того, что офлайн и онлайн
меряют одной линейкой — тот же приём, которым ground_truth.py принимает
GitRunner.
"""
from __future__ import annotations

from typing import Callable, Iterable

from reviewer.metrics.brief_quality.classify import is_core_production_path

Traversal = Callable[[list], set]
"""Обход графа: отсортированный список сид-символов → множество соседних node_id."""


def node_paths(node_ids: Iterable[str]) -> set:
    """Пути символов. node_id = "path#fqn"; идентификатор без '#' пропускается.

    Пропуск, а не разбор до первого слэша: строка без разделителя — это не
    символ, и достраивать из неё путь значило бы завышать ядро догадкой.
    """
    return {nid.split("#", 1)[0] for nid in node_ids if "#" in nid}


def symbol_name(node_id: str) -> str:
    """Простое имя символа: последний сегмент fqn у "path#a.b.c" — это "c"."""
    return node_id.split("#", 1)[1].split(".")[-1] if "#" in node_id else ""


def derive_context_core(
    seed_ids: Iterable[str],
    changed_core: Iterable[str],
    traverse: Traversal,
    allowed_names: set | None = None,
) -> set:
    """Контекстное ядро: core-пути соседей сид-символов минус изменённое ядро.

    Вычитание обязательно: файл, который задача и читала, и меняла, уже
    посчитан знаменателем core-recall, и в обоих знаменателях сразу он дал бы
    двойной вес.

    ``allowed_names`` — простые имена, вызванные (или унаследованные) на
    ИЗМЕНЁННЫХ строках диффа (PRI-262). Сосед выживает, только если его имя
    там названо. Без фильтра нетронутое тело задетой функции отдаёт в ядро
    своих callee: измеренный отказ PRI-261 — ``config_show()``, чья правка
    трогала help-текст, а ядро набиралось из существовавших вызовов
    ``CommittedLayerFetcher``/``resolve_policy_data`` (PRI-236, 0 из 5).

    ``None`` — фильтра нет, поведение тождественно поведению до PRI-262 (аддитивность).
    Пустое множество — НЕ то же самое: это высказывание «на изменённых строках
    вызовов нет», и ядро при нём пусто. Слить их значило бы вернуть весь обход
    ровно там, где сказать нечего.
    """
    seeds = sorted(seed_ids)
    if not seeds:
        return set()
    neighbours = traverse(seeds)
    if allowed_names is not None:
        neighbours = {n for n in neighbours if symbol_name(n) in allowed_names}
    paths = {p for p in node_paths(neighbours) if is_core_production_path(p)}
    return paths - set(changed_core)
