"""Тест: reviewer index фильтрует файлы по paths.ignore из .review.yml ветки."""
from __future__ import annotations

from unittest.mock import MagicMock

from click.testing import CliRunner

import reviewer.entrypoints.cli as cli_mod


def test_index_filters_ignored_files(monkeypatch):
    captured = {}

    def fake_update_base(store, embedder, repo, branch, files, **kwargs):
        captured["files"] = list(files)
        captured["ignore"] = list(kwargs.get("ignore", []))

    fake_components = MagicMock()
    monkeypatch.setattr(cli_mod, "build_components", lambda s: fake_components)
    monkeypatch.setattr(cli_mod, "_resolve_repo", lambda *a: "o/r")
    monkeypatch.setattr(cli_mod, "list_python_files",
                        lambda repo, ref: ["vendor/x.py", "reviewer/a.py"])
    monkeypatch.setattr(cli_mod, "rev_parse", lambda repo, ref: "deadbeef")
    monkeypatch.setattr(cli_mod, "build_code_graph",
                        lambda *a, **k: (set(), [], "tree-sitter"))

    def fake_file_at_ref(repo, path, ref):
        if path == ".review.yml":
            return "paths:\n  ignore:\n    - vendor\n"
        return "def f():\n    pass\n"

    monkeypatch.setattr(cli_mod, "file_at_ref", fake_file_at_ref)
    monkeypatch.setattr(cli_mod, "update_base", fake_update_base)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "main"])
    assert result.exit_code == 0, result.output
    assert captured["files"] == ["reviewer/a.py"]      # vendor/x.py отфильтрован
    assert "vendor" in captured["ignore"]


def test_index_uses_home_policy_without_committed_file(
    monkeypatch,
    isolated_xdg_config_home,
):
    """index применяет paths.ignore из home-слоя без committed policy."""
    home = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    home.parent.mkdir(parents=True)
    home.write_text("paths:\n  ignore:\n    - vendor\n", encoding="utf-8")
    captured = {}
    committed_refs = []

    def fake_update_base(store, embedder, repo, branch, files, **kwargs):
        captured["files"] = list(files)
        captured["ignore"] = list(kwargs.get("ignore", []))

    fake_components = MagicMock()
    monkeypatch.setattr(cli_mod, "build_components", lambda s: fake_components)
    monkeypatch.setattr(cli_mod, "_resolve_repo", lambda *a: "o/r")
    monkeypatch.setattr(cli_mod, "list_python_files",
                        lambda repo, ref: ["vendor/x.py", "reviewer/a.py"])
    monkeypatch.setattr(cli_mod, "rev_parse", lambda repo, ref: "deadbeef")
    monkeypatch.setattr(cli_mod, "build_code_graph",
                        lambda *a, **k: (set(), [], "tree-sitter"))

    def fake_file_at_ref(repo, path, ref):
        if path == ".review.yml":
            committed_refs.append((repo, path, ref))
            return None
        return "def f():\n    pass\n"

    monkeypatch.setattr(cli_mod, "file_at_ref", fake_file_at_ref)
    monkeypatch.setattr(cli_mod, "update_base", fake_update_base)

    result = CliRunner().invoke(cli_mod.cli, ["index", "/tmp/repo", "--ref", "release/2026"])
    assert result.exit_code == 0, result.output
    assert captured["files"] == ["reviewer/a.py"]
    assert captured["ignore"] == ["vendor"]
    assert committed_refs == [("/tmp/repo", ".review.yml", "release/2026")]
