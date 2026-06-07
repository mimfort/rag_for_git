from __future__ import annotations
import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from reviewer.index.chunker import chunk_python
from reviewer.graph.scip import build_fqn_resolver

_PY = Language(tspython.language())
_PARSER = Parser(_PY)

def _called_name(call_node) -> str | None:
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "identifier":
        return fn.text.decode("utf-8")
    if fn.type == "attribute":
        attr = fn.child_by_field_name("attribute")
        return attr.text.decode("utf-8") if attr is not None else None
    return None

def _iter_calls(node):
    if node.type == "call":
        yield node
    for ch in node.children:
        yield from _iter_calls(ch)

def build_graph_from_files(files: dict[str, str]):
    """Строит (nodes, edges) графа кода по tree-sitter.
    Узлы = все символы (path#fqn). Рёбра CALLS — по имени вызываемой функции/метода
    (резолвинг по простому имени; v1, неточный для перегрузок имён)."""
    chunks_by_path: dict[str, list] = {}
    name_to_nodes: dict[str, list[str]] = {}
    nodes: set[str] = set()
    for path, src in files.items():
        chunks = chunk_python(path, src.encode("utf-8"))
        chunks_by_path[path] = chunks
        for c in chunks:
            nodes.add(c.node_id)
            simple = c.symbol_fqn.split(".")[-1]
            name_to_nodes.setdefault(simple, []).append(c.node_id)
    resolve = build_fqn_resolver(chunks_by_path)
    edges: list[tuple[str, str, str]] = []
    for path, src in files.items():
        tree = _PARSER.parse(src.encode("utf-8"))
        for call in _iter_calls(tree.root_node):
            name = _called_name(call)
            if not name or name not in name_to_nodes:
                continue
            caller_fqn = resolve(path, call.start_point[0] + 1)
            if not caller_fqn:
                continue
            caller = f"{path}#{caller_fqn}"
            for callee in name_to_nodes[name]:
                if callee != caller:
                    edges.append((caller, "CALLS", callee))
    return nodes, list(dict.fromkeys(edges))
