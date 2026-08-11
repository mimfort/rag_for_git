"""MCP читает коммиченный `.review.yml` из локального клона, если он известен (PRI-235)."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService


def _make_clone(tmp_path, *, remote: str | None, content: str):
    root = tmp_path / "clone"
    root.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (root / ".review.yml").write_text(content, encoding="utf-8")
    git("add", ".review.yml")
    git("commit", "-qm", "policy")
    if remote:
        git("remote", "add", "origin", remote)
    return root


def _svc(clone_path, committed_via_vcs: str):
    s = Settings()
    s.voyage_api_key = "test"
    s.github_token = "test"
    c = MagicMock()
    c.store.get_repo_clone.return_value = clone_path
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = committed_via_vcs
    created: list[str] = []

    def factory(owner, name):
        created.append(f"{owner}/{name}")
        return vcs

    return MCPReviewService(s, c, vcs_factory=factory), vcs, created


def test_known_clone_is_read_locally_without_vcs(tmp_path):
    """Путь из индекса валиден → слой читается локально, провайдер не создаётся."""
    clone = _make_clone(
        tmp_path, remote="https://github.com/o/r.git", content="max_comments: 11\n"
    )
    svc, vcs, created = _svc(str(clone), "max_comments: 7\n")

    policy, _meta = svc._resolve_policy("o/r", "main")

    assert policy.max_comments == 11
    assert created == []                      # VCS-провайдер даже не создан
    vcs.get_file_at_ref.assert_not_called()


def test_stale_clone_path_falls_back_to_vcs(tmp_path):
    """Путь указывает на чужой клон → прежнее поведение через VCS."""
    clone = _make_clone(
        tmp_path, remote="https://github.com/other/project.git",
        content="max_comments: 11\n",
    )
    svc, vcs, created = _svc(str(clone), "max_comments: 7\n")

    policy, _meta = svc._resolve_policy("o/r", "main")

    assert policy.max_comments == 7
    assert created == ["o/r"]
    vcs.get_file_at_ref.assert_called_once()


def test_unknown_clone_path_keeps_previous_behaviour(tmp_path):
    """Пути в индексе нет → поведение не меняется."""
    svc, vcs, created = _svc(None, "max_comments: 7\n")

    policy, _meta = svc._resolve_policy("o/r", "main")

    assert policy.max_comments == 7
    assert created == ["o/r"]


def test_store_failure_does_not_break_policy_resolution(tmp_path):
    """Недоступный Postgres лишает резолв локального пути, но не роняет его."""
    svc, vcs, created = _svc(None, "max_comments: 7\n")
    svc.components.store.get_repo_clone.side_effect = RuntimeError("pg down")

    policy, _meta = svc._resolve_policy("o/r", "main")

    assert policy.max_comments == 7
    assert created == ["o/r"]
