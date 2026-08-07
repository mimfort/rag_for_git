from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from reviewer.entrypoints.cli import cli


@patch("reviewer.entrypoints.cli.remote_url")
@patch("reviewer.entrypoints.cli.build_components")
def test_migrate_branches_calls_store_and_graph(m_build, mock_remote_url, monkeypatch, tmp_path):
    monkeypatch.setenv("REVIEW_BRANCHES", "main,master")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mock_remote_url.return_value = "https://github.com/owner/myrepo.git"
    c = MagicMock()
    c.store.migrate_legacy_base.return_value = 5
    m_build.return_value = c
    res = CliRunner().invoke(cli, ["migrate-branches"])
    assert res.exit_code == 0, res.output
    c.store.migrate_legacy_base.assert_called_with("main")
    c.graph.migrate_legacy_branch.assert_called_with("main")


@patch("reviewer.entrypoints.cli.remote_url")
@patch("reviewer.entrypoints.cli.build_components")
def test_migrate_branches_explicit_repo_overrides_remote(
    m_build, mock_remote_url, monkeypatch, tmp_path
):
    """--repo резолвит ветки конкретного репозитория, а не производного от remote."""
    monkeypatch.setenv("REVIEW_BRANCHES", "dev,main")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mock_remote_url.return_value = None
    c = MagicMock()
    c.store.migrate_legacy_base.return_value = 3
    m_build.return_value = c
    res = CliRunner().invoke(cli, ["migrate-branches", "--repo", "owner/other"])
    assert res.exit_code == 0, res.output
    c.store.migrate_legacy_base.assert_called_with("dev")
    c.graph.migrate_legacy_branch.assert_called_with("dev")


@patch("reviewer.entrypoints.cli.remote_url")
def test_migrate_branches_no_repo_raises(mock_remote_url, monkeypatch, tmp_path):
    """Без --repo, git remote и DEFAULT_REPO — понятная ошибка, а не чужая ветка."""
    monkeypatch.setenv("DEFAULT_REPO", "")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mock_remote_url.return_value = None
    result = CliRunner().invoke(cli, ["migrate-branches"])
    assert result.exit_code != 0
    assert "repo" in result.output.lower()
