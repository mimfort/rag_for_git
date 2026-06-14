from unittest.mock import MagicMock, patch
from click.testing import CliRunner
from reviewer.entrypoints.cli import cli


@patch("reviewer.entrypoints.cli.build_components")
def test_migrate_branches_calls_store_and_graph(m_build, monkeypatch):
    monkeypatch.setenv("REVIEW_BRANCHES", "main,master")
    c = MagicMock()
    c.store.migrate_legacy_base.return_value = 5
    m_build.return_value = c
    res = CliRunner().invoke(cli, ["migrate-branches"])
    assert res.exit_code == 0, res.output
    c.store.migrate_legacy_base.assert_called_with("main")
    c.graph.migrate_legacy_branch.assert_called_with("main")
