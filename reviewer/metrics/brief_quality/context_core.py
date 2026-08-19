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


def derive_context_core(
    seed_ids: Iterable[str],
    changed_core: Iterable[str],
    traverse: Traversal,
) -> set:
    """Контекстное ядро: core-пути соседей сид-символов минус изменённое ядро.

    Вычитание обязательно: файл, который задача и читала, и меняла, уже
    посчитан знаменателем core-recall, и в обоих знаменателях сразу он дал бы
    двойной вес.
    """
    seeds = sorted(seed_ids)
    if not seeds:
        return set()
    neighbours = traverse(seeds)
    paths = {p for p in node_paths(neighbours) if is_core_production_path(p)}
    return paths - set(changed_core)
