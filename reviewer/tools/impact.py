"""Анализ радиуса поражения (blast-radius): изменённые сигнатуры PR → вызывающие вне диффа."""
from __future__ import annotations

from dataclasses import dataclass, field

from reviewer.index.refs import base_ref
from reviewer.index.struct_diff import extract_signature  # ре-экспорт (обратная совместимость)


@dataclass
class CallerRef:
    """Ссылка на вызывающий символ: идентификатор, путь, строка и сниппет заголовка."""

    node_id: str
    path: str
    line: int
    snippet: str


@dataclass
class ImpactItem:
    """Символ с изменённой сигнатурой и список его внешних вызывающих."""

    node_id: str
    old_sig: str
    new_sig: str
    callers: list[CallerRef] = field(default_factory=list)


def compute_impact(graph, store, *, repo, branch, changed_node_ids,
                   changed_paths, overlay_ref) -> list[ImpactItem]:
    """Символы изменённых файлов с РЕАЛЬНО изменённой сигнатурой → их вызывающие вне диффа.

    Гейт: сигнатура символа в overlay (head) != сигнатура в base (до PR).
    Это отсекает чисто внутренние рефакторинги (тело поменяли, контракт нет).
    Вызывающие, чей файл входит в diff (changed_paths), отфильтрованы — их автор уже видит.
    Возвращает [] при отсутствии графа/стора/изменений.
    """
    if graph is None or store is None or not changed_node_ids:
        return []
    changed = set(changed_paths or [])
    new_by_id = {n.node_id: n for n in store.fetch_nodes_at(repo, changed_node_ids, overlay_ref)}
    old_by_id = {n.node_id: n for n in
                 store.fetch_nodes_at(repo, changed_node_ids, base_ref(branch))}

    items: list[ImpactItem] = []
    for nid in changed_node_ids:
        old, new = old_by_id.get(nid), new_by_id.get(nid)
        if old is None or new is None:
            continue  # добавленный/удалённый символ — нет пары для сравнения
        old_sig, new_sig = extract_signature(old.text), extract_signature(new.text)
        if not old_sig or not new_sig or old_sig == new_sig:
            continue  # ГЕЙТ: сигнатура не менялась
        caller_ids = sorted(graph.callers(repo, [nid], branch=branch))
        external = [cid for cid in caller_ids if cid.split("#", 1)[0] not in changed]
        if not external:
            continue
        nodes = {n.node_id: n for n in
                 store.fetch_nodes(repo, external, overlay_ref, changed_paths,
                                   base_ref=base_ref(branch))}
        callers: list[CallerRef] = []
        for cid in external:
            n = nodes.get(cid)
            if n is None:
                callers.append(CallerRef(cid, cid.split("#", 1)[0], 0, ""))
                continue
            snippet = extract_signature(n.text) or (n.text.splitlines()[0] if n.text else "")
            callers.append(CallerRef(cid, n.path, n.start_line, snippet))
        items.append(ImpactItem(nid, old_sig, new_sig, callers))
    return items


def format_impact(items: list[ImpactItem]) -> str:
    """Отчёт о радиусе поражения для MCP-вывода."""
    if not items:
        return "(изменений сигнатур с внешними вызывающими не найдено)"
    blocks = []
    for it in items:
        head = (f"{it.node_id}:\n"
                f"  было:  {it.old_sig}\n"
                f"  стало: {it.new_sig}\n"
                f"  устаревшие вызывающие (вне диффа):")
        rows = "\n".join(f"    - {c.path}:{c.line} | {c.snippet}" for c in it.callers)
        blocks.append(head + "\n" + rows)
    return "\n\n".join(blocks)
