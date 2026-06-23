"""Тест: prepare прокидывает paths.ignore из .review.yml base-ветки в build_overlay."""
from unittest.mock import MagicMock

import reviewer.services.review_service as rs
from reviewer.config.settings import Settings
from reviewer.vcs.base import ChangedFile, PullRequest


def _settings() -> Settings:
    """Минимальные настройки для unit-теста (аналог фикстуры test_review_service)."""
    s = Settings()
    s.review_history = False
    s.review_skip_drafts = False
    s.review_max_files = 50
    s.voyage_api_key = "test"
    s.github_token = "test"
    s.task_board_type = ""
    s.task_board_mcp = ""
    s.task_board_key_pattern = ""
    s.task_board_url_template = ""
    return s


def test_prepare_passes_ignore_to_build_overlay(monkeypatch):
    """prepare() резолвит paths.ignore из .review.yml целевой ветки и прокидывает
    в build_overlay — агент не видит игнорируемые пути в overlay."""
    captured = {}
    monkeypatch.setattr(rs, "build_overlay",
                        lambda *a, **k: captured.update(ignore=k.get("ignore")))
    monkeypatch.setattr(rs, "chunk_python", lambda path, src: [])
    monkeypatch.setattr(rs, "_structural_summary", lambda *a, **k: "")

    vcs = MagicMock()
    pr = PullRequest(
        number=7, base_sha="b", head_sha="h", base_ref="main",
        title="Test PR", body="", draft=False,
    )
    vcs.get_pull_request.return_value = pr
    vcs.get_changed_files.return_value = [
        ChangedFile(path="reviewer/a.py", status="modified", patch="@@"),
    ]
    vcs.get_file_at_ref.side_effect = (
        lambda path, sha: "paths:\n  ignore:\n    - vendor\n"
        if path == ".review.yml" else "def f():\n    pass\n")

    components = MagicMock()
    components.store.get_index_meta.return_value = None     # без base-досинка

    svc = rs.ReviewService(_settings(), components)
    svc.prepare("o", "r", 7, vcs_provider=vcs)
    assert captured.get("ignore") == ["vendor"]
