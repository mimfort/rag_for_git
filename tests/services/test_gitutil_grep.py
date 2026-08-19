"""git-фолбэк similar-diffs по сообщениям коммитов на настоящем репозитории (PRI-257)."""
import subprocess

import pytest

from reviewer.gitutil import paths_touched_by_grep


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
