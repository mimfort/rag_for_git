from __future__ import annotations
from collections.abc import Callable

from reviewer.index.chunker import chunk_python

DEFINITION = 0x1
FqnResolver = Callable[[str, int], str | None]   # (path, line_1based) -> fqn|None

def _start_line_1based(occ) -> int:
    return occ.range[0] + 1   # SCIP 0-based -> 1-based

def _is_local(symbol: str) -> bool:
    """True для файл-скоупного символа SCIP (`local <N>`).

    Такой идентификатор уникален ТОЛЬКО внутри своего документа: `local 0` в
    двух разных файлах — один и тот же ключ. Глобальная карта символов на них
    давала кросс-файловую фикцию (половина CALLS-рёбер, PRI-252).
    """
    return symbol.startswith("local ")


def parse_scip(index, resolve: FqnResolver):
    """index: scip_pb2.Index. Возвращает (nodes:set[str], edges:list[(src,rel,dst)])."""
    nodes: set[str] = set()
    edges: list[tuple[str, str, str]] = []
    symbol_to_node: dict[str, str] = {}                  # глобальные символы
    local_to_node: dict[str, dict[str, str]] = {}        # {документ: {символ: node_id}}

    def lookup(path: str, symbol: str) -> str | None:
        if _is_local(symbol):
            return local_to_node.get(path, {}).get(symbol)
        return symbol_to_node.get(symbol)

    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & DEFINITION:
                fqn = resolve(doc.relative_path, _start_line_1based(occ))
                if fqn:
                    nid = f"{doc.relative_path}#{fqn}"
                    if _is_local(occ.symbol):
                        local_to_node.setdefault(doc.relative_path, {})[occ.symbol] = nid
                    else:
                        symbol_to_node[occ.symbol] = nid
                    nodes.add(nid)

    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & DEFINITION:
                continue
            callee = lookup(doc.relative_path, occ.symbol)
            if callee is None:
                continue
            caller_fqn = resolve(doc.relative_path, _start_line_1based(occ))
            if not caller_fqn:
                continue
            caller = f"{doc.relative_path}#{caller_fqn}"
            if caller != callee:
                nodes.add(caller)
                edges.append((caller, "CALLS", callee))

    for doc in index.documents:
        for si in doc.symbols:
            src = lookup(doc.relative_path, si.symbol)
            if src is None:
                continue
            for rel in si.relationships:
                if rel.is_implementation:
                    dst = lookup(doc.relative_path, rel.symbol)
                    if dst:
                        edges.append((src, "IMPLEMENTS", dst))

    return nodes, list(dict.fromkeys(edges))

def build_fqn_resolver(chunks_by_path: dict[str, list]) -> FqnResolver:
    def resolve(path: str, line1: int) -> str | None:
        best = None
        for c in chunks_by_path.get(path, []):
            if c.start_line <= line1 <= c.end_line:
                span = c.end_line - c.start_line
                if best is None or span < best[1]:
                    best = (c.symbol_fqn, span)
        return best[0] if best else None
    return resolve

def chunks_by_path_for(repo_files: dict[str, str]) -> dict[str, list]:
    return {p: chunk_python(p, src.encode("utf-8")) for p, src in repo_files.items()}

def run_scip_python(repo: str, project_name: str = "repo") -> bytes:
    """Запустить индексер; вернуть содержимое index.scip. Требует npm @sourcegraph/scip-python и активный venv."""
    import pathlib
    import subprocess
    subprocess.run(["scip-python", "index", ".", f"--project-name={project_name}"],
                   cwd=repo, check=True, capture_output=True)
    return pathlib.Path(repo, "index.scip").read_bytes()
