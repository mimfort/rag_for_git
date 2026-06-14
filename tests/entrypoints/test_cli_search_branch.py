from unittest.mock import MagicMock, patch
from click.testing import CliRunner
from reviewer.entrypoints.cli import cli


@patch("reviewer.entrypoints.cli.build_components")
def test_search_passes_base_ref_for_branch(m_build, monkeypatch):
    monkeypatch.setenv("DEFAULT_REPO", "a/x")
    monkeypatch.setenv("REVIEW_BRANCHES", "main,master")
    c = MagicMock()
    c.embedder.embed_query.return_value = [0.0] * 4
    c.store.hybrid_search.return_value = []
    m_build.return_value = c
    runner = CliRunner()
    res = runner.invoke(cli, ["search", "token", "--branch", "master"])
    assert res.exit_code == 0, res.output
    assert c.store.hybrid_search.call_args.kwargs["base_ref"] == "base:master"


@patch("reviewer.entrypoints.cli.build_components")
def test_search_rejects_unknown_branch(m_build, monkeypatch):
    monkeypatch.setenv("DEFAULT_REPO", "a/x")
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    c = MagicMock()
    m_build.return_value = c
    runner = CliRunner()
    res = runner.invoke(cli, ["search", "q", "--branch", "feature/xyz"])
    assert res.exit_code != 0
    assert "REVIEW_BRANCHES" in res.output
    # Проверка ветки стоит ДО build_components — компоненты не строятся.
    m_build.assert_not_called()
    c.store.hybrid_search.assert_not_called()
