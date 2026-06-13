"""Unit-тесты инкрементального патча графа (graph_sync).

Используют фейковый граф без обращения к БД.
"""
from __future__ import annotations

from reviewer.services.graph_sync import patch_graph_incremental


class FakeGraph:
    def __init__(self, existing):
        self.symbols = {k: set(v) for k, v in existing.items()}   # repo -> set(node_id)
        self.deleted = []
        self.deleted_calls = []
        self.upserted_nodes = []
        self.upserted_edges = []

    def symbols_for_paths(self, repo, paths):
        prefixes = [p + "#" for p in paths]
        return {s for s in self.symbols.get(repo, set())
                if any(s.startswith(p) for p in prefixes)}

    def delete_symbols(self, repo, ids):
        self.deleted.append((repo, set(ids)))
        self.symbols.get(repo, set()).difference_update(ids)

    def delete_outgoing_calls(self, repo, ids):
        self.deleted_calls.append((repo, set(ids)))

    def upsert_nodes(self, repo, ids):
        self.upserted_nodes.append((repo, set(ids)))
        self.symbols.setdefault(repo, set()).update(ids)

    def upsert_edges(self, repo, edges):
        self.upserted_edges.append((repo, list(edges)))


def test_patch_removes_stale_and_refreshes_changed():
    g = FakeGraph({"a/x": {"a.py#foo", "a.py#gone"}})
    sources = {"a.py": "def foo():\n    bar()\n\ndef bar():\n    return 1\n"}
    patch_graph_incremental(g, "a/x", changed_sources=sources, removed_paths=[])
    assert ("a/x", {"a.py#gone"}) in g.deleted
    assert any(repo == "a/x" and "a.py#bar" in ids for repo, ids in g.upserted_nodes)
    assert g.deleted_calls and g.deleted_calls[0][0] == "a/x"


def test_patch_removed_files_delete_symbols():
    g = FakeGraph({"a/x": {"old.py#x"}})
    patch_graph_incremental(g, "a/x", changed_sources={}, removed_paths=["old.py"])
    assert ("a/x", {"old.py#x"}) in g.deleted
