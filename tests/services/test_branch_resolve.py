from types import SimpleNamespace

import pytest
from reviewer.config.settings import Settings
from reviewer.services import branch as branch_mod
from reviewer.services.branch import current_git_branch, resolve_branch


def _settings(csv):
    return Settings(_env_file=None, review_branches=csv)


def test_requested_in_allowlist_used():
    s = _settings("main,master")
    assert resolve_branch("master", "main", s) == "master"


def test_requested_outside_allowlist_raises():
    s = _settings("main,master")
    with pytest.raises(ValueError, match="develop"):
        resolve_branch("develop", "main", s)


def test_current_git_branch_used_when_tracked():
    s = _settings("main,master")
    assert resolve_branch(None, "master", s) == "master"


def test_falls_back_to_primary_when_current_untracked():
    s = _settings("main,master")
    assert resolve_branch(None, "feature/x", s) == "main"


def test_falls_back_to_primary_when_no_current():
    s = _settings("main,master")
    assert resolve_branch(None, None, s) == "main"


def test_current_git_branch_returns_stripped_name(monkeypatch):
    monkeypatch.setattr(
        branch_mod.subprocess, "run",
        lambda *a, **k: SimpleNamespace(stdout="feature/x\n"),
    )
    assert current_git_branch() == "feature/x"


def test_current_git_branch_returns_none_on_oserror(monkeypatch):
    def _boom(*a, **k):
        raise OSError("git not found")

    monkeypatch.setattr(branch_mod.subprocess, "run", _boom)
    assert current_git_branch() is None


def test_current_git_branch_returns_none_on_detached_head(monkeypatch):
    monkeypatch.setattr(
        branch_mod.subprocess, "run",
        lambda *a, **k: SimpleNamespace(stdout="\n"),
    )
    assert current_git_branch() is None
