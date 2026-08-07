from types import SimpleNamespace

import pytest

from reviewer.config.branches import RepoBranches
from reviewer.services import branch as branch_mod
from reviewer.services.branch import current_git_branch, resolve_branch


def _branches(*names):
    return RepoBranches(primary=names[0], index=tuple(names), source="test")


def test_requested_branch_is_used_when_tracked():
    assert resolve_branch("master", "main", _branches("main", "master")) == "master"


def test_requested_branch_outside_index_raises():
    with pytest.raises(ValueError) as exc:
        resolve_branch("develop", "main", _branches("main"))
    assert "develop" in str(exc.value)


def test_current_git_branch_used_when_tracked():
    assert resolve_branch(None, "master", _branches("main", "master")) == "master"


def test_untracked_current_branch_falls_back_to_primary():
    assert resolve_branch(None, "feature/x", _branches("main")) == "main"


def test_no_signal_falls_back_to_primary():
    assert resolve_branch(None, None, _branches("dev", "main")) == "dev"


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
