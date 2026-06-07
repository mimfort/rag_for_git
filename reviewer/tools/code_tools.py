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

    return [
        StructuredTool.from_function(search_code),
        StructuredTool.from_function(get_related_symbols),
    ]
