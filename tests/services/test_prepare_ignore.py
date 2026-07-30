"""Тесты policy paths.ignore при подготовке ревью."""
from unittest.mock import MagicMock, call

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


def _minimal_vcs_for_prepare() -> MagicMock:
    """VCS-двойник с минимальным PR и одним изменённым Python-файлом."""
    vcs = MagicMock()
    pr = PullRequest(
        number=7, base_sha="b", head_sha="h", base_ref="main",
        title="Test PR", body="", draft=False,
    )
    vcs.get_pull_request.return_value = pr
    vcs.get_changed_files.return_value = [
        ChangedFile(path="reviewer/a.py", status="modified", patch="@@"),
    ]
    return vcs


def test_prepare_passes_ignore_to_build_overlay(monkeypatch):
    """prepare() передаёт committed paths.ignore в overlay."""
    captured = {}
    monkeypatch.setattr(rs, "build_overlay",
                        lambda *a, **k: captured.update(ignore=k.get("ignore")))
    monkeypatch.setattr(rs, "chunk_python", lambda path, src: [])
    monkeypatch.setattr(rs, "_structural_summary", lambda *a, **k: "")

    vcs = _minimal_vcs_for_prepare()
    vcs.get_file_at_ref.side_effect = (
        lambda path, sha: "paths:\n  ignore:\n    - vendor\n"
        if path == ".review.yml" else "def f():\n    pass\n")

    components = MagicMock()
    components.store.get_index_meta.return_value = None     # без base-досинка

    svc = rs.ReviewService(_settings(), components)
    svc.prepare("o", "r", 7, vcs_provider=vcs)
    assert captured.get("ignore") == ["vendor"]


def test_prepare_uses_home_policy_without_committed_file(monkeypatch, tmp_path):
    """Home-policy применяется, когда в base-коммите нет .review.yml."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    home = tmp_path / "rag-reviewer/repos/o/r.yml"
    home.parent.mkdir(parents=True)
    home.write_text(
        "paths: {ignore: [vendor]}\nmax_comments: 4\ntask_board: null\n",
        encoding="utf-8",
    )
    captured = {}
    monkeypatch.setattr(rs, "build_overlay", lambda *a, **k: captured.update(ignore=k["ignore"]))
    monkeypatch.setattr(rs, "chunk_python", lambda path, src: [])
    monkeypatch.setattr(rs, "_structural_summary", lambda *a, **k: "")
    vcs = _minimal_vcs_for_prepare()
    vcs.get_file_at_ref.side_effect = (
        lambda path, ref: None if path == ".review.yml" else "def f():\n    pass\n"
    )
    components = MagicMock()
    components.store.get_index_meta.return_value = None

    prepared = rs.ReviewService(_settings(), components).prepare(
        "o", "r", 7, vcs_provider=vcs
    )

    assert captured["ignore"] == ["vendor"]
    assert prepared.policy.max_comments == 4
    assert prepared.config_sources["sources"]["paths"] == "home:repos/o/r.yml"
    committed_calls = [
        call for call in vcs.get_file_at_ref.call_args_list if call.args[0] == ".review.yml"
    ]
    assert committed_calls == [call(".review.yml", "b")]
