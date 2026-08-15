"""Инкрементальный repo-aware патч графа кода (tree-sitter) для self-heal на prepare.

Симметрично self-heal векторов: переразбирает только изменённые файлы, сохраняя
ВХОДЯЩИЕ CALLS и IMPLEMENTS от неизменённых файлов (см. spec §5), и сносит только
ИСХОДЯЩИЕ CALLS/IMPLEMENTS патчируемых узлов — иначе смена базы класса в PR
оставляла бы в графе фантомное ребро навсегда. tree-sitter теперь эмитит и
class-level IMPLEMENTS (PRI-251).

Паритет с CALLS неполный. Локальный парсер видит только ``changed_sources`` —
если база наследования лежит в НЕизменённом файле (типовой случай: адаптер
на общей ``RestBoardBase``), после сноса исходящих IMPLEMENTS её нечем
пересобрать без файла базы. Поэтому перед пересборкой запрашивается дешёвый
снимок ``graph.all_node_ids`` — простое-имя → node_id уже существующих в графе
символов, подмешиваемый в ``build_graph_from_files`` ТОЛЬКО как дополнительный
источник глобального fallback-резолвинга баз (``extra_base_names``), не CALLS.
Восстанавливает ребро, если база уже была проиндексирована когда-либо
(обычный случай — правится один файл-наследник, база стабильна). Остаётся
дыра: если базы в графе нет вовсе (репозиторий не индексировался целиком, или
и база, и наследник — новые символы одного PR, а база при этом не входит в
``changed_sources`` этого патча), ребро не восстановится до ручного
`reviewer index`. Метод-уровневые override-ы (SCIP) self-heal не трогает —
полная точность по ним восстанавливается только ручным `reviewer index` с SCIP.
"""
from __future__ import annotations

from reviewer.graph.builder import build_graph_from_files


def _index_existing_by_simple_name(node_ids) -> dict[str, list[str]]:
    """node_id ('path#fqn') -> простое имя (хвост fqn после последней точки).

    Используется исключительно как ``extra_base_names`` для резолвинга баз
    наследования — см. модульный докстринг и предупреждение в
    :func:`reviewer.graph.inherit.extract_inheritance_edges`.
    """
    index: dict[str, list[str]] = {}
    for node_id in node_ids:
        _, _, fqn = node_id.partition("#")
        if not fqn:
            continue
        simple = fqn.split(".")[-1]
        index.setdefault(simple, []).append(node_id)
    return index


def patch_graph_incremental(graph, repo: str, *, branch: str = "",
                            changed_sources: dict[str, str],
                            removed_paths: list[str]) -> None:
    """Обновить граф (repo, branch) по изменённым/удалённым файлам.

    changed_sources: {path: источник целевой (base) версии} только .py изменённых/
        добавленных файлов — граф досинхронизируется к base-ветке, как и вектора.
    removed_paths: пути удалённых из PR .py-файлов.
    """
    # Удалённые файлы — снести их символы целиком.
    if removed_paths:
        gone = graph.symbols_for_paths(repo, removed_paths, branch=branch)
        graph.delete_symbols(repo, list(gone), branch=branch)

    if not changed_sources:
        return

    extra_base_names = _index_existing_by_simple_name(
        graph.all_node_ids(repo, branch=branch))
    nodes, edges = build_graph_from_files(changed_sources, extra_base_names)
    changed_paths = list(changed_sources)

    # Снести символы изменённых путей, исчезнувшие из новой версии.
    old = graph.symbols_for_paths(repo, changed_paths, branch=branch)
    stale = old - nodes
    graph.delete_symbols(repo, list(stale), branch=branch)

    # Снести только исходящие CALLS и IMPLEMENTS изменённой поверхности (входящие
    # сохраняем — их могут держать неизменённые файлы), затем переустановить узлы
    # и свежие исходящие рёбра.
    graph.delete_outgoing_calls(repo, list(nodes), branch=branch)
    graph.delete_outgoing_implements(repo, list(nodes), branch=branch)
    graph.upsert_nodes(repo, list(nodes), branch=branch)
    graph.upsert_edges(repo, edges, branch=branch)
