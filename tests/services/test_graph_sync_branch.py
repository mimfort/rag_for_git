# tests/services/test_graph_sync_branch.py
from reviewer.services.graph_sync import patch_graph_incremental


class FakeGraph:
    def __init__(self):
        self.upsert_branches = []

    def symbols_for_paths(self, repo, paths, *, branch=""):
        return set()

    def all_node_ids(self, repo, *, branch=""):
        return set()

    def delete_symbols(self, repo, ids, *, branch=""):
        pass

    def delete_outgoing_calls(self, repo, ids, *, branch=""):
        pass

    def delete_outgoing_implements(self, repo, ids, *, branch=""):
        pass

    def upsert_nodes(self, repo, ids, *, branch=""):
        self.upsert_branches.append(branch)

    def upsert_edges(self, repo, edges, *, branch=""):
        pass


def test_patch_graph_incremental_uses_branch():
    g = FakeGraph()
    patch_graph_incremental(
        g, "a/x", branch="master",
        changed_sources={"mod.py": "def f():\n    pass\n"}, removed_paths=[])
    assert g.upsert_branches and all(b == "master" for b in g.upsert_branches)
