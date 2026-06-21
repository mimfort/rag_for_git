from __future__ import annotations

import logging

from reviewer.index.refs import base_ref

log = logging.getLogger(__name__)

_CAP = 25


def _rel_label(nb: dict) -> str:
    """Метка связи: входящие вызовы → 'CALLS'; соседи → 'CALLS→IMPLEMENTS, d2'."""
    if "rels" in nb:
        seen: list[str] = []
        for r in nb.get("rels") or []:
            if r not in seen:
                seen.append(r)
        types = "→".join(seen) if seen else "?"
        return f"{types}, d{nb.get('dist', '?')}"
    return nb.get("rel", "?")


def _first_line(text: str) -> str:
    """Первая непустая строка текста."""
    for line in (text or "").splitlines():
        if line.strip():
            return line
    return ""


def format_neighbors(neighbors: list[dict], *, store, repo: str, branch: str,
                     overlay_ref, changed_paths: list[str], empty_msg: str) -> str:
    """Рендер соседей графа: '// id (path:line) [REL]\\n<сниппет>'.

    Сниппет — строка определения символа из Postgres (store.fetch_nodes).
    store=None → деградация к 'id [REL]'; промах индекса → '… (вне индекса)';
    кап _CAP элементов, хвост '(…ещё N, усечено)'. Порядок сохраняется как есть.
    """
    if not neighbors:
        return empty_msg
    total = len(neighbors)
    items = neighbors[:_CAP]
    nodes: dict[str, object] = {}
    if store is not None:
        try:
            fetched = store.fetch_nodes(repo, [n["id"] for n in items],
                                        overlay_ref, changed_paths,
                                        base_ref=base_ref(branch))
            nodes = {n.node_id: n for n in fetched}
        except Exception as e:
            # fail-open: при сбое Postgres рендерим '(вне индекса)', но логируем дегрейд
            log.warning("format_neighbors: store.fetch_nodes упал (%s) — дегрейд к '(вне индекса)'", e)
            nodes = {}
    lines: list[str] = []
    for nb in items:
        rel = _rel_label(nb)
        meta = nodes.get(nb["id"])
        if meta is not None:
            snippet = _first_line(meta.text)
            header = f"// {nb['id']} ({meta.path}:{meta.start_line}) [{rel}]"
            lines.append(f"{header}\n{snippet}" if snippet else header)
        elif store is None:
            lines.append(f"// {nb['id']} [{rel}]")
        else:
            lines.append(f"// {nb['id']} [{rel}] (вне индекса)")
    if total > _CAP:
        lines.append(f"(…ещё {total - _CAP}, усечено)")
    return "\n".join(lines)
