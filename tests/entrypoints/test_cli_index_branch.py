from unittest.mock import MagicMock, patch
from click.testing import CliRunner
from reviewer.entrypoints.cli import cli


@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.build_code_graph", return_value=(["a.py#f"], [], "treesitter"))
@patch("reviewer.entrypoints.cli.rev_parse", return_value="deadbeef")
@patch("reviewer.entrypoints.cli.file_at_ref", return_value="def f(): pass")
@patch("reviewer.entrypoints.cli.list_python_files", return_value=["a.py"])
@patch("reviewer.entrypoints.cli.update_base")
def test_index_stores_under_branch_ref(m_update, m_lpf, m_far, m_rev, m_graph, m_build,
                                       monkeypatch, tmp_path):
    monkeypatch.setenv("DEFAULT_REPO", "a/x")
    monkeypatch.setenv("REVIEW_BRANCHES", "main,master")
    c = MagicMock()
    m_build.return_value = c
    runner = CliRunner()
    res = runner.invoke(cli, ["index", str(tmp_path), "--ref", "master", "--repo", "a/x"])
    assert res.exit_code == 0, res.output
    # update_base вызван с target_ref="master"
    assert m_update.call_args.args[3] == "master"
    # граф/мета пишутся под ветку master
    c.store.set_index_meta.assert_called_with("a/x", "base:master", "deadbeef")
    c.graph.clear.assert_called_with("a/x", branch="master")
    c.store.delete_paths_except.assert_called_with("a/x", "base:master", ["a.py"])


@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.build_code_graph", return_value=(["a.py#f"], [], "treesitter"))
@patch("reviewer.entrypoints.cli.rev_parse", return_value="deadbeef")
@patch("reviewer.entrypoints.cli.file_at_ref", return_value="def f(): pass")
@patch("reviewer.entrypoints.cli.list_python_files", return_value=["a.py"])
@patch("reviewer.entrypoints.cli.update_base")
def test_index_ref_not_validated_when_branch_given(m_update, m_lpf, m_far, m_rev, m_graph,
                                                    m_build, monkeypatch, tmp_path):
    """--branch задан явно → --ref (например, тег) не должен проверяться против
    отслеживаемых веток: расщепление ref↔branch — легальный сценарий."""
    monkeypatch.setenv("DEFAULT_REPO", "a/x")
    monkeypatch.setenv("REVIEW_BRANCHES", "main,master")
    c = MagicMock()
    m_build.return_value = c
    runner = CliRunner()
    res = runner.invoke(
        cli, ["index", str(tmp_path), "--ref", "v1.2", "--branch", "main",
              "--repo", "a/x"]
    )
    assert res.exit_code == 0, res.output
    # чтение файлов идёт с непроверенного ref v1.2 (не проверяется против
    # отслеживаемых веток)...
    m_lpf.assert_called_with(str(tmp_path), "v1.2")
    assert m_rev.call_args.args[1] == "v1.2"
    # ...а хранение (update_base принимает branch, не ref) — под явно заданной
    # меткой main
    assert m_update.call_args.args[3] == "main"
    c.store.set_index_meta.assert_called_with("a/x", "base:main", "deadbeef")
    c.graph.clear.assert_called_with("a/x", branch="main")
    c.store.delete_paths_except.assert_called_with("a/x", "base:main", ["a.py"])


@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.build_code_graph", return_value=(["a.py#f"], [], "treesitter"))
@patch("reviewer.entrypoints.cli.rev_parse", return_value="deadbeef")
@patch("reviewer.entrypoints.cli.file_at_ref", return_value="def f(): pass")
@patch("reviewer.entrypoints.cli.list_python_files", return_value=["a.py"])
@patch("reviewer.entrypoints.cli.update_base")
def test_index_ref_still_validated_without_branch(m_update, m_lpf, m_far, m_rev, m_graph,
                                                   m_build, monkeypatch, tmp_path):
    """--branch не задан → --ref остаётся ключом хранения и обязан быть
    отслеживаемой веткой (старое поведение сохранено)."""
    monkeypatch.setenv("DEFAULT_REPO", "a/x")
    monkeypatch.setenv("REVIEW_BRANCHES", "main,master")
    c = MagicMock()
    m_build.return_value = c
    runner = CliRunner()
    res = runner.invoke(cli, ["index", str(tmp_path), "--ref", "v1.2", "--repo", "a/x"])
    assert res.exit_code != 0
    assert "v1.2" in res.output
