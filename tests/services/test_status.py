from datetime import datetime

import reviewer.services.status as status_mod
from reviewer.services.status import build_status_report, OverlayStatus


class FakeStore:
    def __init__(self, meta, chunks, refs):
        self._meta, self._chunks, self._refs = meta, chunks, refs

    def get_index_meta_row(self, repo, ref):
        return self._meta.get(ref)

    def count_chunks(self, repo, ref):
        return self._chunks.get(ref, 0)

    def list_refs(self, repo):
        return list(self._refs)


class FakeGraph:
    def __init__(self, nodes, fail=False):
        self._nodes, self._fail = nodes, fail

    def count_nodes(self, repo, branch):
        if self._fail:
            raise RuntimeError("neo4j down")
        return self._nodes.get(branch, 0)


def test_build_status_report_fresh_and_behind(monkeypatch):
    dt = datetime(2026, 6, 18, 14, 2)
    store = FakeStore(
        meta={"base:main": ("abc1234", dt), "base:dev": ("def5678", dt)},
        chunks={"base:main": 1843, "base:dev": 1850, "pr:24": 18},
        refs=["base:main", "base:dev", "pr:24"])
    graph = FakeGraph(nodes={"main": 1207, "dev": 1190})
    drifts = {"main": 0, "dev": 12}
    monkeypatch.setattr(status_mod, "commits_behind",
                        lambda path, sha, ref: drifts.get(ref))
    rep = build_status_report(store, graph, "a/x", ["main", "dev"], "/tmp/repo")
    assert rep.branches[0].drift == 0 and rep.branches[0].graph_nodes == 1207
    assert rep.branches[0].indexed_sha == "abc1234"
    assert rep.branches[1].drift == 12
    assert rep.overlays == [OverlayStatus(ref="pr:24", chunks=18)]


def test_build_status_report_not_indexed_and_neo4j_down(monkeypatch):
    store = FakeStore(meta={}, chunks={"base:main": 0}, refs=["base:main"])
    graph = FakeGraph(nodes={}, fail=True)
    monkeypatch.setattr(status_mod, "commits_behind", lambda *a: None)
    rep = build_status_report(store, graph, "a/x", ["main"], "/tmp/repo")
    b = rep.branches[0]
    assert b.indexed_sha is None and b.drift is None and b.graph_nodes is None
    assert rep.overlays == []  # base:main исключён из overlay
