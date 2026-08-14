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


def _resolver(intervals):
    """Резолвер fqn по интервалам строк: {path: [(fqn, start, end), ...]}."""
    def resolve(path, line1):
        best = None
        for fqn, s, e in intervals.get(path, []):
            if s <= line1 <= e and (best is None or (e - s) < (best[2] - best[1])):
                best = (fqn, s, e)
        return best[0] if best else None
    return resolve


def test_local_symbols_do_not_leak_across_documents():
    """`local 0` в разных файлах — разные символы; ребра между файлами быть не должно."""
    idx = Index()
    a = Document(relative_path="a.py")
    a.occurrences.append(_occ("local 0", 0, DEF))    # определение local 0 в a.py
    b = Document(relative_path="b.py")
    b.occurrences.append(_occ("local 0", 5, DEF))    # то же ИМЯ символа в b.py
    b.occurrences.append(_occ("local 0", 6))         # ссылка внутри b.py
    idx.documents.extend([b, a])   # b (опр.+ссылка) раньше a — на старом коде a перетирает символ b

    resolve = _resolver({"a.py": [("f", 1, 4)],
                         "b.py": [("g", 6, 6), ("h", 7, 9)]})
    _, edges = scip.parse_scip(idx, resolve)

    assert ("b.py#h", "CALLS", "a.py#f") not in edges     # кросс-файловая фикция
    assert not [e for e in edges if e[0].startswith("b.py") and e[2].startswith("a.py")]


def test_local_symbol_resolves_within_its_own_document():
    """Внутри одного документа local-символ по-прежнему даёт ребро."""
    idx = Index()
    doc = Document(relative_path="a.py")
    doc.occurrences.append(_occ("local 3", 0, DEF))   # вложенная функция в f
    doc.occurrences.append(_occ("local 3", 7))        # использована в g
    idx.documents.append(doc)

    resolve = _resolver({"a.py": [("f", 1, 4), ("g", 6, 9)]})
    _, edges = scip.parse_scip(idx, resolve)

    assert ("a.py#g", "CALLS", "a.py#f") in edges


def test_global_symbols_still_resolve_across_documents():
    """Регрессия: глобальные символы должны связывать файлы как и раньше."""
    idx = Index()
    a = Document(relative_path="a.py")
    a.occurrences.append(_occ("scip . pkg f().", 0, DEF))
    b = Document(relative_path="b.py")
    b.occurrences.append(_occ("scip . pkg f().", 6))
    idx.documents.extend([a, b])

    resolve = _resolver({"a.py": [("f", 1, 4)], "b.py": [("g", 6, 9)]})
    _, edges = scip.parse_scip(idx, resolve)

    assert ("b.py#g", "CALLS", "a.py#f") in edges
