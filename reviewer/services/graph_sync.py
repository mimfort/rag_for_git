"""Инкрементальный repo-aware патч графа кода (tree-sitter) для self-heal на prepare.

Симметрично self-heal векторов: переразбирает только изменённые файлы, сохраняя
ВХОДЯЩИЕ CALLS от неизменённых вызывающих (см. spec §5). Полная точность (IMPLEMENTS,
все рёбра) восстанавливается ручным `reviewer index` с SCIP.
"""
from __future__ import annotations

from reviewer.graph.builder import build_graph_from_files


def patch_graph_incremental(graph, repo: str, *, changed_sources: dict[str, str],
                            removed_paths: list[str]) -> None:
    """Обновить граф репозитория repo по изменённым/удалённым файлам.

    changed_sources: {path: head-источник} только .py изменённых/добавленных файлов.
    removed_paths: пути удалённых из PR .py-файлов.
    """
    # Удалённые файлы — снести их символы целиком.
    if removed_paths:
        gone = graph.symbols_for_paths(repo, removed_paths)
        graph.delete_symbols(repo, list(gone))

    if not changed_sources:
        return

    nodes, edges = build_graph_from_files(changed_sources)
    changed_paths = list(changed_sources)

    # Снести символы изменённых путей, исчезнувшие из новой версии.
    old = graph.symbols_for_paths(repo, changed_paths)
    stale = old - nodes
    graph.delete_symbols(repo, list(stale))

    # Снести только исходящие CALLS изменённой поверхности (входящие сохраняем),
    # затем переустановить узлы и свежие исходящие рёбра.
    graph.delete_outgoing_calls(repo, list(nodes))
    graph.upsert_nodes(repo, list(nodes))
    graph.upsert_edges(repo, edges)
