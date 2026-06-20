import json
from datetime import datetime

import reviewer.services.status as status_mod
from reviewer.services.status import build_status_report, OverlayStatus, render_status, render_status_json, RepoStatus, BranchStatus
from click.testing import CliRunner
import reviewer.entrypoints.cli as cli_mod


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
    assert rep.branches[0].updated_at == dt
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


def test_render_status_shapes_output():
    dt = datetime(2026, 6, 18, 14, 2)
    rep = RepoStatus(
        repo="a/x",
        branches=[
            BranchStatus("main", "base:main", "abc1234567", dt, 1843, 1207, 0),
            BranchStatus("dev", "base:dev", "def5678901", dt, 1850, None, 12),
            BranchStatus("old", "base:old", None, None, 0, None, None),
            BranchStatus("nogit", "base:nogit", "aaa1111222", dt, 10, 5, None),  # drift=None
        ],
        overlays=[OverlayStatus("pr:24", 18)])
    out = render_status(rep, "tree-sitter (fallback)")
    assert "Репозиторий: a/x" in out
    assert "✓ свежо" in out
    assert "отстаёт на 12 коммитов" in out
    assert "Neo4j недоступен" in out         # dev: graph_nodes=None
    assert "не проиндексирована" in out       # old: indexed_sha=None
    assert "pr:24   18 чанков" in out
    assert "abc1234" in out                    # короткий SHA (7 символов)
    assert "дрейф неизвестен" in out          # nogit: drift=None, sha присутствует


def test_status_command_smoke(monkeypatch):
    dt = datetime(2026, 6, 18, 14, 2)
    rep = RepoStatus(
        repo="a/x",
        branches=[BranchStatus("main", "base:main", "abc1234", dt, 5, 3, 0)],
        overlays=[])
    monkeypatch.setattr(cli_mod, "build_status_report", lambda *a, **k: rep)
    res = CliRunner().invoke(cli_mod.cli, ["status", ".", "--repo", "a/x"])
    assert res.exit_code == 0, res.output
    assert "Ветка main" in res.output
    assert "✓ свежо" in res.output


def test_render_status_json_shapes_payload():
    dt = datetime(2026, 6, 18, 14, 2)
    rep = RepoStatus(
        repo="a/x",
        branches=[
            BranchStatus("main", "base:main", "abc1234567def", dt, 1843, 1207, 0),
            BranchStatus("dev", "base:dev", "def5678901abc", dt, 1850, None, 12),
            BranchStatus("old", "base:old", None, None, 0, None, None),
            BranchStatus("nogit", "base:nogit", "aaa1111222bbb", dt, 10, 5, None),
        ],
        overlays=[OverlayStatus("pr:24", 18)])
    payload = json.loads(render_status_json(rep))
    assert payload["repo"] == "a/x"
    by = {b["branch"]: b for b in payload["branches"]}
    assert by["main"]["drift"] == 0
    assert by["main"]["indexed_sha"] == "abc1234567def"      # полный SHA, не усечён
    assert by["main"]["updated_at"] == "2026-06-18T14:02:00"  # ISO 8601
    assert by["dev"]["drift"] == 12
    assert by["dev"]["graph_nodes"] is None                   # Neo4j недоступен → null
    assert by["old"]["indexed_sha"] is None                   # не проиндексирована
    assert by["old"]["updated_at"] is None
    assert by["nogit"]["drift"] is None                       # дрейф неизвестен
    assert payload["overlays"] == [{"ref": "pr:24", "chunks": 18}]


def test_status_command_json(monkeypatch):
    dt = datetime(2026, 6, 18, 14, 2)
    rep = RepoStatus(
        repo="a/x",
        branches=[BranchStatus("main", "base:main", "abc1234567def", dt, 5, 3, 0)],
        overlays=[])
    monkeypatch.setattr(cli_mod, "build_status_report", lambda *a, **k: rep)
    res = CliRunner().invoke(cli_mod.cli, ["status", ".", "--repo", "a/x", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["repo"] == "a/x"
    assert payload["branches"][0]["drift"] == 0
    assert payload["branches"][0]["indexed_sha"] == "abc1234567def"
