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
