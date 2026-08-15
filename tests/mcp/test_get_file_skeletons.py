"""PRI-245: контракт session-less тула get_file_skeletons."""
from unittest.mock import MagicMock

import pytest

from reviewer.config.settings import Settings
from reviewer.mcp.service import _MAX_SKELETON_PATHS, MCPReviewService

SRC_A = '''class A:
    """Класс A."""

    def m(self):
        return 1
'''


def _fetch(repo, branch, paths):
    rows = {"a.py": [(1, SRC_A)]}
    return [
        (path, start, text)
        for path in paths
        for start, text in rows.get(path, [])
    ]


@pytest.fixture
def service(monkeypatch):
    settings = Settings()
    settings.voyage_api_key = "test"
    settings.github_token = "test"
    components = MagicMock()
    components.store.fetch_chunks_at_paths.side_effect = _fetch
    svc = MCPReviewService(settings, components)
    monkeypatch.setattr(
        svc, "_resolve_repo_branch", lambda repo, branch: ("owner/name", "dev")
    )
    return svc


def test_returns_line_numbered_skeleton(service):
    out = service.get_file_skeletons("owner/name", ["a.py"])
    assert "1|class A:" in out["a.py"]
    assert "4|    def m(self):" in out["a.py"]
    assert "return 1" not in out["a.py"]


def test_missing_path_gets_note_not_exception(service):
    out = service.get_file_skeletons("owner/name", ["nope.py"])
    assert out["nope.py"] == "(файл не найден в индексе: nope.py)"


def test_batch_returns_every_requested_path(service):
    out = service.get_file_skeletons("owner/name", ["a.py", "nope.py"])
    assert set(out) == {"a.py", "nope.py"}


def test_paths_over_cap_are_reported_not_dropped(service):
    paths = [f"f{i}.py" for i in range(_MAX_SKELETON_PATHS + 3)]
    out = service.get_file_skeletons("owner/name", paths)
    assert set(out) == set(paths), "усечение не должно быть молчаливым"
    assert out[paths[-1]].startswith("(превышен лимит путей на вызов")


def test_empty_paths_returns_empty(service):
    assert service.get_file_skeletons("owner/name", []) == {}
