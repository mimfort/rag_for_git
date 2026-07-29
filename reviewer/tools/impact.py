"""Анализ радиуса поражения: изменённые сигнатуры PR → evidence вызывающих."""
from __future__ import annotations

from dataclasses import dataclass, field

from reviewer.index.refs import base_ref
from reviewer.index.struct_diff import extract_signature  # ре-экспорт (обратная совместимость)


@dataclass
class CallerRef:
    """Прочитанный из индекса caller-кандидат для проверки."""

    node_id: str
    path: str
    line: int
    snippet: str
    changed_file: bool = False


@dataclass
class ImpactItem:
    """Символ с изменённой сигнатурой и evidence его callers."""

    node_id: str
    old_sig: str
    new_sig: str
    callers: list[CallerRef] = field(default_factory=list)
    unresolved_caller_ids: list[str] = field(default_factory=list)


def compute_impact(graph, store, *, repo, branch, changed_node_ids,
                   changed_paths, overlay_ref) -> list[ImpactItem]:
    """Символы изменённых файлов с РЕАЛЬНО изменённой сигнатурой → их callers.

    Гейт: сигнатура символа в overlay (head) != сигнатура в base (до PR).
    Это отсекает чисто внутренние рефакторинги (тело поменяли, контракт нет).
    Callers из изменённых файлов также возвращаются и помечаются для честного scope.
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
        if not caller_ids:
            continue
        nodes = {
            node.node_id: node
            for node in store.fetch_nodes(
                repo,
                caller_ids,
                overlay_ref,
                changed_paths,
                base_ref=base_ref(branch),
            )
        }
        callers: list[CallerRef] = []
        unresolved: list[str] = []
        for cid in caller_ids:
            node = nodes.get(cid)
            if node is None:
                unresolved.append(cid)
                continue
            snippet = extract_signature(node.text) or (
                node.text.splitlines()[0] if node.text else ""
            )
            callers.append(
                CallerRef(
                    cid,
                    node.path,
                    node.start_line,
                    snippet,
                    changed_file=node.path in changed,
                )
            )
        items.append(ImpactItem(nid, old_sig, new_sig, callers, unresolved))
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
                "  кандидаты callers для проверки:")
        rows = [
            f"    - [{'в PR' if caller.changed_file else 'вне PR'}] "
            f"{caller.path}:{caller.line} | {caller.snippet}"
            for caller in it.callers
        ]
        if it.unresolved_caller_ids:
            rows.append(
                "    - [пробел покрытия] метаданные callers не найдены в индексе "
                f"({len(it.unresolved_caller_ids)}): "
                + ", ".join(it.unresolved_caller_ids)
            )
        blocks.append(head + "\n" + "\n".join(rows))
    return "\n\n".join(blocks)
