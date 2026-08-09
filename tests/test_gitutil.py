import subprocess

import reviewer.gitutil as gitutil
from reviewer.gitutil import (
    changed_files,
    commits_behind,
    file_at_ref,
    remote_url,
)


def _run(*a, cwd): subprocess.run(a, cwd=cwd, check=True, capture_output=True)


def test_changed_files_and_file_at_ref(tmp_path):
    r = tmp_path
    _run("git", "init", "-q", cwd=r)
    _run("git", "config", "user.email", "t@t", "--local", cwd=r)
    _run("git", "config", "user.name", "t", "--local", cwd=r)
    (r / "a.py").write_text("x=1\n")
    _run("git", "add", "-A", cwd=r)
    _run("git", "commit", "-qm", "c1", cwd=r)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=r, capture_output=True, text=True
    ).stdout.strip()
    (r / "a.py").write_text("x=2\n")
    (r / "b.py").write_text("y=1\n")
    _run("git", "add", "-A", cwd=r)
    _run("git", "commit", "-qm", "c2", cwd=r)
    assert set(changed_files(str(r), base, "HEAD")) == {"a.py", "b.py"}
    assert file_at_ref(str(r), "a.py", base) == "x=1\n"


def test_commits_behind(tmp_path):
    r = tmp_path
    _run("git", "init", "-q", cwd=r)
    _run("git", "config", "user.email", "t@t", "--local", cwd=r)
    _run("git", "config", "user.name", "t", "--local", cwd=r)
    (r / "a.py").write_text("x=1\n")
    _run("git", "add", "-A", cwd=r)
    _run("git", "commit", "-qm", "c1", cwd=r)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r,
                         capture_output=True, text=True).stdout.strip()
    assert commits_behind(str(r), sha, "HEAD") == 0
    (r / "a.py").write_text("x=2\n")
    _run("git", "add", "-A", cwd=r)
    _run("git", "commit", "-qm", "c2", cwd=r)
    (r / "a.py").write_text("x=3\n")
    _run("git", "add", "-A", cwd=r)
    _run("git", "commit", "-qm", "c3", cwd=r)
    assert commits_behind(str(r), sha, "HEAD") == 2
    assert commits_behind(str(r), "0" * 40, "HEAD") is None        # мусорный sha
    assert commits_behind(str(tmp_path / "nope"), sha, "HEAD") is None  # не git-репо


def test_remote_url_returns_origin(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    assert remote_url(str(repo)) is None
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/owner/name.git"], check=True, capture_output=True)
    assert "github.com" in (remote_url(str(repo)) or "")


def test_repo_root_returns_top_level_from_nested_path(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "one" / "two"
    nested.mkdir(parents=True)
    _run("git", "init", "-q", cwd=repo)

    assert gitutil.repo_root(str(nested)) == str(repo.resolve())


def test_repo_root_returns_none_for_missing_and_non_git_paths(tmp_path):
    non_git = tmp_path / "non-git"
    non_git.mkdir()

    assert gitutil.repo_root(str(non_git)) is None
    assert gitutil.repo_root(str(tmp_path / "missing")) is None


def test_repo_root_returns_none_on_os_error(monkeypatch):
    def raise_os_error(*args):
        raise OSError("git unavailable")

    monkeypatch.setattr(gitutil, "_git", raise_os_error)

    assert gitutil.repo_root(".") is None


def test_remote_default_branch_returns_local_origin_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "-q", cwd=repo)
    _run(
        "git",
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/dev",
        cwd=repo,
    )

    assert gitutil.remote_default_branch(str(repo)) == "dev"


def test_remote_default_branch_returns_none_without_origin_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "-q", cwd=repo)

    assert gitutil.remote_default_branch(str(repo)) is None


def test_remote_default_branch_uses_only_local_symbolic_ref(monkeypatch):
    calls = []

    def fake_git(repo, *args):
        calls.append((repo, args))
        return "refs/remotes/origin/dev\n"

    monkeypatch.setattr(gitutil, "_git", fake_git)

    assert gitutil.remote_default_branch("/repo") == "dev"
    assert calls == [
        ("/repo", ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"))
    ]


def test_remote_default_branch_returns_none_for_invalid_ref(monkeypatch):
    monkeypatch.setattr(gitutil, "_git", lambda *args: "refs/heads/dev\n")

    assert gitutil.remote_default_branch("/repo") is None


def test_remote_default_branch_returns_none_on_os_error(monkeypatch):
    def raise_os_error(*args):
        raise OSError("git unavailable")

    monkeypatch.setattr(gitutil, "_git", raise_os_error)

    assert gitutil.remote_default_branch("/repo") is None
