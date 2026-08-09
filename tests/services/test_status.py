import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest
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


class FakeSummaryStore:
    def __init__(self, counts, fail=False):
        self._counts, self._fail = counts, fail

    def count_summaries(self, repo, branch):
        if self._fail:
            raise RuntimeError("postgres down")
        return self._counts.get(branch, 0)


@pytest.fixture
def status_report() -> RepoStatus:
    """Возвращает отчёт с одной свежей основной веткой."""
    return RepoStatus(
        repo="a/x",
        branches=[
            BranchStatus(
                "main", "base:main", "abc1234567def", datetime(2026, 6, 18, 14, 2), 5, 3, 0
            )
        ],
        overlays=[],
    )


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
    monkeypatch.setattr(cli_mod, "ChunkStore", MagicMock())
    monkeypatch.setattr(cli_mod, "GraphStore", MagicMock())
    monkeypatch.setattr(cli_mod, "SummaryStore", MagicMock())
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
    monkeypatch.setattr(cli_mod, "ChunkStore", MagicMock())
    monkeypatch.setattr(cli_mod, "GraphStore", MagicMock())
    monkeypatch.setattr(cli_mod, "SummaryStore", MagicMock())
    res = CliRunner().invoke(cli_mod.cli, ["status", ".", "--repo", "a/x", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["repo"] == "a/x"
    assert payload["branches"][0]["drift"] == 0
    assert payload["branches"][0]["indexed_sha"] == "abc1234567def"


def test_build_status_report_counts_summaries(monkeypatch):
    dt = datetime(2026, 6, 18, 14, 2)
    store = FakeStore(
        meta={"base:main": ("abc1234", dt), "base:dev": ("def5678", dt)},
        chunks={"base:main": 1843, "base:dev": 1850},
        refs=["base:main", "base:dev"])
    graph = FakeGraph(nodes={"main": 1207, "dev": 1190})
    monkeypatch.setattr(status_mod, "commits_behind", lambda *a: 0)
    rep = build_status_report(store, graph, "a/x", ["main", "dev"], "/tmp/repo",
                              summary_store=FakeSummaryStore({"main": 26, "dev": 14}))
    assert rep.branches[0].summaries == 26
    assert rep.branches[1].summaries == 14


def test_build_status_report_without_summary_store(monkeypatch):
    dt = datetime(2026, 6, 18, 14, 2)
    store = FakeStore(meta={"base:main": ("abc1234", dt)},
                      chunks={"base:main": 5}, refs=["base:main"])
    graph = FakeGraph(nodes={"main": 3})
    monkeypatch.setattr(status_mod, "commits_behind", lambda *a: 0)
    rep = build_status_report(store, graph, "a/x", ["main"], "/tmp/repo")
    assert rep.branches[0].summaries is None      # обратная совместимость вызовов


def test_build_status_report_summary_store_down(monkeypatch):
    dt = datetime(2026, 6, 18, 14, 2)
    store = FakeStore(meta={"base:main": ("abc1234", dt)},
                      chunks={"base:main": 5}, refs=["base:main"])
    graph = FakeGraph(nodes={"main": 3})
    monkeypatch.setattr(status_mod, "commits_behind", lambda *a: 0)
    rep = build_status_report(store, graph, "a/x", ["main"], "/tmp/repo",
                              summary_store=FakeSummaryStore({}, fail=True))
    assert rep.branches[0].summaries is None      # fail-soft, как у graph_nodes


def test_render_status_json_includes_summaries():
    dt = datetime(2026, 6, 18, 14, 2)
    rep = RepoStatus(
        repo="a/x",
        branches=[
            BranchStatus("main", "base:main", "abc1234567def", dt, 1843, 1207, 0, 26),
            BranchStatus("dev", "base:dev", "def5678901abc", dt, 1850, None, 12, None),
        ],
        overlays=[])
    by = {b["branch"]: b for b in json.loads(render_status_json(rep))["branches"]}
    assert by["main"]["summaries"] == 26
    assert by["dev"]["summaries"] is None       # стор недоступен → null


def test_render_status_shows_summaries():
    dt = datetime(2026, 6, 18, 14, 2)
    rep = RepoStatus(
        repo="a/x",
        branches=[
            BranchStatus("main", "base:main", "abc1234567", dt, 1843, 1207, 0, 26),
            BranchStatus("dev", "base:dev", "def5678901", dt, 1850, None, 12, None),
        ],
        overlays=[])
    out = render_status(rep, "tree-sitter (fallback)")
    assert "Сводки: 26" in out
    assert "Сводки: —" in out                   # неизвестно


def test_status_command_passes_and_closes_summary_store(monkeypatch, status_report):
    captured = {}

    def fake_build(*a, **k):
        captured.update(k)
        return status_report

    summary_store_cls = MagicMock()
    monkeypatch.setattr(cli_mod, "build_status_report", fake_build)
    monkeypatch.setattr(cli_mod, "ChunkStore", MagicMock())
    monkeypatch.setattr(cli_mod, "GraphStore", MagicMock())
    monkeypatch.setattr(cli_mod, "SummaryStore", summary_store_cls)
    res = CliRunner().invoke(cli_mod.cli, ["status", ".", "--repo", "a/x", "--json"])
    assert res.exit_code == 0, res.output
    assert captured["summary_store"] is summary_store_cls.return_value
    summary_store_cls.return_value.close.assert_called_once()
