"""Unit-тест постинга walkthrough-гида (PRI-119). Сессия и VCS — фейки."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService


def _settings() -> Settings:
    s = Settings()
    s.voyage_api_key = "test"
    s.github_token = "test"
    s.default_repo = ""
    return s


def test_post_pr_walkthrough_posts_body_with_marker_and_no_comments():
    svc = MCPReviewService(_settings(), MagicMock())
    vcs = MagicMock()
    prepared = SimpleNamespace(vcs=vcs, prq=SimpleNamespace(head_sha="head456"))
    svc._session = lambda repo, pr: SimpleNamespace(prepared=prepared)   # изолируем сессию

    out = svc.post_pr_walkthrough("o/n", 7, "## Начни отсюда\n- a.py")

    assert out == {"posted": True, "pr": 7}
    vcs.publish_review.assert_called_once()
    number, head_sha, body, comments = vcs.publish_review.call_args.args
    assert number == 7 and head_sha == "head456"
    assert body.startswith("<!-- ai-walkthrough -->")
    assert "Начни отсюда" in body
    assert comments == []      # гид — без inline-находок


def test_post_pr_walkthrough_fail_soft_on_network_error():
    svc = MCPReviewService(_settings(), MagicMock())
    vcs = MagicMock()
    vcs.publish_review.side_effect = RuntimeError("boom")
    prepared = SimpleNamespace(vcs=vcs, prq=SimpleNamespace(head_sha="h"))
    svc._session = lambda repo, pr: SimpleNamespace(prepared=prepared)

    out = svc.post_pr_walkthrough("o/n", 7, "guide")
    assert out["posted"] is False
    assert "RuntimeError" in out["reason"]
