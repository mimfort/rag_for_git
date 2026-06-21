"""Обогащение находок центральностью символа в графе кода (PRI-129).

Центральность символа = число входящих CALLS (сколько мест зависит от него).
Находка в высокоцентральном «хабе» при равной severity должна идти выше при
сортировке и реже отсекаться cap'ом — поэтому каждой находке проставляется
``centrality``, используемая как tie-breaker в ``assemble_review``.
"""
from __future__ import annotations


def annotate_centrality(findings, graph, store, *, repo, branch,
                        changed_node_ids, overlay_ref) -> None:
    """Проставить ``f.centrality`` каждой находке (мутация на месте).

    Маппинг: ``(file, line)`` находки → охватывающий символ изменённого файла
    (самый узкий диапазон при вложенности) → число входящих CALLS символа.
    Fail-soft: нет графа/стора/изменённых символов/совпадений → ``centrality``
    остаётся дефолтным 0.0, порядок сортировки не меняется.
    """
    if graph is None or store is None or not changed_node_ids or not findings:
        return
    # Символы изменённых файлов с диапазонами строк (head-версия из overlay).
    by_file: dict[str, list[tuple[int, int, str]]] = {}
    for n in store.fetch_nodes_at(repo, changed_node_ids, overlay_ref):
        by_file.setdefault(n.path, []).append((n.start_line, n.end_line, n.node_id))

    # Для каждой находки — охватывающий символ (минимальная ширина диапазона).
    finding_nid: list[tuple[object, str | None]] = []
    to_query: set[str] = set()
    for f in findings:
        nid = None
        if f.line is not None:
            spans = [(end - start, node_id)
                     for start, end, node_id in by_file.get(f.file, [])
                     if start <= f.line <= end]
            if spans:
                nid = min(spans)[1]   # минимальный (end-start) = самый узкий диапазон
                to_query.add(nid)
        finding_nid.append((f, nid))

    if not to_query:
        return
    deg = graph.in_degree(repo, list(to_query), branch=branch)
    for f, nid in finding_nid:
        if nid is not None:
            f.centrality = float(deg.get(nid, 0))
