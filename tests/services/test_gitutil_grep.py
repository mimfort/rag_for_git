"""git-фолбэк similar-diffs по сообщениям коммитов на настоящем репозитории (PRI-257)."""
import subprocess

import pytest

from reviewer.gitutil import paths_touched_by_grep, rev_parse
from reviewer.retrieval.augment import collect_similar_task_paths


def _run(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    _run(tmp_path, "init", "-q", "-b", "main")
    _run(tmp_path, "config", "user.email", "t@example.com")
    _run(tmp_path, "config", "user.name", "t")
    return tmp_path


def _commit(repo, message, files):
    for name, body in files.items():
        (repo / name).write_text(body, encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", message)


@pytest.mark.integration
def test_paths_touched_by_grep_matches_task_key(repo):
    _commit(repo, "feat(x): PRI-999 сделано", {"a.py": "1"})
    _commit(repo, "чужой коммит", {"b.py": "1"})
    assert paths_touched_by_grep(str(repo), "PRI-999", limit=50) == ["a.py"]


@pytest.mark.integration
def test_paths_touched_by_grep_matches_merge_commit_branch_name(repo):
    # ключ задачи в этом репозитории обычно живёт не в теле обычного коммита,
    # а в имени ветки, попадающем в сообщение merge-коммита; --name-only для
    # merge-коммита по умолчанию файлов не печатает, поэтому нужен
    # --diff-merges=first-parent (см. paths_touched_by_grep).
    _commit(repo, "начальный коммит", {"d.py": "1"})
    _run(repo, "checkout", "-q", "-b", "feat/PRI-999-x")
    _commit(repo, "правка в ветке", {"c.py": "1"})
    _run(repo, "checkout", "-q", "main")
    _run(repo, "merge", "--no-ff", "-m",
         "Merge pull request #1 from feat/PRI-999-x", "feat/PRI-999-x")
    assert paths_touched_by_grep(str(repo), "PRI-999", limit=50) == ["c.py"]


@pytest.mark.integration
def test_non_git_path_is_fail_soft(tmp_path):
    assert paths_touched_by_grep(str(tmp_path / "нет"), "PRI-1", limit=10) == []


@pytest.mark.integration
def test_similar_diffs_searches_exact_alias_on_indexed_snapshot(repo):
    _commit(repo, "начальный коммит", {"base.py": "1"})
    _run(repo, "checkout", "-q", "-b", "feat/pri-27-scope")
    _commit(repo, "правка в ветке", {"own.py": "1"})
    _run(repo, "checkout", "-q", "main")
    _run(repo, "merge", "--no-ff", "-m",
         "Merge pull request #1 from feat/pri-27-scope", "feat/pri-27-scope")
    _commit(repo, "PRI-278 и xPRI-27", {"foreign.py": "1"})
    indexed_sha = rev_parse(str(repo), "HEAD")
    _commit(repo, "PRI-27 позже индексации", {"future.py": "1"})

    result = collect_similar_task_paths(
        repo="o/r", keys=["ID-27"], aliases_by_key={"ID-27": ["PRI-27"]},
        history=None, clone_path=str(repo), indexed_sha=indexed_sha, limit=10)

    assert result.paths == ["own.py"]
    assert result.gaps == []


@pytest.mark.integration
def test_git_fallback_does_not_treat_custom_key_as_regular_expression(repo):
    _commit(repo, "fix: Team.X-27", {"own.py": "1"})
    _commit(repo, "fix: TeamYX-27", {"foreign.py": "1"})
    assert paths_touched_by_grep(str(repo), "TEAM.X-27", limit=50) == ["own.py"]


@pytest.mark.integration
@pytest.mark.parametrize("indexed_sha", [None, "0" * 40, "--all"])
def test_similar_diffs_missing_snapshot_does_not_fall_back_to_head(repo, indexed_sha):
    _commit(repo, "PRI-27 есть только на HEAD", {"future.py": "1"})

    result = collect_similar_task_paths(
        repo="o/r", keys=["PRI-27"], aliases_by_key={}, history=None,
        clone_path=str(repo), indexed_sha=indexed_sha, limit=10)

    assert result.paths == []
    assert any("сним" in gap for gap in result.gaps)


@pytest.mark.integration
def test_similar_diffs_rejects_clone_of_another_repository(repo):
    _run(repo, "remote", "add", "origin", "https://github.com/o/other.git")
    _commit(repo, "PRI-27 в другом проекте", {"foreign.py": "1"})

    result = collect_similar_task_paths(
        repo="o/r", keys=["PRI-27"], aliases_by_key={}, history=None,
        clone_path=str(repo), indexed_sha=rev_parse(str(repo), "HEAD"), limit=10)

    assert result.paths == []
    assert result.gaps == ["клон недоступен, git-фолбэк пропущен"]
