"""reviewer index: разбивка рёбер по типам и предупреждение о просадке (PRI-252)."""
from __future__ import annotations

from unittest.mock import MagicMock

from click.testing import CliRunner

import reviewer.entrypoints.cli as cli_mod
from reviewer.services.repo_id import RepoResolution

EDGES = [
    ("reviewer/a.py#f", "CALLS", "reviewer/b.py#g"),
    ("reviewer/a.py#f", "CALLS", "reviewer/b.py#h"),
    ("reviewer/a.py#C", "IMPLEMENTS", "reviewer/b.py#Base"),
]


def _wire(monkeypatch, components, edges=EDGES):
    monkeypatch.setenv("REVIEW_BRANCHES", "main,dev")
    monkeypatch.setattr(cli_mod, "build_components", lambda s: components)
    monkeypatch.setattr(cli_mod, "_resolve_repo",
                        lambda *a: RepoResolution("o/r", "cli"))
    monkeypatch.setattr(cli_mod, "list_python_files",
                        lambda repo, ref: ["reviewer/a.py"])
    monkeypatch.setattr(cli_mod, "rev_parse", lambda repo, ref: "deadbeef")
    monkeypatch.setattr(cli_mod, "file_at_ref",
                        lambda repo, path, ref: None if path == ".review.yml"
                        else "def f():\n    pass\n")
    monkeypatch.setattr(cli_mod, "update_base", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "build_code_graph",
                        lambda *a, **k: ({"reviewer/a.py#f"}, edges, "scip"))


def test_index_prints_edge_breakdown(monkeypatch):
    components = MagicMock()
    components.store.get_graph_edge_counts.return_value = None
    _wire(monkeypatch, components)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "main"])

    assert result.exit_code == 0, result.output
    assert "рёбер 3 (CALLS 2, IMPLEMENTS 1)" in result.output
    components.store.set_graph_edge_counts.assert_called_once_with(
        "o/r", "base:main", {"CALLS": 2, "IMPLEMENTS": 1})


def test_index_warns_on_edge_regression(monkeypatch):
    components = MagicMock()
    components.store.get_graph_edge_counts.return_value = {"CALLS": 100, "IMPLEMENTS": 1}
    _wire(monkeypatch, components)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "main"])

    assert result.exit_code == 0, result.output          # предупреждение не роняет команду
    assert "Просадка полноты графа" in result.output
    assert "CALLS 100 → 2 (−98%)" in result.output
    assert "IMPLEMENTS" not in result.output.split("Просадка полноты графа")[1]


def test_index_silent_without_previous_measurement(monkeypatch):
    components = MagicMock()
    components.store.get_graph_edge_counts.return_value = None
    _wire(monkeypatch, components)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "main"])

    assert result.exit_code == 0, result.output
    assert "Просадка" not in result.output


def test_index_survives_failed_counter_write(monkeypatch):
    """Счётчики вторичны: их запись не вправе ронять уже построенный индекс."""
    components = MagicMock()
    components.store.get_graph_edge_counts.return_value = None
    components.store.set_graph_edge_counts.side_effect = RuntimeError("нет колонки")
    _wire(monkeypatch, components)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "main"])

    assert result.exit_code == 0, result.output
    assert "рёбер 3 (CALLS 2, IMPLEMENTS 1)" in result.output


def test_index_survives_failed_counter_read(monkeypatch):
    """Недоступный предыдущий замер — не повод падать: печатаем разбивку без сравнения."""
    components = MagicMock()
    components.store.get_graph_edge_counts.side_effect = RuntimeError("нет колонки")
    _wire(monkeypatch, components)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "main"])

    assert result.exit_code == 0, result.output
    assert "рёбер 3 (CALLS 2, IMPLEMENTS 1)" in result.output
    assert "Просадка" not in result.output
