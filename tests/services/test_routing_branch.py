# tests/services/test_routing_branch.py
import pytest
from reviewer.config.settings import Settings
from reviewer.services.review_service import ReviewService, BranchNotTrackedError
from reviewer.vcs.base import PullRequest


class FakeVCS:
    def __init__(self, base_ref):
        self._pr = PullRequest(number=1, base_sha="sha_base", head_sha="sha_head",
                               base_ref=base_ref, title="t", body="")

    def get_pull_request(self, n):
        return self._pr

    def get_changed_files(self, n):
        return []

    def get_file_at_ref(self, p, r):
        return None

    def compare_files(self, a, b):
        return []

    def close(self):
        pass


class FakeStore:
    def delete_ref(self, repo, ref):
        pass

    def get_index_meta(self, repo, ref):
        return None


class FakeComponents:
    def __init__(self):
        self.store = FakeStore()
        self.graph = None
        self.embedder = None


def _svc(csv):
    s = Settings(_env_file=None, review_branches=csv)
    return ReviewService(s, FakeComponents())


def test_untracked_branch_raises_skip():
    svc = _svc("main,master")
    with pytest.raises(BranchNotTrackedError) as exc:
        svc.prepare("a", "x", 1, vcs_provider=FakeVCS("feature/zzz"))
    assert exc.value.branch == "feature/zzz"
