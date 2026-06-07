from __future__ import annotations
from dataclasses import dataclass, field
from langchain_core.tools import StructuredTool

@dataclass
class ToolContext:
    retriever: object
    graph: object
    overlay_ref: str
    changed_paths: list[str]
    changed_node_ids: list[str] = field(default_factory=list)
    read_file_fn: object = None            # Callable[[str], str | None] — head-версия файла
    patches: dict = field(default_factory=dict)
    store: object = None                   # индекс-стор для get_definition

def make_tools(ctx: ToolContext) -> list[StructuredTool]:
    def search_code(query: str) -> str:
        """Семантико-лексический поиск релевантного кода по всему репозиторию."""
        pack = ctx.retriever.retrieve(
            query=query, changed_node_ids=ctx.changed_node_ids,
            overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths, top_k=8)
        return pack.as_context() or "(ничего не найдено)"

    def get_related_symbols(node_id: str) -> str:
        """Связанные символы (вызовы/реализации/тесты) для node_id вида 'path#fqn'."""
        related = ctx.graph.expand([node_id], hops=2)
        return "\n".join(sorted(related)) or "(нет связей)"

    def read_file(path: str, start: int = 1, end: int = 400) -> str:
        """Точный исходник файла на head-ревизии PR, строки [start..end] с номерами (N|код).
        Окно ограничено 400 строками."""
        if ctx.read_file_fn is None:
            return "(чтение файлов недоступно)"
        src = ctx.read_file_fn(path)
        if src is None:
            return f"(файл не найден: {path})"
        lines = src.splitlines()
        if not lines:
            return "(файл пуст)"
        s = max(1, start)
        e = min(len(lines), end)
        if e - s + 1 > 400:
            e = s + 399
        if s > len(lines):
            return f"(нет строки {s}; в файле {len(lines)} строк)"
        body = "\n".join(f"{i}|{lines[i - 1]}" for i in range(s, e + 1))
        if e < len(lines):
            body += "\n(…усечено)"
        return body

    def get_definition(symbol: str) -> str:
        """Где определён символ + его исходный код. Резолв имени через граф, код — через индекс.
        Фолбэк на семантический поиск, если граф/стор пусты."""
        ids: list[str] = []
        if ctx.graph is not None and hasattr(ctx.graph, "find_symbol"):
            ids = ctx.graph.find_symbol(symbol)
        if ids and ctx.store is not None:
            nodes = ctx.store.fetch_nodes(ids[:3], ctx.overlay_ref, ctx.changed_paths)
            if nodes:
                return "\n\n".join(
                    f"// {n.node_id} ({n.path}:{n.start_line}-{n.end_line})\n{n.text}"
                    for n in nodes)
        pack = ctx.retriever.retrieve(
            query=symbol, changed_node_ids=ctx.changed_node_ids,
            overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths, top_k=3)
        return pack.as_context() or "(определение не найдено)"

    def find_callers(node_id: str) -> str:
        """Кто вызывает символ node_id ('path#fqn') — направленный CALLS (impact-анализ)."""
        if ctx.graph is None or not hasattr(ctx.graph, "callers"):
            return "(граф недоступен)"
        found = ctx.graph.callers([node_id])
        return "\n".join(sorted(found)) or "(вызовов не найдено)"

    def get_changed_file_diff(path: str) -> str:
        """Дифф другого изменённого файла этого PR."""
        patch = (ctx.patches or {}).get(path)
        return patch or "(файл не входит в изменения PR)"

    return [
        StructuredTool.from_function(search_code),
        StructuredTool.from_function(get_related_symbols),
        StructuredTool.from_function(read_file),
        StructuredTool.from_function(get_definition),
        StructuredTool.from_function(find_callers),
        StructuredTool.from_function(get_changed_file_diff),
    ]
