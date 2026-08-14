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
        self.deleted_implements = []
        self.upserted_nodes = []
        self.upserted_edges = []

    def symbols_for_paths(self, repo, paths, *, branch=""):
        prefixes = [p + "#" for p in paths]
        return {s for s in self.symbols.get(repo, set())
                if any(s.startswith(p) for p in prefixes)}

    def all_node_ids(self, repo, *, branch=""):
        return set(self.symbols.get(repo, set()))

    def delete_symbols(self, repo, ids, *, branch=""):
        self.deleted.append((repo, set(ids)))
        self.symbols.get(repo, set()).difference_update(ids)

    def delete_outgoing_calls(self, repo, ids, *, branch=""):
        self.deleted_calls.append((repo, set(ids)))

    def delete_outgoing_implements(self, repo, ids, *, branch=""):
        self.deleted_implements.append((repo, set(ids)))

    def upsert_nodes(self, repo, ids, *, branch=""):
        self.upserted_nodes.append((repo, set(ids)))
        self.symbols.setdefault(repo, set()).update(ids)

    def upsert_edges(self, repo, edges, *, branch=""):
        self.upserted_edges.append((repo, list(edges)))


def test_patch_removes_stale_and_refreshes_changed():
    g = FakeGraph({"a/x": {"a.py#foo", "a.py#gone"}})
    sources = {"a.py": "def foo():\n    bar()\n\ndef bar():\n    return 1\n"}
    patch_graph_incremental(g, "a/x", changed_sources=sources, removed_paths=[])
    assert ("a/x", {"a.py#gone"}) in g.deleted
    assert any(repo == "a/x" and "a.py#bar" in ids for repo, ids in g.upserted_nodes)
    assert g.deleted_calls and g.deleted_calls[0][0] == "a/x"


def test_patch_deletes_outgoing_implements_of_changed_surface():
    """Important 3: смена базы класса в PR не оставляет фантомное IMPLEMENTS.

    До фикса patch_graph_incremental сносил только исходящие CALLS — смена
    ``class X(A)`` -> ``class X(B)`` оставляла в графе ``X IMPLEMENTS A``
    навсегда, до ручного `reviewer index`.
    """
    g = FakeGraph({"a/x": {"a.py#X"}})
    sources = {"a.py": "class X(B):\n    pass\n\nclass B:\n    pass\n"}
    patch_graph_incremental(g, "a/x", changed_sources=sources, removed_paths=[])
    assert g.deleted_implements and g.deleted_implements[0][0] == "a/x"
    deleted_ids = g.deleted_implements[0][1]
    assert "a.py#X" in deleted_ids


def test_patch_restores_implements_when_base_lives_in_unchanged_file():
    """Important N1 (ре-ревью): инкрементальный парс одного файла-наследника
    не может локально резолвить базу из неизменённого файла — без доп.
    источника ребро терялось бы после сноса исходящих IMPLEMENTS self-heal'ом
    (Important 3) и не восстанавливалось бы до ручного `reviewer index`.
    Если база уже проиндексирована (есть в графе), self-heal обязан
    восстановить ребро, подмешав существующие символы графа как
    дополнительный (последний по приоритету) источник резолвинга.
    """
    g = FakeGraph({"a/x": {"a.py#Child", "base.py#Base"}})
    sources = {"a.py": "class Child(Base):\n    pass\n"}
    patch_graph_incremental(g, "a/x", changed_sources=sources, removed_paths=[])
    edges = [e for _, batch in g.upserted_edges for e in batch]
    assert ("a.py#Child", "IMPLEMENTS", "base.py#Base") in edges


def test_patch_graph_symbol_fallback_does_not_leak_into_calls():
    """Доп. источник резолвинга (символы графа) используется ТОЛЬКО для баз
    наследования — резолвинг CALLS не должен видеть ничего, кроме changed_sources,
    иначе это молчаливо расширило бы глобальный fallback вызовов на весь граф."""
    g = FakeGraph({"a/x": {"a.py#caller", "other.py#helper"}})
    sources = {"a.py": "def caller():\n    helper()\n"}
    patch_graph_incremental(g, "a/x", changed_sources=sources, removed_paths=[])
    edges = [e for _, batch in g.upserted_edges for e in batch]
    # helper() резолвится только внутри changed_sources (a.py) — там его нет,
    # значит CALLS-ребро на other.py#helper НЕ создаётся, даже если такой
    # символ уже существует в графе под тем же простым именем.
    assert not any(rel == "CALLS" and dst == "other.py#helper" for _, rel, dst in edges)


def test_patch_removed_files_delete_symbols():
    g = FakeGraph({"a/x": {"old.py#x"}})
    patch_graph_incremental(g, "a/x", changed_sources={}, removed_paths=["old.py"])
    assert ("a/x", {"old.py#x"}) in g.deleted
