"""Тесты модуля reviewer.graph.backend: graph_from_scip_bytes и выбор бэкенда."""
import pytest

from reviewer.graph.scip_pb2 import Document, Index, Occurrence
from reviewer.index.models import Chunk

DEF = 0x1


def _occ(symbol, line, role=0):
    """Вспомогательный хелпер — создать Occurrence с нужными полями."""
    o = Occurrence(symbol=symbol, symbol_roles=role)
    o.range.extend([line, 0, line, 20])   # [sl, sc, el, ec], 0-based
    return o


# ---------------------------------------------------------------------------
# graph_from_scip_bytes
# ---------------------------------------------------------------------------

def test_graph_from_scip_bytes_edges_and_leaf_nodes():
    """Проверяем, что graph_from_scip_bytes:
    - возвращает ребро CALLS между двумя функциями;
    - включает все node_id из чанков (даже листовые, не затронутые SCIP).
    """
    from reviewer.graph.backend import graph_from_scip_bytes

    # Собираем минимальный Index с двумя определениями и одним вызовом
    idx = Index()
    doc = Document(relative_path="a.py")
    doc.occurrences.append(_occ("scip . pkg f().", 0, DEF))   # def f — строка 1 (0-based 0)
    doc.occurrences.append(_occ("scip . pkg g().", 5, DEF))   # def g — строка 6 (0-based 5)
    doc.occurrences.append(_occ("scip . pkg f().", 6))         # вызов f внутри g — строка 7 (0-based 6)
    idx.documents.append(doc)

    data = idx.SerializeToString()

    # chunks_by_path с реальными Chunk-объектами (как в test_scip.py)
    chunks_by_path = {
        "a.py": [
            Chunk("a.py", "python", "f", "function", 1, 4, "def f():\n    pass"),
            Chunk("a.py", "python", "g", "method",   6, 8, "def g():\n    f()"),
            # листовой символ — SCIP его не трогает, но он должен попасть в nodes
            Chunk("a.py", "python", "helper", "function", 10, 12, "def helper(): ..."),
        ]
    }

    nodes, edges = graph_from_scip_bytes(data, chunks_by_path)

    # Основное ребро CALLS
    assert ("a.py#g", "CALLS", "a.py#f") in edges, "Ожидалось ребро CALLS g->f"

    # Все node_id из чанков должны быть в nodes (включая листовой helper)
    for chunk_list in chunks_by_path.values():
        for c in chunk_list:
            assert c.node_id in nodes, f"node_id {c.node_id!r} не найден в nodes"


# ---------------------------------------------------------------------------
# build_code_graph — выбор бэкенда через monkeypatch
# ---------------------------------------------------------------------------

def test_build_code_graph_auto_falls_back_to_treesitter_when_scip_unavailable(monkeypatch):
    """auto + scip_available=False → tree-sitter, backend_used='treesitter'."""
    from reviewer.graph import backend as _backend

    monkeypatch.setattr(_backend, "scip_available", lambda: False)
    # Мокаем build_graph_from_files чтобы не зависеть от файлов
    monkeypatch.setattr(_backend, "build_graph_from_files", lambda src: ({"n1"}, [("n1", "CALLS", "n2")]))

    nodes, edges, used = _backend.build_code_graph(".", "HEAD", [], {}, backend="auto")

    assert used == "treesitter"
    assert "n1" in nodes


def test_build_code_graph_auto_uses_scip_when_available(monkeypatch):
    """auto + scip_available=True + build_with_scip успешен → backend_used='scip'."""
    from reviewer.graph import backend as _backend

    monkeypatch.setattr(_backend, "scip_available", lambda: True)
    monkeypatch.setattr(
        _backend, "build_with_scip",
        lambda repo, ref, src: ({"scip_node"}, [("scip_node", "CALLS", "other")])
    )

    nodes, edges, used = _backend.build_code_graph(".", "HEAD", [], {}, backend="auto")

    assert used == "scip"
    assert "scip_node" in nodes


def test_build_code_graph_auto_falls_back_on_scip_error(monkeypatch):
    """auto + build_with_scip бросает исключение → откат на tree-sitter, backend='treesitter'."""
    from reviewer.graph import backend as _backend

    monkeypatch.setattr(_backend, "scip_available", lambda: True)
    monkeypatch.setattr(_backend, "build_with_scip", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("scip упал")))
    monkeypatch.setattr(_backend, "build_graph_from_files", lambda src: ({"ts_node"}, []))

    nodes, edges, used = _backend.build_code_graph(".", "HEAD", [], {}, backend="auto")

    assert used == "treesitter"
    assert "ts_node" in nodes


def test_build_code_graph_explicit_scip_raises_on_error(monkeypatch):
    """backend='scip' + исключение → пробрасывается (нет молчаливого отката)."""
    from reviewer.graph import backend as _backend

    monkeypatch.setattr(_backend, "scip_available", lambda: True)
    monkeypatch.setattr(
        _backend, "build_with_scip",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("scip сломан"))
    )

    with pytest.raises(RuntimeError, match="scip сломан"):
        _backend.build_code_graph(".", "HEAD", [], {}, backend="scip")
