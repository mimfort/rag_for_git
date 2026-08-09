# tests/services/test_review_service_branch_gate.py
"""Гейт prepare() маршрутизирует PR по per-repo ветками из домашних слоёв."""
import pytest
from reviewer.config.settings import Settings
from reviewer.services.review_service import BranchNotTrackedError, ReviewService
from reviewer.vcs.base import PullRequest


class FakeVCS:
    def __init__(self, base_ref):
        self.pull_request = PullRequest(
            number=1,
            base_sha="sha_base",
            head_sha="sha_head",
            base_ref=base_ref,
            title="t",
            body="",
        )

    def get_pull_request(self, n):
        return self.pull_request

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

    def existing_hashes(self, repo, ref):
        return set()


class FakeComponents:
    def __init__(self):
        self.store = FakeStore()
        self.graph = None
        self.embedder = None


@pytest.fixture
def fake_vcs():
    return FakeVCS(base_ref="main")


@pytest.fixture
def service():
    s = Settings(_env_file=None, review_branches="main,master")
    return ReviewService(s, FakeComponents())


def test_pr_into_untracked_branch_is_skipped_per_repo(tmp_path, monkeypatch, service, fake_vcs):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "rag-reviewer" / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("repository:\n  index_branches: [dev]\n", encoding="utf-8")
    fake_vcs.pull_request.base_ref = "main"
    with pytest.raises(BranchNotTrackedError):
        service.prepare("o", "r", 1, vcs_provider=fake_vcs)


def test_pr_into_home_tracked_branch_is_prepared(tmp_path, monkeypatch, service, fake_vcs):
    # Домашний слой отслеживает ветку, отсутствующую в REVIEW_BRANCHES: гейт
    # обязан пропустить PR (per-repo слой перекрывает глобальный env).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "rag-reviewer" / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("repository:\n  index_branches: [dev]\n", encoding="utf-8")
    fake_vcs.pull_request.base_ref = "dev"
    result = service.prepare("o", "r", 1, vcs_provider=fake_vcs)
    assert result.prq.base_ref == "dev"


def test_pr_gate_falls_back_to_env_without_home_layer(tmp_path, monkeypatch, service, fake_vcs):
    # Без домашних файлов поведение остаётся прежним (env REVIEW_BRANCHES).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    fake_vcs.pull_request.base_ref = "feature/zzz"
    with pytest.raises(BranchNotTrackedError):
        service.prepare("o", "r", 1, vcs_provider=fake_vcs)
