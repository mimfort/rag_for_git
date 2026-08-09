from click.testing import CliRunner

from reviewer.entrypoints.cli import cli


def _home(tmp_path, repo, body):
    path = tmp_path / "repos" / f"{repo}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_search_rejects_branch_outside_per_repo_index(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main,release")
    _home(tmp_path / "rag-reviewer", "o/r", "repository:\n  index_branches: [dev]\n")
    result = CliRunner().invoke(
        cli, ["search", "q", "--repo", "o/r", "--branch", "release"]
    )
    assert result.exit_code != 0
    assert "release" in result.output
    assert "REVIEW_BRANCHES" not in result.output


def test_two_repos_get_disjoint_index_branches(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    root = tmp_path / "rag-reviewer"
    _home(root, "o/a", "repository:\n  index_branches: [dev]\n")
    _home(root, "o/b", "repository:\n  index_branches: [trunk]\n")
    rejected = CliRunner().invoke(
        cli, ["search", "q", "--repo", "o/a", "--branch", "trunk"]
    )
    assert rejected.exit_code != 0
    assert "trunk" in rejected.output
