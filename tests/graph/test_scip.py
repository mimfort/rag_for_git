from reviewer.graph import scip
from reviewer.graph.scip_pb2 import Index, Document, Occurrence

DEF = 0x1

def _occ(symbol, line, role=0):
    o = Occurrence(symbol=symbol, symbol_roles=role)
    o.range.extend([line, 0, line, 20])   # [sl, sc, el, ec], 0-based
    return o

def test_calls_edges_resolved_to_path_fqn():
    idx = Index()
    doc = Document(relative_path="a.py")
    doc.occurrences.append(_occ("scip . pkg f().", 0, DEF))    # def f
    doc.occurrences.append(_occ("scip . pkg g().", 5, DEF))    # def g
    doc.occurrences.append(_occ("scip . pkg f().", 6))         # g() вызывает f()
    idx.documents.append(doc)

    intervals = {"a.py": [("f", 1, 4), ("g", 6, 8)]}
    def resolve(path, line1):
        best = None
        for fqn, s, e in intervals.get(path, []):
            if s <= line1 <= e and (best is None or (e - s) < (best[2] - best[1])):
                best = (fqn, s, e)
        return best[0] if best else None

    nodes, edges = scip.parse_scip(idx, resolve)
    assert "a.py#f" in nodes and "a.py#g" in nodes
    assert ("a.py#g", "CALLS", "a.py#f") in edges


def test_build_fqn_resolver_picks_narrowest():
    from reviewer.index.models import Chunk
    chunks = {"a.py": [
        Chunk("a.py","python","Outer","class",1,10,"x"),
        Chunk("a.py","python","Outer.inner","method",5,7,"y"),
    ]}
    r = scip.build_fqn_resolver(chunks)
    assert r("a.py", 6) == "Outer.inner"   # narrowest containing
    assert r("a.py", 2) == "Outer"
    assert r("a.py", 99) is None
