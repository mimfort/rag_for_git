# tests/mcp/test_prepare_skip.py
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.services.review_service import BranchNotTrackedError


class FakeReviewService:
    def prepare(self, owner, name, pr, vcs_provider=None):
        raise BranchNotTrackedError("feature/zzz")


def test_prepare_review_returns_skip_payload(monkeypatch):
    s = Settings(_env_file=None, review_branches="main,master")

    class Comp:
        graph = None

    svc = MCPReviewService(s, Comp())
    svc._review_service = FakeReviewService()
    out = svc.prepare_review("a/x", 1)
    assert out["status"] == "skipped"
    assert "feature/zzz" in out["reason"]
